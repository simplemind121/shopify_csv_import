# -*- coding: utf-8 -*-
import base64
import hashlib
import io
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from odoo import api, fields, models
from odoo.tools import config

_logger = logging.getLogger(__name__)

# 本地模式下 Odoo 只保存最大 1920px 的图片（image_1920），Shopify CDN 支持按参数缩放，
# 直接下缩好的版本。备份到对象存储时则下载原图（那是将来唯一的原图备份）。
SHOPIFY_CDN_HOSTS = ('cdn.shopify.com',)
SHOPIFY_MAX_PX = 1920
# 单批图片同步最多占用"cron / 请求超时时间"的这个比例，剩下的留给最后一张图
# （下载超时 30s + 上传超时 60s）收尾，保证不会被 Odoo 强杀。
TIME_BUDGET_RATIO = 0.5
# 同时进行的网络请求数（下载 / 链接检查）。线程里只做 HTTP 请求，
# 所有数据库写入和上传仍然在主线程里逐张进行。
DOWNLOAD_WORKERS = 8
# 明确表示"文件不存在"的 HTTP 状态码。超时、5xx、403 等都不算（可能是临时故障）
GONE_CODES = (404, 410)
# 一个批次连续这么多张图上传对象存储失败、且一张都没成功过，就自动暂停这个批次：
# 这种情况基本都是配置问题（token、上传文件夹、图片源被停用），继续跑只会把剩下
# 的图全部下载一遍再标成"缺备份"
AUTO_PAUSE_AFTER_FAILURES = 5

try:
    import requests
except ImportError:  # pragma: no cover - requests 是 Odoo 标准依赖，正常都有
    requests = None

JOBS = [
    ('sync', '同步'),
    ('backup', '补传备份'),
    ('verify', '对账'),
]


