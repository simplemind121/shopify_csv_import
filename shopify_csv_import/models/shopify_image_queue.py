# -*- coding: utf-8 -*-
import base64
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from odoo import api, fields, models
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Odoo 只保存最大 1920px 的图片（image_1920），Shopify CDN 支持按参数缩放，
# 直接下缩好的版本：原图常见 1~3MB / 1200~2400 万像素，没必要整张下载。
SHOPIFY_CDN_HOSTS = ('cdn.shopify.com',)
SHOPIFY_MAX_PX = 1920
# 单批图片同步最多占用"cron / 请求超时时间"的这个比例，剩下的留给最后一张图
# （下载超时 30s + Alist 上传超时 60s）收尾，保证不会被 Odoo 强杀。
TIME_BUDGET_RATIO = 0.5
# 同时下载的图片数。下载是纯网络等待（实测 3 秒/张），并行下载能把吞吐量提高好几倍；
# 线程里只做 HTTP 请求，所有数据库写入仍然在主线程里逐张进行。
DOWNLOAD_WORKERS = 8

try:
    import requests
except ImportError:  # pragma: no cover - requests 是 Odoo 标准依赖，正常都有
    requests = None


class ShopifyImageQueue(models.Model):
    _name = 'shopify.image.queue'
    _description = 'Shopify 商品图片同步队列'
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
    source_url = fields.Char(string='Shopify 原始图片URL', required=True)

    # 走 CDN 模式时使用：选了 media_source_id 就会尝试上传到 Alist，
    # 失败或没选就直接回退成 Odoo 本地二进制图片。
    media_source_id = fields.Many2one(
        'product.media.source', string='Alist 图片源',
        help='对应 media_picker 模块里已经配置好的 Alist 连接（product.media.source）。'
             '留空则该图片直接存成 Odoo 本地二进制图片。')
    alist_target_path = fields.Char(
        string='Alist 目标路径',
        help='上传到 Alist 后的目标路径，例如 /b2/shopify-products/123_foo.jpg。')

    state = fields.Selection([
        ('pending', '待处理'),
        ('done', '已完成'),
        ('error', '失败'),
    ], string='状态', default='pending', index=True)
    error_message = fields.Char(string='错误信息')

    # ---------------------------------------------------------------
    # cron 入口
    # ---------------------------------------------------------------
    @api.model
    def _cron_process_pending(self, limit=30):
        """处理一批待同步图片，返回本次处理的张数。

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

        records = self.search([('state', '=', 'pending')], limit=limit, order='id')
        if in_cron:
            IrCron._commit_progress(remaining=self.search_count([('state', '=', 'pending')]))

        processed = 0
        for start in range(0, len(records), DOWNLOAD_WORKERS):
            if processed and time.monotonic() >= deadline:
                break
            chunk = records[start:start + DOWNLOAD_WORKERS]
            downloads = self._prefetch(chunk)
            for rec in chunk:
                rec._process(download=downloads.get(rec.id))
                processed += 1
                if in_cron:
                    IrCron._commit_progress(1)
        return processed

    @api.model
    def _prefetch(self, records):
        """并行下载一组图片，返回 {记录id: 图片字节 或 下载时抛出的异常}。"""
        if len(records) <= 1:
            return {}
        urls = {rec.id: rec.source_url for rec in records}

        def fetch(url):
            try:
                return self._download(url)
            except Exception as e:  # 在 _process 里统一记成失败
                return e

        with ThreadPoolExecutor(max_workers=min(DOWNLOAD_WORKERS, len(urls))) as pool:
            results = pool.map(fetch, urls.values())
            return dict(zip(urls.keys(), results))

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

    def action_retry(self):
        # 只重试选中的这几条（以前是按 id 顺序处理"任意 N 条待处理"，
        # 队列里还有别的待处理记录时，选中的那条可能根本没被处理）
        self.write({'state': 'pending', 'error_message': False})
        for rec in self:
            rec._process()
        return True

    # ---------------------------------------------------------------
    # 核心处理逻辑
    # ---------------------------------------------------------------
    def _process(self, download=None):
        """处理一张图。download 是已经预先下载好的结果（字节或异常），没有就现下。"""
        self.ensure_one()
        try:
            if isinstance(download, Exception):
                raise download
            # savepoint：写图片时如果触发数据库错误（例如图片字段校验失败），
            # 只回滚这一张图，事务还能继续把 state=error 写进去、处理下一张。
            with self.env.cr.savepoint():
                self._process_one(download)
            self.write({'state': 'done', 'error_message': False})
        except Exception as e:
            _logger.exception('图片同步失败: %s', self.source_url)
            self.write({'state': 'error', 'error_message': str(e)[:250]})

    def _process_one(self, content=None):
        if content is None:
            content = self._download(self.source_url)
        if not content:
            raise ValueError('下载失败或返回空内容')

        cdn_url = False
        if self.media_source_id:
            try:
                cdn_url = self._upload_and_resolve(content)
            except Exception:
                # Alist 上传/解析失败：记录日志，直接走本地二进制兜底，
                # 不让图片同步整体失败。
                _logger.info(
                    'Alist 上传/解析失败，改用本地二进制存储: %s',
                    self.source_url, exc_info=True,
                )
                cdn_url = False

        if cdn_url:
            self._save_via_media_bind(cdn_url)
        else:
            self._save_binary(content)

    # ---------------------------------------------------------------
    # 方案 A：本地二进制兜底（不依赖 media_picker）
    # ---------------------------------------------------------------
    def _save_binary(self, content):
        b64 = base64.b64encode(content)
        if self.product_variant_id:
            # 变体专属图片只挂到该变体上，不再额外塞进商品公共画廊
            self.product_variant_id.image_1920 = b64
        elif self.role == 'main':
            self.product_tmpl_id.image_1920 = b64
        else:
            self.env['product.image'].create({
                'product_tmpl_id': self.product_tmpl_id.id,
                'name': self.product_tmpl_id.name,
                'image_1920': b64,
            })

    # ---------------------------------------------------------------
    # 方案 B：转存 Alist，走 media_picker 的 media.bind 外链画廊
    # （即 product.template.upsert_external_media_from_shopify，
    # 是 media_picker 自己文档里指定的 Shopify 导入接入点）
    # ---------------------------------------------------------------
    def _save_via_media_bind(self, cdn_url):
        media_item = {
            # 用原始 Shopify 图片 URL 当去重键：同一张图重复导入不会建重复记录，
            # 只会更新已有的 media.bind 行。
            # 变体图片和画廊里的同一张图要分开存：键里带上变体 id，
            # 否则会覆盖掉画廊那条 media.bind（把它变成变体专属、取消主图）。
            'shopify_media_id': (
                f'{self.source_url}#variant-{self.product_variant_id.id}'
                if self.product_variant_id else self.source_url),
            'url': cdn_url,
            'media_type': 'image',
            'sequence': self.sequence,
            'is_main': self.role == 'main',
        }
        if self.product_variant_id:
            media_item['product_variant_id'] = self.product_variant_id.id

        self.product_tmpl_id.upsert_external_media_from_shopify([media_item])

        if not self.product_tmpl_id.use_external_media:
            self.product_tmpl_id.write({'use_external_media': True})

    def _upload_and_resolve(self, content):
        """上传到 Alist，再用 media_picker 自带的 get_file 解析出最终可信直链。

        没有复用 media_picker 的域名改写逻辑（那是 media.source v2 的功能），
        而是走跟 media_picker 的 product.media.source（v1，产品图片专用连接）
        完全一样的路径：调用 Alist 的 /api/fs/get 拿 raw_url，再用
        source._check_domain_trusted() 校验，这跟你后台"选文件挂到商品"那条
        路径用的是同一套代码、同一份信任逻辑。
        """
        source = self.media_source_id
        path = self.alist_target_path or self._default_alist_path()
        self._alist_put(source, path, content)

        # 复用 media_picker 自己的、已经在生产验证过的解析逻辑
        from odoo.addons.media_picker.models import pem_alist_client as alist_client
        data = alist_client.get_file(source, path)
        raw_url = data.get('raw_url')
        if not raw_url:
            raise ValueError('Alist 未返回 raw_url')

        hostname = urlparse(raw_url).hostname
        if not source._check_domain_trusted(hostname):
            raise ValueError(
                f'解析出的域名 {hostname} 不在该 Alist 图片源的可信域名列表里，'
                f'请检查 product.media.source 的 trusted_domains 配置')
        return raw_url

    def _default_alist_path(self):
        filename = self._guess_filename(self.source_url)
        return f'/b2/shopify-products/{self.product_tmpl_id.id}_{filename}'

    # ---------------------------------------------------------------
    # 下载 / 上传工具方法
    # ---------------------------------------------------------------
    @classmethod
    def _download(cls, url):
        if requests is None:
            raise RuntimeError('服务器缺少 requests 库，无法下载图片')
        resized = cls._shopify_resized_url(url)
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
    def _guess_filename(url):
        name = url.split('/')[-1].split('?')[0]
        return name or 'image.jpg'

    @staticmethod
    def _alist_put(source, path, content):
        """把字节流 PUT 到 Alist（v3 的 /api/fs/put）。

        media_picker 自带的 pem_alist_client.py 只有 list_dir/get_file
        （只读，供"挑选已存在文件"用），没有上传，这是本模块唯一需要
        自己实现网络调用的地方。如果你实际部署的 Alist 版本上传接口不一样，
        改这一个方法就行，其它代码不用动。
        """
        if requests is None:
            raise RuntimeError('服务器缺少 requests 库，无法上传图片')
        if not source.alist_url:
            raise ValueError('这个 Alist 图片源没有配置 alist_url')

        resp = requests.put(
            f"{source.alist_url.rstrip('/')}/api/fs/put",
            headers={
                'Authorization': source.alist_token or '',
                'File-Path': requests.utils.quote(path, safe=''),
                'Content-Type': 'application/octet-stream',
                'As-Task': 'false',
            },
            data=content,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 200:
            raise ValueError(f'Alist 上传失败: {data}')
