# -*- coding: utf-8 -*-
import logging
import time
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# 商品导入阶段：每处理这么多个商品提交一次（进度页面就能看到数字在涨）
PRODUCT_COMMIT_EVERY = 10
ACTIVE_STATES = ('queued', 'importing', 'syncing')
# 计算速度 / 预计剩余时间用的时间窗口
RATE_WINDOW = timedelta(minutes=5)


class ShopifyImportBatch(models.Model):
    """一次 Shopify CSV 导入 = 一个批次。

    商品导入和图片同步都在后台定时任务里执行，进度保存在这条记录上：
    关掉浏览器窗口也不影响，随时可以从「Shopify 导入 → 导入记录」回来查看。
    """
    _name = 'shopify.import.batch'
    _inherit = ['shopify.import.logic']
    _description = 'Shopify 导入批次'
    _order = 'id desc'

    name = fields.Char(string='批次', required=True, readonly=True)
    csv_file = fields.Binary(string='CSV 文件', attachment=True, readonly=True)
    csv_filename = fields.Char(string='文件名', readonly=True)
    media_source_id = fields.Many2one(
        'media.source', string='对象存储图片源', readonly=True,
        help='留空表示图片直接存成 Odoo 本地图片。')
    alist_upload_path_prefix = fields.Char(
        string='上传文件夹', default='shopify-products',
        help='图片源根目录下的文件夹。Alist 挂了多个存储时要带上存储的挂载路径，'
             '例如 b2/shopify-products。改完点「应用到未备份的图片」。')

    state = fields.Selection([
        ('queued', '排队中'),
        ('importing', '导入商品中'),
        ('syncing', '同步图片中'),
        ('done', '已完成'),
        ('done_errors', '已完成（有失败）'),
    ], string='状态', default='queued', required=True, readonly=True, index=True)
    paused = fields.Boolean(string='已暂停', readonly=True)
    status_message = fields.Char(string='当前进度', readonly=True)

    total_products = fields.Integer(string='商品总数', readonly=True)
    next_index = fields.Integer(string='已处理商品', readonly=True)
    created_count = fields.Integer(string='新建商品', readonly=True)
    updated_count = fields.Integer(string='更新商品', readonly=True)
    error_count = fields.Integer(string='失败商品', readonly=True)
    warning_count = fields.Integer(string='警告', readonly=True)
    image_queue_count = fields.Integer(string='本次排队图片', readonly=True)
    import_log = fields.Text(string='导入日志', readonly=True)

    started_at = fields.Datetime(string='开始时间', readonly=True)
    products_done_at = fields.Datetime(string='商品导入完成', readonly=True)
    images_started_at = fields.Datetime(string='图片同步开始', readonly=True)
    finished_at = fields.Datetime(string='全部完成', readonly=True)

    queue_ids = fields.One2many('shopify.image.queue', 'batch_id', string='图片')

    # ---- 进度（实时计算，不落库）----
    img_total = fields.Integer(string='图片总数', compute='_compute_progress')
    img_pending = fields.Integer(string='待处理', compute='_compute_progress')
    img_done = fields.Integer(string='已完成', compute='_compute_progress')
    img_cdn = fields.Integer(string='已备份到对象存储', compute='_compute_progress')
    img_binary = fields.Integer(string='本地图片', compute='_compute_progress')
    img_fallback = fields.Integer(string='缺备份', compute='_compute_progress')
    img_display_shopify = fields.Integer(string='显示 Shopify 链接', compute='_compute_progress')
    img_display_backup = fields.Integer(string='显示对象存储链接', compute='_compute_progress')
    img_source_gone = fields.Integer(string='Shopify 已失效', compute='_compute_progress')
    img_lost = fields.Integer(string='已失效且无备份', compute='_compute_progress')
    img_error = fields.Integer(string='失败', compute='_compute_progress')
    product_progress = fields.Float(string='商品进度', compute='_compute_progress')
    image_progress = fields.Float(string='图片进度', compute='_compute_progress')
    progress = fields.Float(string='总进度', compute='_compute_progress')
    speed_text = fields.Char(string='速度', compute='_compute_progress')
    eta_text = fields.Char(string='预计剩余', compute='_compute_progress')
    last_error = fields.Char(string='最近一次失败', compute='_compute_progress')

    @api.depends('state', 'next_index', 'total_products', 'queue_ids.state')
    def _compute_progress(self):
        Queue = self.env['shopify.image.queue']
        stats = {}
        ids = [b.id for b in self if isinstance(b.id, int)]
        if ids:
            for batch, state, storage, count in Queue._read_group(
                    [('batch_id', 'in', ids)], ['batch_id', 'state', 'storage'], ['__count']):
                s = stats.setdefault(batch.id, {})
                s[state] = s.get(state, 0) + count
                if state == 'done':
                    s[storage or 'binary'] = s.get(storage or 'binary', 0) + count
            for batch, count in Queue._read_group(
                    [('batch_id', 'in', ids), ('cdn_error', '!=', False)], ['batch_id'], ['__count']):
                stats.setdefault(batch.id, {})['fallback'] = count
            # 旧版本留下的"上传失败后回退成本地图片"的记录：既是本地图片又缺备份，
            # 进度条里只算进"缺备份"，不重复算进"本地图片"
            for batch, count in Queue._read_group(
                    [('batch_id', 'in', ids), ('cdn_error', '!=', False), ('storage', '=', 'binary'),
                     ('state', '=', 'done')], ['batch_id'], ['__count']):
                stats.setdefault(batch.id, {})['legacy_fallback'] = count
            for batch, display, count in Queue._read_group(
                    [('batch_id', 'in', ids), ('display_source', '!=', False)],
                    ['batch_id', 'display_source'], ['__count']):
                stats.setdefault(batch.id, {})[f'display_{display}'] = count
            for batch, source_state, verdict, count in Queue._read_group(
                    [('batch_id', 'in', ids), ('source_state', '=', 'gone')],
                    ['batch_id', 'source_state', 'verdict'], ['__count']):
                s = stats.setdefault(batch.id, {})
                s['gone'] = s.get('gone', 0) + count
                if verdict == 'lost':
                    s['lost'] = s.get('lost', 0) + count
        now = fields.Datetime.now()
        for batch in self:
            s = stats.get(batch.id, {})
            done, error, pending = s.get('done', 0), s.get('error', 0), s.get('pending', 0)
            total = done + error + pending
            batch.img_total = total
            batch.img_done = done
            batch.img_error = error
            batch.img_pending = pending
            batch.img_cdn = s.get('cdn', 0)
            batch.img_binary = s.get('binary', 0) - s.get('legacy_fallback', 0)
            batch.img_fallback = s.get('fallback', 0)
            batch.img_display_shopify = s.get('display_shopify', 0)
            batch.img_display_backup = s.get('display_backup', 0)
            batch.img_source_gone = s.get('gone', 0)
            batch.img_lost = s.get('lost', 0)
            batch.product_progress = (
                100.0 * batch.next_index / batch.total_products if batch.total_products
                else (100.0 if batch.state not in ('queued', 'importing') else 0.0))
            batch.image_progress = 100.0 * (done + error) / total if total else (
                100.0 if batch.state in ('done', 'done_errors') else 0.0)
            # 总进度：商品导入占 20%，图片同步占 80%（图片耗时远多于商品）
            batch.progress = round(0.2 * batch.product_progress + 0.8 * batch.image_progress, 1)

            speed, eta = False, False
            if batch.state == 'importing' and batch.started_at and batch.next_index:
                elapsed = max((now - batch.started_at).total_seconds(), 1)
                rate = batch.next_index / elapsed
                speed = f'{rate * 60:.0f} 个商品/分钟'
                eta = self._format_duration((batch.total_products - batch.next_index) / rate)
            elif batch.state == 'syncing' and batch.images_started_at:
                rate = batch._recent_image_rate(now)
                if batch.paused:
                    eta = '已暂停'
                elif rate:
                    per_min = rate * 60
                    speed = f'{per_min:.0f} 张/分钟' if per_min >= 10 else f'{per_min:.1f} 张/分钟'
                    eta = self._format_duration(pending / rate)
                else:
                    # 最近几分钟没有处理任何图片：不拿全程平均值硬算（会严重失真）
                    eta = '暂未处理（等待后台任务）'
            batch.speed_text = speed
            batch.eta_text = eta

            last = Queue.search(
                [('batch_id', '=', batch.id), '|', ('state', '=', 'error'), ('cdn_error', '!=', False)],
                order='write_date desc, id desc', limit=1) if isinstance(batch.id, int) else Queue
            batch.last_error = (last.error_message or last.cdn_error) if last else False

    def _recent_image_rate(self, now):
        """最近几分钟的实际处理速度（张/秒）。

        不能只用"开始同步到现在"的平均值：中间暂停过、服务器重启过、机器休眠过，
        平均速度会被拉得很低，预计剩余时间就会离谱（实测出现过"约 4 小时"，
        实际只要十几分钟）。最近窗口里没有处理记录时返回 0。
        """
        self.ensure_one()
        since = now - RATE_WINDOW
        recent = self.env['shopify.image.queue'].search_count([
            ('batch_id', '=', self.id), ('processed_at', '>=', since),
        ])
        if not recent:
            return 0.0
        window_start = max(since, self.images_started_at)
        return recent / max((now - window_start).total_seconds(), 1)

    @staticmethod
    def _format_duration(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return f'约 {max(seconds, 1)} 秒'
        if seconds < 3600:
            return f'约 {round(seconds / 60)} 分钟'
        return f'约 {seconds // 3600} 小时 {round(seconds % 3600 / 60)} 分钟'

    # =================================================================
    # 后台执行
    # =================================================================
    @api.model
    def _cron_run(self):
        """定时任务入口：按顺序执行排队中 / 导入中的批次（商品导入阶段）。"""
        IrCron = self.env['ir.cron']
        Queue = self.env['shopify.image.queue']
        in_cron = bool(self.env.context.get('cron_id'))
        deadline = time.monotonic() + Queue._time_budget(in_cron)
        if in_cron:
            job_deadline = Queue._cron_job_deadline()
            if job_deadline is not None:
                deadline = min(deadline, job_deadline)
        batches = self.search([('state', 'in', ('queued', 'importing')), ('paused', '=', False)],
                              order='id')
        if in_cron:
            IrCron._commit_progress(remaining=len(batches))
        for batch in batches:
            if not batch._run_products(deadline, in_cron):
                break  # 时间预算用完，剩下的下一轮接着做
            if in_cron:
                IrCron._commit_progress(1)

    def _run_products(self, deadline=None, in_cron=False):
        """导入本批次的商品，可以分多次调用（从 next_index 接着做）。

        返回 True 表示商品已经全部导入完。
        """
        self.ensure_one()
        IrCron = self.env['ir.cron']
        groups = list(self._group_rows_by_handle(self._read_csv_rows(self.csv_file)).items())
        if self.state == 'queued':
            self.write({
                'state': 'importing', 'total_products': len(groups),
                'started_at': fields.Datetime.now(), 'status_message': '开始导入商品',
            })
        index = self.next_index
        counters = dict(created_count=self.created_count, updated_count=self.updated_count,
                        error_count=self.error_count, warning_count=self.warning_count,
                        image_queue_count=self.image_queue_count)
        log_lines = []

        def flush(final=False):
            vals = dict(counters, next_index=index, status_message=(
                f'正在导入商品 {index}/{len(groups)}' if not final else f'商品导入完成（{len(groups)} 个）'))
            if log_lines:
                vals['import_log'] = '\n'.join(filter(None, [self.import_log] + log_lines))
                log_lines.clear()
            self.write(vals)
            if in_cron:
                IrCron._commit_progress(0)

        processed = 0
        while index < len(groups):
            if deadline is not None and processed and time.monotonic() >= deadline:
                flush()
                return False
            handle, group_rows = groups[index]
            try:
                with self.env.cr.savepoint():
                    tmpl, is_new, n_images, warnings = self._import_one_product(handle, group_rows)
                counters['image_queue_count'] += n_images
                counters['created_count' if is_new else 'updated_count'] += 1
                counters['warning_count'] += len(warnings)
                log_lines.append(
                    f"[{'新建' if is_new else '更新'}] {handle} -> {tmpl.name}（排队图片 {n_images} 张）")
                log_lines += [f'[警告] {handle}: {w}' for w in warnings]
            except Exception as e:
                counters['error_count'] += 1
                _logger.exception('导入商品失败: handle=%s', handle)
                log_lines.append(f'[失败] {handle}: {e}')
            index += 1
            processed += 1
            if processed % PRODUCT_COMMIT_EVERY == 0:
                flush()

        flush(final=True)
        has_images = bool(self.env['shopify.image.queue'].search_count(
            [('batch_id', '=', self.id), ('state', '=', 'pending')], limit=1))
        now = fields.Datetime.now()
        self.write({'products_done_at': now, 'images_started_at': now, 'state': 'syncing'})
        if has_images:
            self._trigger_image_sync()
        else:
            self._refresh_image_state()
        return True

    def _refresh_image_state(self):
        """图片都处理完了的批次标记为完成。"""
        for batch in self.filtered(lambda b: b.state == 'syncing'):
            batch.invalidate_recordset(['img_pending', 'img_error'])
            if batch.img_pending:
                continue
            batch.write({
                'state': 'done_errors' if (batch.img_error or batch.error_count) else 'done',
                'finished_at': fields.Datetime.now(),
                'status_message': '全部完成' if not batch.img_error
                else f'全部完成，其中 {batch.img_error} 张图片失败（可点「重试失败图片」）',
            })

    def _set_status(self, message):
        for batch in self:
            batch.status_message = message

    def _trigger_image_sync(self):
        cron = self.env.ref('shopify_csv_import.ir_cron_shopify_image_sync', raise_if_not_found=False)
        if cron and cron.active:
            cron._trigger()

    def _trigger_batch_run(self):
        cron = self.env.ref('shopify_csv_import.ir_cron_shopify_import_batch', raise_if_not_found=False)
        if cron and cron.active:
            cron._trigger()

    # =================================================================
    # 页面按钮
    # =================================================================
    def action_pause(self):
        self.write({'paused': True, 'status_message': '已暂停（点「继续」恢复）'})

    def action_resume(self):
        self.write({'paused': False, 'status_message': '已恢复，等待后台任务处理'})
        self._trigger_batch_run()
        self._trigger_image_sync()

    def action_run_now(self):
        """不等定时任务，在当前请求里马上处理一段（受请求超时时间控制）。"""
        self.ensure_one()
        Queue = self.env['shopify.image.queue']
        if self.state in ('queued', 'importing'):
            self._run_products(deadline=time.monotonic() + Queue._time_budget(False))
            processed_msg = f'已导入商品 {self.next_index}/{self.total_products}'
        else:
            n = Queue._cron_process_pending(limit=50, batch=self)
            processed_msg = f'本次处理 {n} 张图片'
        return self._notify(f'{processed_msg}；剩下的会在后台继续，可以关闭这个页面。')

    def action_retry_errors(self):
        rows = self.queue_ids.filtered(lambda q: q.state == 'error')
        rows.write({'state': 'pending', 'error_message': False})
        self._reopen_for_images(f'{len(rows)} 张失败图片已重新排队')
        return self._notify(f'{len(rows)} 张失败图片已重新排队，后台会继续处理。')

    def action_retry_cdn_fallback(self):
        """中继：把缺备份的图片补传到对象存储（改好图片源配置后用）。
        Shopify 源还在就从 Shopify 拉原图，已经失效的主图改用本地那份。"""
        rows = self.queue_ids.filtered(
            lambda q: q.media_source_id and q.backup_state != 'ok' and q.state != 'pending')
        rows.write({'job': 'backup', 'state': 'pending', 'error_message': False})
        self._reopen_for_images(f'{len(rows)} 张缺备份的图片重新排队上传对象存储')
        return self._notify(f'{len(rows)} 张图片已排队补传到对象存储。')

    def action_apply_upload_folder(self):
        """上传文件夹填错时（比如 Alist 里没有对应的存储）：在批次上改好文件夹后，
        把还没备份成功的图片的目标路径都换成新文件夹。已经备份好的不动。"""
        self.ensure_one()
        folder = (self.alist_upload_path_prefix or '').strip('/')
        if not folder:
            return self._notify('请先填写上传文件夹。', kind='warning')
        rows = self.queue_ids.filtered(lambda q: q.media_source_id and q.backup_state != 'ok')
        for row in rows:
            row.alist_target_path = (
                f'{folder}/{row.product_tmpl_id.id}_{row._guess_filename(row.source_url)}')
        self.alist_upload_path_prefix = folder
        return self._notify(f'{len(rows)} 张未备份的图片已改为上传到 {folder}/。')

    def action_verify(self):
        """对账：逐张检查 Shopify 源和对象存储备份，在后台进行。"""
        rows = self.queue_ids.filtered(lambda q: q.state != 'pending' and q.media_source_id)
        rows.write({'job': 'verify', 'state': 'pending', 'error_message': False})
        self._reopen_for_images(f'{len(rows)} 张图片排队对账')
        return self._notify(f'{len(rows)} 张图片已排队对账，结果看「图片台账」的「对账结论」。')

    def action_failover(self):
        """把 Shopify 链接已经失效的图片，立刻换成对象存储的备份链接。"""
        switched = self.env['shopify.image.queue']._cron_failover()
        return self._notify(
            f'已把 {switched} 张 Shopify 失效的图片切到对象存储链接。' if switched
            else '没有需要切换的图片（Shopify 链接都还在，或失效的图片没有备份）。')

    def _reopen_for_images(self, message):
        for batch in self:
            # 重新排队就意味着要继续跑：之前暂停过（手动或连续失败自动暂停）的批次一并恢复
            vals = {'status_message': message, 'paused': False}
            if batch.state in ('done', 'done_errors'):
                vals.update(state='syncing', finished_at=False,
                            images_started_at=fields.Datetime.now())
            batch.write(vals)
        self._trigger_image_sync()

    def action_open_import_wizard(self):
        """导入记录列表上的「导入 Shopify CSV」按钮：弹出上传窗口。

        不能加 @api.model：列表按钮被点击时，Odoo 会把当前勾选的记录 id（没勾选就是
        空列表）当作第一个参数传进来，它要落到 self 上。
        """
        action = self.env['ir.actions.act_window']._for_xml_id(
            'shopify_csv_import.action_shopify_import_wizard')
        action['context'] = {}
        return action

    def action_view_images(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': f'{self.name} 的图片',
            'res_model': 'shopify.image.queue',
            'view_mode': 'list,form',
            'domain': [('batch_id', '=', self.id)],
            'context': dict(self.env.context.get('queue_filter') or {}),
        }

    def _notify(self, message, kind='success'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': self.name, 'message': message, 'type': kind, 'sticky': False,
                       'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

    @api.model
    def get_progress_snapshot(self, batch_id):
        """进度面板轮询用：一次性返回页面上要显示的所有数字。

        公开方法（前端 RPC 不能调用下划线开头的方法）；内部用 read()，照样受访问权限控制。
        """
        batch = self.browse(batch_id).exists()
        if not batch:
            return {}
        fnames = ['name', 'state', 'paused', 'status_message', 'total_products', 'next_index',
                  'created_count', 'updated_count', 'error_count', 'warning_count',
                  'img_total', 'img_pending', 'img_done', 'img_cdn', 'img_binary', 'img_fallback',
                  'img_error', 'img_display_shopify', 'img_display_backup', 'img_source_gone',
                  'img_lost', 'product_progress', 'image_progress', 'progress',
                  'speed_text', 'eta_text', 'last_error', 'media_source_id']
        data = batch.read(fnames)[0]
        data['state_label'] = dict(self._fields['state'].selection).get(batch.state)
        data['media_source_name'] = batch.media_source_id.display_name or False
        data['active'] = batch.state in ACTIVE_STATES
        # 定时任务被停用时，批次永远不会往前走：页面上要明确提示，而不是显示一个预计时间
        warnings = []
        for xmlid, phase, states in (
                ('ir_cron_shopify_import_batch', '商品导入', ('queued', 'importing')),
                ('ir_cron_shopify_image_sync', '图片同步', ('syncing',))):
            cron = self.env.ref(f'shopify_csv_import.{xmlid}', raise_if_not_found=False)
            if batch.state in states and (not cron or not cron.sudo().active):
                warnings.append(f'「Shopify {phase}」定时任务已停用，后台不会处理这个批次。'
                                f'请到 设置 → 技术 → 计划任务 里启用它，或点「立即处理一段」手动推进。')
        data['cron_warning'] = ' '.join(warnings) or False
        return data