class ShopifyImageQueue(models.Model):
    """图片台账：每张 Shopify 图片一行，记录它在三个地方的情况——

    - 源：Shopify CDN 上的原图（source_url）
    - 备份：通过 media_picker 的图片源上传到对象存储后的 CDN 直链（backup_url）
    - 本地：只有主图有，由 media_picker 把主图外链同步进商品的 image_1920

    前台显示哪条链接（display_source）写在 media_picker 的 media.bind 上：
    Shopify 链接还在就用 Shopify 的，确认失效后换成对象存储的备份。
    """
    _name = 'shopify.image.queue'
    _description = 'Shopify 图片台账'
    _order = 'product_tmpl_id, sequence, id'

    product_tmpl_id = fields.Many2one(
        'product.template', string='商品', required=True, ondelete='cascade', index=True)
    product_variant_id = fields.Many2one(
        'product.product', string='对应变体', ondelete='cascade',
        help='仅当 Shopify 该行有 Variant Image 时才会关联到具体变体，否则为空表示商品公共图片。')
    sequence = fields.Integer(string='顺序', default=10)
    role = fields.Selection([
        ('main', '主图'),
        ('extra', '附加图片'),
    ], string='类型', default='extra', required=True)
    batch_id = fields.Many2one(
        'shopify.import.batch', string='导入批次', ondelete='set null', index=True)

    # ---- 任务状态 ----
    job = fields.Selection(JOBS, string='任务', default='sync', required=True, readonly=True)
    state = fields.Selection([
        ('pending', '待处理'),
        ('done', '已完成'),
        ('error', '失败'),
    ], string='状态', default='pending', index=True)
    error_message = fields.Char(string='错误信息')
    # 实际处理完成（成功或失败）的时间，用来算同步速度。不能用 write_date：
    # 改批次、重新排队等操作也会更新 write_date
    processed_at = fields.Datetime(string='处理时间', readonly=True, copy=False, index=True)

    # ---- 源：Shopify ----
    source_url = fields.Char(string='Shopify 原图链接', required=True)
    source_state = fields.Selection([
        ('unknown', '未检查'),
        ('ok', '正常'),
        ('gone', '已失效'),
    ], string='Shopify 源', default='unknown', readonly=True, copy=False)
    source_checked_at = fields.Datetime(string='源检查时间', readonly=True, copy=False)
    payload_size = fields.Integer(string='原图大小（字节）', readonly=True, copy=False)
    payload_sha256 = fields.Char(string='原图 SHA-256', readonly=True, copy=False)
    width = fields.Integer(string='宽', readonly=True, copy=False)
    height = fields.Integer(string='高', readonly=True, copy=False)

    # ---- 备份：对象存储（media_picker 的图片源）----
    media_source_id = fields.Many2one(
        'media.source', string='对象存储图片源',
        help='media_picker 里已经配置好、开启了上传的图片源（Alist / S3）。'
             '留空则该图片直接存成 Odoo 本地图片，不走外链。')
    alist_target_path = fields.Char(
        string='备份路径',
        help='上传到图片源后的相对路径（相对图片源的根目录），例如 shopify-products/123_foo.jpg。')
    backup_state = fields.Selection([
        ('none', '未备份'),
        ('ok', '已备份'),
        ('failed', '上传失败'),
        ('missing', '备份文件不见了'),
        ('mismatch', '大小不一致'),
    ], string='对象存储备份', default='none', readonly=True, copy=False)
    backup_url = fields.Char(string='对象存储 CDN 直链', readonly=True, copy=False)
    backup_ref = fields.Char(string='对象存储内路径', readonly=True, copy=False)
    backup_size = fields.Integer(string='备份大小（字节）', readonly=True, copy=False)
    backup_checked_at = fields.Datetime(string='备份检查时间', readonly=True, copy=False)
    cdn_error = fields.Char(string='备份失败原因', readonly=True, copy=False)

    # ---- 本地 ----
    storage = fields.Selection([
        ('cdn', '外链 + 对象存储备份'),
        ('link', '仅 Shopify 外链（缺备份）'),
        ('binary', '本地图片'),
    ], string='存储方式', readonly=True, copy=False)
    # 本地模式下建的附加图片；以后改走外链时要删掉它，不能让同一张图在画廊里出现两次
    binary_image_id = fields.Many2one(
        'product.image', string='本地附加图片', ondelete='set null', readonly=True, copy=False)
    local_state = fields.Selection([
        ('na', '不存本地'),
        ('ok', '已存本地'),
        ('none', '本地缺图'),
    ], string='本地图片', compute='_compute_local_state')
    local_image = fields.Image(string='本地图片预览', related='product_tmpl_id.image_256')

    # ---- 前台显示 ----
    bind_id = fields.Many2one(
        'media.bind', string='外链记录', ondelete='set null', readonly=True, copy=False)
    display_source = fields.Selection([
        ('shopify', 'Shopify CDN'),
        ('backup', '对象存储 CDN'),
        ('local', '本地图片'),
    ], string='前台显示来源', readonly=True, copy=False)
    display_url = fields.Char(string='当前显示链接', related='bind_id.url')
    bind_health = fields.Selection(string='显示链接健康', related='bind_id.health_status')

    verdict = fields.Selection([
        ('ok', '一致'),
        ('local', '本地模式'),
        ('no_backup', '缺备份'),
        ('backup_missing', '备份文件丢失'),
        ('mismatch', '备份与原图不一致'),
        ('source_gone', 'Shopify 已失效（备份已接替）'),
        ('lost', 'Shopify 已失效且无备份'),
        ('unchecked', '未对账'),
    ], string='对账结论', compute='_compute_verdict', store=True, index=True)

    @api.depends('role', 'product_variant_id', 'product_tmpl_id.image_128', 'media_source_id', 'storage')
    def _compute_local_state(self):
        for rec in self:
            if rec.media_source_id and (rec.role != 'main' or rec.product_variant_id):
                rec.local_state = 'na'
            elif rec.role == 'main' and not rec.product_variant_id:
                rec.local_state = 'ok' if rec.product_tmpl_id.image_128 else 'none'
            else:
                rec.local_state = 'ok' if rec.storage == 'binary' else 'na'

    @api.depends('state', 'storage', 'media_source_id', 'source_state', 'backup_state')
    def _compute_verdict(self):
        for rec in self:
            backed_up = rec.backup_state == 'ok'
            if rec.state != 'done' and not rec.storage:
                rec.verdict = 'unchecked'
            elif not rec.media_source_id and rec.storage == 'binary':
                rec.verdict = 'local'
            elif rec.source_state == 'gone':
                rec.verdict = 'source_gone' if backed_up else 'lost'
            elif rec.backup_state in ('none', 'failed'):
                rec.verdict = 'no_backup'
            elif rec.backup_state == 'missing':
                rec.verdict = 'backup_missing'
            elif rec.backup_state == 'mismatch':
                rec.verdict = 'mismatch'
            else:
                rec.verdict = 'ok'

    # ---------------------------------------------------------------
    # cron 入口
    # ---------------------------------------------------------------
    @api.model
    def _cron_process_pending(self, limit=30, batch=None):
        """处理一批待处理的图片任务（同步 / 补传备份 / 对账），返回本次处理的张数。

        - 在 cron 里运行时，每处理完一张就用 ir.cron._commit_progress 提交一次：
          一张图平均要几秒（实测从 Shopify CDN 下载约 3 秒/张），整批放在一个
          事务里的话，一旦超过 cron 的超时时间整批回滚，下一次又从同一批重来，
          队列永远不动。逐张提交后最多损失正在处理的那一张。
        - 同时把队列里剩余的数量报告给 Odoo，队列没清空时 Odoo 会在同一次触发里
          接着跑下一批（最多 10 轮），不用干等下一个周期。
        - 按时间预算停止：超过预算就不再开始处理新图片，避免被 Odoo 强杀。
        """
        in_cron = bool(self.env.context.get('cron_id'))
        IrCron = self.env['ir.cron']
        now = time.monotonic()
        deadline = now + self._time_budget(in_cron)
        if in_cron:
            job_deadline = self._cron_job_deadline()
            if job_deadline is not None:
                deadline = min(deadline, job_deadline)
            if now >= deadline:
                # 整个 cron 任务的时间预算已经用完：报告"没有剩余"，让这次触发结束，
                # 剩下的交给下一个周期
                IrCron._commit_progress(remaining=0)
                return 0

        domain = self._pending_domain()
        if batch:
            domain = [('batch_id', '=', batch.id)] + domain
        records = self.search(domain, limit=limit, order='id')
        if in_cron:
            IrCron._commit_progress(remaining=self.search_count(domain))

        processed = 0
        streak = {}  # 批次 id -> 本轮连续上传失败的张数
        for start in range(0, len(records), DOWNLOAD_WORKERS):
            if processed and time.monotonic() >= deadline:
                break
            chunk = records[start:start + DOWNLOAD_WORKERS].filtered(
                lambda r: not r.batch_id.paused)
            if not chunk:
                continue
            # 进度说明：导入批次页面上会实时显示这一句
            verifying = all(r.job == 'verify' for r in chunk)
            chunk.batch_id._set_status(
                f'正在检查 {len(chunk)} 张图片的链接' if verifying
                else f'正在并行下载 {len(chunk)} 张图片')
            if in_cron:
                IrCron._commit_progress(0)
            fetched = self._prefetch(chunk)
            for rec in chunk:
                if rec.batch_id.paused:
                    continue  # 这一组处理到一半时批次被自动暂停了：剩下的留着不动
                if rec.media_source_id and rec.job != 'verify':
                    rec.batch_id._set_status(
                        f'正在上传对象存储：{self._guess_filename(rec.source_url)}')
                rec._process(download=fetched.get(rec.id))
                processed += 1
                rec._track_upload_streak(streak)
                if in_cron:
                    IrCron._commit_progress(1)
        records.batch_id._refresh_image_state()
        return processed

    def _track_upload_streak(self, streak):
        """记录所属批次连续上传失败的张数，达到阈值就自动暂停批次。"""
        batch = self.batch_id
        if not batch or not self.media_source_id or self.job == 'verify':
            return
        if self.backup_state == 'ok':
            streak[batch.id] = 0
            return
        streak[batch.id] = streak.get(batch.id, 0) + 1
        if streak[batch.id] >= AUTO_PAUSE_AFTER_FAILURES and not batch.paused:
            reason = self.cdn_error or self.error_message or '未知原因'
            batch.write({
                'paused': True,
                'status_message': f'已自动暂停：连续 {streak[batch.id]} 张图片上传对象存储失败（{reason}）。'
                                  f'请检查图片源的 token / 上传文件夹，修好后点「继续」',
            })
            _logger.warning('批次 %s 连续 %s 张图片上传失败，已自动暂停: %s',
                            batch.id, streak[batch.id], reason)

    @api.model
    def _pending_domain(self):
        """待处理、且所属批次没有被暂停的图片（升级前导入的旧记录没有批次）。"""
        # 批次还在导入商品阶段时先不处理它的图片：避免和商品导入同时写同一个商品
        return [
            ('state', '=', 'pending'),
            '|', ('batch_id', '=', False),
            '&', ('batch_id.paused', '=', False),
            ('batch_id.state', 'not in', ('queued', 'importing')),
        ]

    @staticmethod
    def _image_key(url):
        """图片去重键：Shopify CDN 的 URL 去掉 v（版本时间戳）和缩放参数。"""
        parts = urlparse(url or '')
        if parts.hostname not in SHOPIFY_CDN_HOSTS:
            return url
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k not in ('v', 'width', 'height', 'crop')]
        return urlunparse(parts._replace(query=urlencode(query)))

    @api.model
    def _prefetch(self, records):
        """并行做这一组的网络请求，返回 {记录id: 结果 或 抛出的异常}。

        - 同步 / 补传备份：下载图片字节（走外链备份时下原图，本地模式下 1920px 版本）
        - 对账：检查 Shopify 链接和备份链接
        """
        if len(records) <= 1:
            return {}
        tasks = {
            rec.id: (rec.job, rec.source_url, bool(rec.media_source_id), rec.backup_url)
            for rec in records
        }

        def run(task):
            job, source_url, original, backup_url = task
            try:
                if job == 'verify':
                    return {'source': self._probe(source_url),
                            'backup': self._probe(backup_url) if backup_url else None}
                return self._download(source_url, original=original)
            except Exception as e:  # 在 _process 里统一处理
                return e

        with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(tasks))) as pool:
            return dict(zip(tasks.keys(), pool.map(run, tasks.values())))

    @api.model
    def _time_budget(self, in_cron):
        """本批最多可以用多少秒（按 Odoo 的超时配置算，没有限制时按 5 分钟）。"""
        limit = config.get('limit_time_real_cron', -1) if in_cron else -1
        if limit is None or limit < 0:
            limit = config.get('limit_time_real', 120)
        if not limit or limit <= 0:
            limit = 600
        return limit * TIME_BUDGET_RATIO

    @api.model
    def _cron_job_deadline(self):
        """cron 会把本函数循环调用多轮；这里算出整个 cron 任务的截止时间。

        Odoo 19 在 context 里放了 cron_end_time = 任务开始时间 + MIN_TIME_PER_JOB。
        """
        end_time = self.env.context.get('cron_end_time')
        if not end_time:
            return None
        try:
            from odoo.addons.base.models.ir_cron import MIN_TIME_PER_JOB
        except ImportError:  # pragma: no cover - 以后的 Odoo 版本改名了就退化成只按单批控制
            return None
        return end_time - MIN_TIME_PER_JOB + self._time_budget(True)

    # ---------------------------------------------------------------
    # 台账上的操作（中继 / 对账 / 切换显示）
    # ---------------------------------------------------------------
    def _queue_job(self, job):
        """把选中的图片排进后台任务，由定时任务处理（进度在批次页面里看）。"""
        self.write({'job': job, 'state': 'pending', 'error_message': False})
        self.batch_id._reopen_for_images(f'{len(self)} 张图片已排队：{dict(JOBS)[job]}')
        if not self.batch_id:
            self.env['shopify.import.batch']._trigger_image_sync()
        return True

    def action_retry(self):
        """重新完整同步：重新从 Shopify 拉原图，覆盖备份和显示链接。"""
        return self._queue_job('sync')

    def action_push_backup(self):
        """中继：把图片（重新）上传到对象存储。Shopify 源已失效时，主图改用本地那份上传。"""
        return self.filtered('media_source_id')._queue_job('backup')

    def action_verify(self):
        """对账：检查 Shopify 源和对象存储备份是否都在、大小是否一致。"""
        return self._queue_job('verify')

    def action_switch_to_backup(self):
        """手动把前台显示切到对象存储的 CDN 直链。"""
        for rec in self.filtered(lambda r: r.backup_state == 'ok' and r.backup_url):
            rec.write(rec._write_bind('backup'))
        return True

    def action_switch_to_shopify(self):
        """手动把前台显示切回 Shopify CDN。"""
        for rec in self.filtered(lambda r: r.media_source_id and r.source_state != 'gone'):
            rec.write(rec._write_bind('shopify'))
        return True

    @api.model
    def _cron_failover(self):
        """Shopify 链接被判定失效的图片，把前台显示换成对象存储的备份。

        "失效"有两个来源：media_picker 自己的链接健康检查把这条外链标成 broken，
        或者本模块对账时 Shopify 明确返回 404/410。没有备份的会在台账里标成
        "Shopify 已失效且无备份"，需要人工处理。
        """
        rows = self.search([
            ('display_source', '=', 'shopify'), ('bind_id', '!=', False),
            '|', ('bind_id.health_status', '=', 'broken'), ('source_state', '=', 'gone'),
        ])
        switched = 0
        for rec in rows:
            vals = {'source_state': 'gone'}
            if rec.backup_state == 'ok' and rec.backup_url:
                vals.update(rec._write_bind('backup'))
                switched += 1
            rec.write(vals)
        if rows:
            _logger.warning('Shopify 图片源失效 %s 张，其中 %s 张已切到对象存储备份',
                            len(rows), switched)
        return switched

    # ---------------------------------------------------------------
    # 核心处理逻辑
    # ---------------------------------------------------------------
    def _process(self, download=None):
        """处理一张图当前排的任务。download 是预先取好的结果（字节 / 对账结果 / 异常）。"""
        self.ensure_one()
        try:
            if isinstance(download, Exception) and self.job != 'backup':
                raise download
            # savepoint：写图片时如果触发数据库错误（例如图片字段校验失败），
            # 只回滚这一张图，事务还能继续把 state=error 写进去、处理下一张。
            with self.env.cr.savepoint():
                vals = getattr(self, f'_job_{self.job}')(download)
            self.write(dict(vals, state='done', error_message=False,
                            processed_at=fields.Datetime.now()))
        except Exception as e:
            _logger.exception('图片任务失败（%s）: %s', self.job, self.source_url)
            vals = {'state': 'error', 'error_message': str(e)[:250],
                    'processed_at': fields.Datetime.now()}
            if self.job == 'backup' and self.backup_state != 'ok':
                # 补传失败：「备份失败原因」也换成这次的原因，不留着上一次的
                vals.update(backup_state='failed', cdn_error=str(e)[:250])
            self.write(vals)

    def _job_sync(self, content=None):
        """完整同步一张图。

        没选图片源（本地模式）：下载后存成 Odoo 本地图片，和以前一样。
        选了图片源：下载原图 → 上传对象存储（备份）→ 前台显示写 Shopify 链接；
        主图由 media_picker 自动同步进商品的本地图片，其余图片不存本地。
        """
        if not self.media_source_id:
            content = content or self._download(self.source_url)
            if not content:
                raise ValueError('下载失败或返回空内容')
            self._save_binary(content)
            return {'storage': 'binary', 'cdn_error': False, 'display_source': 'local',
                    'source_state': 'ok', 'source_checked_at': fields.Datetime.now()}

        content = content or self._download(self.source_url, original=True)
        if not content:
            raise ValueError('下载失败或返回空内容')
        vals = self._fingerprint(content)
        vals.update(source_state='ok', source_checked_at=fields.Datetime.now())
        vals.update(self._backup(content))
        vals.update(self._write_bind('shopify', backup_url=vals.get('backup_url')))
        return vals

    def _job_backup(self, content=None):
        """中继：把图片上传到对象存储。源下不到时，主图改用本地那份。"""
        if isinstance(content, Exception) or content is None:
            try:
                content = self._download(self.source_url, original=True)
            except Exception as e:
                content = self._local_bytes()
                if not content:
                    raise ValueError(f'Shopify 源下载失败（{e}），本地也没有这张图，无法补传') from e
        vals = self._fingerprint(content)
        vals.update(self._backup(content))
        if vals['backup_state'] != 'ok':
            raise ValueError(vals['cdn_error'])
        prefer = self.display_source if self.display_source in ('shopify', 'backup') else 'shopify'
        if self.source_state == 'gone':
            prefer = 'backup'
        vals.update(self._write_bind(prefer, backup_url=vals['backup_url']))
        return vals

    def _job_verify(self, result=None):
        """对账：Shopify 源 / 对象存储备份各自还在不在，备份大小和原图对不对得上。"""
        result = result or {
            'source': self._probe(self.source_url),
            'backup': self._probe(self.backup_url) if self.backup_url else None,
        }
        now = fields.Datetime.now()
        vals = {}
        state, _code, _size = result['source']
        if state != 'unknown':  # 网络错误不改结论
            vals.update(source_state=state, source_checked_at=now)
        if result.get('backup'):
            state, code, size = result['backup']
            # 对象存储对"路径不对 / 文件不存在"经常回 403 而不是 404，所以备份这一侧 403 也算丢失
            if state == 'gone' or code == 403:
                vals.update(backup_state='missing', backup_checked_at=now)
            elif state == 'ok':
                mismatch = bool(size and self.payload_size and size != self.payload_size)
                vals.update(backup_state='mismatch' if mismatch else 'ok',
                            backup_size=size or self.backup_size, backup_checked_at=now)
        source_gone = vals.get('source_state', self.source_state) == 'gone'
        backup_ok = vals.get('backup_state', self.backup_state) == 'ok'
        if source_gone and backup_ok and self.display_source == 'shopify':
            vals.update(self._write_bind('backup'))
        return vals

    # ---------------------------------------------------------------
    # 本地图片
    # ---------------------------------------------------------------
    def _save_binary(self, content):
        b64 = base64.b64encode(content)
        if self.product_variant_id:
            # 变体专属图片只挂到该变体上，不再额外塞进商品公共画廊
            self.product_variant_id.image_1920 = b64
        elif self.role == 'main':
            self.product_tmpl_id.image_1920 = b64
        elif self.binary_image_id:
            # 之前已经存过一份本地附加图（比如 Shopify 里图片更新了）：覆盖，不新增
            self.binary_image_id.image_1920 = b64
        else:
            self.binary_image_id = self.env['product.image'].create({
                'product_tmpl_id': self.product_tmpl_id.id,
                'name': self.product_tmpl_id.name,
                'image_1920': b64,
            })

    def _local_bytes(self):
        """这张图在 Odoo 本地的那份字节（只有主图 / 变体图 / 本地附加图有）。"""
        holder = (self.binary_image_id or self.product_variant_id
                  or (self.role == 'main' and self.product_tmpl_id))
        data = holder and holder.image_1920
        return base64.b64decode(data) if data else b''

    # ---------------------------------------------------------------
    # 对象存储备份（通过 media_picker 的 media.source.upload_media）
    # ---------------------------------------------------------------
    def _backup(self, content):
        """把字节上传到图片源，返回要写到台账上的字段。失败不抛异常：
        前台仍然可以用 Shopify 链接显示，这张图只是"缺备份"，之后可以补传。"""
        source = self.media_source_id
        folder, filename = self._backup_folder_and_name()
        # 同一个商品里指向同一张 Shopify 图片的另一行（典型情况：变体图片同时也是画廊里的
        # 一张）已经传过，就直接复用那份备份，不重复上传
        key = self._image_key(self.source_url)
        twin = self.search([
            ('id', '!=', self.id), ('product_tmpl_id', '=', self.product_tmpl_id.id),
            ('media_source_id', '=', source.id), ('backup_state', '=', 'ok'),
        ]).filtered(lambda r: self._image_key(r.source_url) == key)[:1]
        if twin and twin.backup_url and twin.payload_size == len(content):
            return {
                'backup_state': 'ok', 'backup_url': twin.backup_url, 'backup_ref': twin.backup_ref,
                'backup_size': len(content), 'backup_checked_at': fields.Datetime.now(),
                'cdn_error': False, 'storage': 'cdn', 'alist_target_path': twin.alist_target_path,
            }
        try:
            result = source.upload_media(
                folder, filename, io.BytesIO(content), size=len(content),
                content_type=self._guess_mime(filename))
            url = (result or {}).get('url')
            if not url:
                raise ValueError('已上传，但对象存储暂时还解析不出直链（索引没刷新），稍后点「补传备份」重试')
            # 传上去不算完：返回的直链要真的能打开、大小要对。否则等 Shopify 失效、
            # 切到这条链接时才发现是坏的就晚了（实测遇到过：图片源的 CDN 路径设置
            # 填错，文件在，但返回的直链少了目录，打开是 403）
            problem = self._check_backup_link(url, len(content))
            if problem:
                raise ValueError(
                    f'文件已上传到 {result.get("source_ref") or filename}，但返回的直链{problem}。'
                    f'请检查图片源的 CDN 设置（CDN 地址 / 要去掉的路径前缀）：{url.split("?")[0]}')
            return {
                'backup_state': 'ok', 'backup_url': url,
                'backup_ref': result.get('source_ref') or False,
                'backup_size': len(content), 'backup_checked_at': fields.Datetime.now(),
                'cdn_error': False, 'storage': 'cdn',
            }
        except Exception as e:
            _logger.warning('上传对象存储失败，先只用 Shopify 外链显示: %s (%s)', self.source_url, e)
            return {'backup_state': 'failed', 'cdn_error': str(e)[:250] or type(e).__name__,
                    'storage': 'link'}

    @api.model
    def _check_backup_link(self, url, expected_size):
        """刚上传的备份直链是否可用。返回问题描述；没问题返回 False。
        网络错误（探测不出结果）不算问题，留给以后的对账去发现。"""
        state, code, size = self._probe(url)
        if state == 'ok':
            if size and expected_size and size != expected_size:
                return f'大小不对（{size} 字节，原图 {expected_size} 字节）'
            return False
        if code:
            return f'打不开（HTTP {code}）'
        return False

    def _backup_folder_and_name(self):
        path = (self.alist_target_path or '').strip('/')
        if not path:
            path = f'shopify-products/{self.product_tmpl_id.id}_{self._guess_filename(self.source_url)}'
        folder, _sep, filename = path.rpartition('/')
        return folder or '/', filename

    # ---------------------------------------------------------------
    # 前台显示：media_picker 的 media.bind
    # ---------------------------------------------------------------
    def _bind_key(self):
        # 键用去掉 ?v= 的 URL：图片在 Shopify 更新过也能对上同一条 media.bind。
        # 变体图片和画廊里的同一张图要分开存：键里带上变体 id。
        key = self._image_key(self.source_url)
        return f'{key}#variant-{self.product_variant_id.id}' if self.product_variant_id else key

    def _write_bind(self, prefer, backup_url=None):
        """把前台显示链接写到 media.bind 上，返回要写到台账上的字段。

        prefer='shopify'：显示 Shopify 链接；如果 media_picker 的可信域名名单不认
        cdn.shopify.com，有备份就改显示备份，没有就报错。
        prefer='backup'：显示对象存储的 CDN 直链。
        """
        self.ensure_one()
        tmpl = self.product_tmpl_id
        backup_url = backup_url or self.backup_url
        Bind = self.env['media.bind']
        bind = self.bind_id or Bind.search([
            ('product_tmpl_id', '=', tmpl.id), ('shopify_media_id', '=', self._bind_key()),
        ], limit=1)
        item = {
            'shopify_media_id': self._bind_key(),
            'media_type': 'image',
            'sequence': self.sequence,
            'is_main': self.role == 'main' and not self.product_variant_id,
        }
        if self.product_variant_id:
            item['product_variant_id'] = self.product_variant_id.id

        order = ['shopify', 'backup'] if prefer == 'shopify' else ['backup', 'shopify']
        last_error = None
        for target in order:
            url = self.source_url if target == 'shopify' else backup_url
            if not url:
                continue
            # Shopify 链接不属于任何图片源；备份链接属于图片源（它的域名由图片源担保）
            source_id = self.media_source_id.id if target == 'backup' else False
            try:
                with self.env.cr.savepoint():
                    if bind:
                        vals = {'url': url, 'source_id': source_id,
                                'sequence': item['sequence'], 'is_main': item['is_main']}
                        if bind.url != url:
                            vals.update(health_status='unchecked', fail_count=0, last_checked=False)
                        bind.write(vals)
                    elif target == 'shopify':
                        # media_picker 给 Shopify 导入定义的接入点
                        res = tmpl.upsert_external_media_from_shopify([dict(item, url=url)])
                        bind = Bind.browse((res.get('created') or res.get('updated') or [])[:1])
                    else:
                        bind = Bind.create(dict(
                            item, url=url, source_id=source_id, res_model='product.template',
                            res_id=tmpl.id, res_field='gallery', product_tmpl_id=tmpl.id,
                            usage='gallery', source_ref=self.backup_ref or self._bind_key()))
                break
            except Exception as e:
                last_error = e
                _logger.info('显示链接写不进 media.bind（%s）: %s', target, e)
        else:
            raise ValueError(
                '外链写不进 media_picker：%s。如果提示域名不可信，请把 cdn.shopify.com 加到 '
                'media_picker 的可信域名（系统参数 media_picker.trusted_domains）里' % last_error)

        if not tmpl.use_external_media:
            tmpl.write({'use_external_media': True})
        if self.binary_image_id:
            # 以前本地模式存的附加图，现在已经改走外链：删掉，免得画廊里重复
            self.binary_image_id.unlink()
        return {'bind_id': bind.id, 'display_source': target}

    # ---------------------------------------------------------------
    # 下载 / 检查工具方法
    # ---------------------------------------------------------------
    @classmethod
    def _download(cls, url, original=False):
        if requests is None:
            raise RuntimeError('服务器缺少 requests 库，无法下载图片')
        resized = url if original else cls._shopify_resized_url(url)
        if resized != url:
            try:
                return cls._http_get_image(resized)
            except Exception:
                _logger.info('Shopify 缩略图下载失败，改下原图: %s', url, exc_info=True)
        return cls._http_get_image(url)

    @staticmethod
    def _shopify_resized_url(url):
        """Shopify CDN 图片加上 width/height 参数，按 1920px 以内等比缩放（小图不会被放大）。"""
        parts = urlparse(url)
        if parts.hostname not in SHOPIFY_CDN_HOSTS:
            return url
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k not in ('width', 'height', 'crop')]
        query += [('width', str(SHOPIFY_MAX_PX)), ('height', str(SHOPIFY_MAX_PX))]
        return urlunparse(parts._replace(query=urlencode(query)))

    @staticmethod
    def _http_get_image(url):
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        content_type = (resp.headers.get('Content-Type') or '').lower()
        if content_type and not content_type.startswith(('image/', 'application/octet-stream')):
            raise ValueError(f'下载到的不是图片（Content-Type: {content_type}）')
        return resp.content

    @staticmethod
    def _probe(url):
        """检查一条链接，返回 (状态, HTTP 状态码, 大小)。

        状态：'ok' 在；'gone' 明确不存在（404/410）；'unknown' 说不准
        （超时、连接失败、5xx、403……都可能是临时故障，不能当成失效）。
        """
        if requests is None or not url:
            return ('unknown', 0, 0)
        try:
            resp = requests.head(url, timeout=15, allow_redirects=True)
            if resp.status_code in (403, 405, 501):
                # 有些存储不支持 HEAD：改用只取 1 个字节的 GET
                resp = requests.get(url, timeout=15, stream=True, headers={'Range': 'bytes=0-0'})
                resp.close()
            code = int(resp.status_code or 0)
            size = 0
            content_range = resp.headers.get('Content-Range') or ''
            if '/' in content_range and content_range.rsplit('/', 1)[-1].isdigit():
                size = int(content_range.rsplit('/', 1)[-1])
            elif (resp.headers.get('Content-Length') or '').isdigit() and code == 200:
                size = int(resp.headers['Content-Length'])
        except Exception:
            return ('unknown', 0, 0)
        if 200 <= code < 300:
            return ('ok', code, size)
        if code in GONE_CODES:
            return ('gone', code, 0)
        return ('unknown', code, 0)

    @staticmethod
    def _fingerprint(content):
        """下载到的原图的指纹：大小、SHA-256、像素尺寸。对账时拿来和备份比。"""
        vals = {
            'payload_size': len(content),
            'payload_sha256': hashlib.sha256(content).hexdigest(),
            'width': 0, 'height': 0,
        }
        try:
            from PIL import Image
            with Image.open(io.BytesIO(content)) as img:
                vals['width'], vals['height'] = img.size
        except Exception:
            pass  # 不是能解码的图片：尺寸留 0，上传照常进行
        return vals

    @staticmethod
    def _guess_filename(url):
        name = url.split('/')[-1].split('?')[0]
        return name or 'image.jpg'

    @staticmethod
    def _guess_mime(filename):
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        return {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp',
                'gif': 'image/gif', 'avif': 'image/avif'}.get(ext, 'application/octet-stream')
