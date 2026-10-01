# -*- coding: utf-8 -*-
import base64
import contextlib
from unittest.mock import call, patch
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import ShopifyImportCase, fake_response, png_bytes

QUEUE_MODULE = 'odoo.addons.shopify_csv_import.models.shopify_image_queue'


def strip_resize(url):
    """把 _download 自动加上的 Shopify 缩放参数去掉，方便按原始 URL 设置 mock。"""
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in ('width', 'height')]
    return urlunparse(parts._replace(query=urlencode(query)))


@tagged('post_install', '-at_install', 'shopify_csv_import')
class TestShopifyImageQueue(ShopifyImportCase):

    def setUp(self):
        super().setUp()
        self.png = png_bytes()

    def _mock_get(self, url_map=None, default=None):
        url_map = url_map or {}
        default = default or fake_response(self.png)
        return patch(f'{QUEUE_MODULE}.requests.get',
                     side_effect=lambda url, **kw: url_map.get(strip_resize(url), default))

    def test_01_binary_fallback(self):
        self._run_import()
        with self._mock_get():
            processed = self.Queue._cron_process_pending(limit=100)
        self.assertEqual(processed, 5)
        self.assertEqual(set(self.Queue.search([]).mapped('state')), {'done'})

        mug = self._tmpl('sci-test-mug')
        self.assertTrue(mug.image_1920, '主图应写到 image_1920')
        self.assertEqual(len(mug.product_template_image_ids), 1, '附加图应写成 product.image')

        tee = self._tmpl('sci-test-tee')
        blue_m = self._variant(tee, {'SCI Color': 'Blue', 'SCI Size': 'M'})
        self.assertTrue(blue_m.image_variant_1920, '变体图片应挂到对应变体')
        # 变体专属图片不再额外塞进公共画廊：tee 只有 1 张附加公共图
        self.assertEqual(len(tee.product_template_image_ids), 1)

    def test_02_cron_respects_limit(self):
        self._run_import()
        with self._mock_get():
            self.assertEqual(self.Queue._cron_process_pending(limit=2), 2)
        self.assertEqual(self.Queue.search_count([('state', '=', 'pending')]), 3)

    @mute_logger(QUEUE_MODULE)
    def test_03_download_error_marks_record(self):
        self._run_import()
        bad_url = 'https://cdn.shopify.com/s/files/1/0000/0001/products/mug-2.jpg?v=1'
        with self._mock_get({bad_url: fake_response(status=404)}):
            self.Queue._cron_process_pending(limit=100)
        bad = self.Queue.search([('source_url', '=', bad_url)])
        self.assertEqual(bad.state, 'error')
        self.assertIn('404', bad.error_message)
        self.assertEqual(self.Queue.search_count([('state', '=', 'done')]), 4)

    @mute_logger(QUEUE_MODULE)
    def test_04_non_image_content_rejected(self):
        self._run_import()
        with self._mock_get(default=fake_response(b'<html>expired</html>', 'text/html')):
            self.Queue._cron_process_pending(limit=1)
        rec = self.Queue.search([('state', '=', 'error')])
        self.assertEqual(len(rec), 1)
        self.assertIn('text/html', rec.error_message)

    @mute_logger(QUEUE_MODULE)
    def test_05_corrupt_image_does_not_poison_batch(self):
        """图片字节损坏会在写 image 字段时报错：只让这一条失败，后面的照常处理。"""
        self._run_import()
        first = self.Queue.search([], order='id', limit=1)
        with self._mock_get({first.source_url: fake_response(b'not really a png', 'image/png')}):
            self.Queue._cron_process_pending(limit=100)
        self.assertEqual(first.state, 'error')
        self.assertEqual(self.Queue.search_count([('state', '=', 'done')]), 4)

    def test_06_retry_requeues_selected_record_only(self):
        """「重新拉取原图」只把选中的图片重新排队（后台处理），别的不动。"""
        self._run_import()
        rows = self.Queue.search([], order='id')
        rows.write({'state': 'done'})
        target = rows[-1]
        target.write({'state': 'error', 'error_message': 'boom', 'job': 'verify'})
        target.action_retry()
        self.assertEqual((target.state, target.job), ('pending', 'sync'))
        self.assertFalse(target.error_message)
        self.assertEqual(set((rows - target).mapped('state')), {'done'})
        with self._mock_get() as get:
            self.assertEqual(self.Queue._cron_process_pending(limit=30), 1)
        self.assertEqual(target.state, 'done')
        self.assertEqual(get.call_count, 1)

    # ------------------------------------------------------------------
    # 对象存储备份 + Shopify 链接显示（media_picker 3.8 的 media.source / media.bind）
    # ------------------------------------------------------------------
    def _sync_mug(self, fail_upload=None, **wizard_vals):
        source = self._media_source()
        self._run_import(media_source_id=source.id, **wizard_vals)
        mug = self._tmpl('sci-test-mug')
        rows = self.Queue.search([('product_tmpl_id', '=', mug.id)], order='sequence')
        # 这组测试只关心杯子的两张图；其它商品的图片标成已完成，免得后面跑定时任务时被顺带处理
        (self.Queue.search([]) - rows).write({'state': 'done'})
        with self._mock_get() as get, self._mock_upload(fail=fail_upload):
            for rec in rows:
                rec._process()
        return source, mug, rows, get

    def _binds(self, tmpl):
        return self.env['media.bind'].search([('product_tmpl_id', '=', tmpl.id)], order='sequence, id')

    def test_07_backup_to_object_storage_display_shopify(self):
        """所有图上传对象存储；前台显示先写 Shopify 链接；非主图不存本地。"""
        source, mug, rows, get = self._sync_mug()
        self.assertEqual(set(rows.mapped('state')), {'done'}, rows.mapped('error_message'))

        # 下的是原图（没有加 1920 缩放参数），每张只下一次、只传一次
        self.assertEqual([c.args[0] for c in get.call_args_list], rows.mapped('source_url'))
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(self.uploads[0]['folder'], 'shopify-products')
        self.assertEqual(self.uploads[0]['filename'], f'{mug.id}_mug-1.jpg')
        self.assertEqual(self.uploads[0]['bytes'], self.png)
        self.assertEqual(self.uploads[0]['size'], len(self.png))

        main, extra = rows
        self.assertEqual(set(rows.mapped('backup_state')), {'ok'})
        self.assertEqual(main.backup_url, f'https://media.example.com/shopify-products/{mug.id}_mug-1.jpg')
        self.assertEqual(set(rows.mapped('storage')), {'cdn'})
        self.assertEqual(set(rows.mapped('display_source')), {'shopify'})
        self.assertEqual(set(rows.mapped('verdict')), {'ok'})
        self.assertEqual(main.payload_size, len(self.png))
        self.assertEqual((main.width, main.height), (4, 4))
        self.assertEqual(len(main.payload_sha256), 64)

        binds = self._binds(mug)
        self.assertEqual(len(binds), 2)
        self.assertEqual(binds.mapped('url'), rows.mapped('source_url'), '前台先用 Shopify 链接')
        self.assertEqual(binds.mapped('is_main'), [True, False])
        self.assertFalse(binds.source_id, 'Shopify 链接不属于任何图片源')
        self.assertEqual(rows.bind_id, binds)
        # 去重键忽略 ?v=，图片在 Shopify 更新过也能对上同一条 media.bind
        self.assertEqual(binds[0].shopify_media_id, self.Queue._image_key(main.source_url))
        self.assertTrue(mug.use_external_media)
        self.assertFalse(mug.product_template_image_ids, '非主图不存本地')

        # 再同步一次：不会多出 media.bind
        with self._mock_get(), self._mock_upload():
            rows.write({'state': 'pending'})
            for rec in rows:
                rec._process()
        self.assertEqual(len(self._binds(mug)), 2)

    def test_08_upload_failure_still_displays_via_shopify(self):
        """对象存储传不上去：图片照样能显示（Shopify 链接），台账标成缺备份、写明原因。"""
        _source, mug, rows, _get = self._sync_mug(fail_upload='403 permission denied')
        self.assertEqual(set(rows.mapped('state')), {'done'})
        self.assertEqual(set(rows.mapped('backup_state')), {'failed'})
        self.assertEqual(set(rows.mapped('storage')), {'link'})
        self.assertEqual(set(rows.mapped('verdict')), {'no_backup'})
        self.assertIn('403', rows[0].cdn_error)
        self.assertEqual(self._binds(mug).mapped('url'), rows.mapped('source_url'))
        self.assertFalse(mug.product_template_image_ids, '缺备份也不再回退存本地')

    def test_08b_uploaded_but_link_unusable_counts_as_no_backup(self):
        """文件传上去了，但图片源返回的直链打不开（CDN 设置填错）：不能记成"已备份"。"""
        source = self._media_source()
        self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        main = self.Queue.search([('product_tmpl_id', '=', mug.id), ('role', '=', 'main')])
        with self._mock_get(), self._mock_upload(link_problem='打不开（HTTP 403）'):
            main._process()
        self.assertEqual((main.state, main.backup_state, main.verdict), ('done', 'failed', 'no_backup'))
        self.assertIn('HTTP 403', main.cdn_error)
        self.assertIn('CDN 设置', main.cdn_error)
        self.assertFalse(main.backup_url)
        self.assertEqual(main.display_source, 'shopify')

    def test_08c_backup_link_check(self):
        Queue = self.Queue
        def probe(result):
            return patch.object(type(Queue), '_probe', staticmethod(lambda url: result))
        with probe(('ok', 200, 100)):
            self.assertFalse(Queue._check_backup_link('https://x/a.jpg', 100))
        with probe(('ok', 200, 90)):
            self.assertIn('大小不对', Queue._check_backup_link('https://x/a.jpg', 100))
        with probe(('unknown', 403, 0)):
            self.assertIn('HTTP 403', Queue._check_backup_link('https://x/a.jpg', 100))
        with probe(('gone', 404, 0)):
            self.assertIn('HTTP 404', Queue._check_backup_link('https://x/a.jpg', 100))
        with probe(('unknown', 0, 0)):  # 网络错误：不当成问题
            self.assertFalse(Queue._check_backup_link('https://x/a.jpg', 100))

    def test_08d_heic_named_image_is_uploaded_with_real_extension(self):
        """iPhone 的 .heic 地址，Shopify 返回的其实是 JPEG/PNG：按实际格式取扩展名上传
        （图片源不允许 .heic，浏览器也显示不了）。常见格式的文件名不改。"""
        source = self._media_source()
        img = 'https://cdn.shopify.com/s/files/1/x/'
        self._run_import(self._csv(
            f'heic-p,Heic,,,,,TRUE,Title,Default Title,,,H-1,,5,,{img}FullSizeRender.heic?v=1,1,,,active',
            f'heic-p,,,,,,,,,,,,,,,{img}normal.jpg?v=1,2,,,',
        ), media_source_id=source.id)
        tmpl = self._tmpl('heic-p')
        rows = self.Queue.search([('product_tmpl_id', '=', tmpl.id)], order='sequence')
        with self._mock_get(), self._mock_upload():
            for rec in rows:
                rec._process()
        self.assertEqual(set(rows.mapped('state')), {'done'}, rows.mapped('error_message'))
        self.assertEqual([u['filename'] for u in self.uploads],
                         [f'{tmpl.id}_FullSizeRender.png', f'{tmpl.id}_normal.jpg'])
        self.assertEqual(self.uploads[0]['content_type'], 'image/png')
        self.assertEqual(rows[0].alist_target_path, f'shopify-products/{tmpl.id}_FullSizeRender.png')

    def test_08e_concurrency_conflict_is_retried_without_reuploading(self):
        """media_picker 的主图同步同时在改同一个商品时，数据库会报并发冲突：
        在定时任务里要回滚重试，而且不能把已经传上去的文件再传一遍。"""
        import psycopg2
        source = self._media_source()
        self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        main = self.Queue.search([('product_tmpl_id', '=', mug.id), ('role', '=', 'main')])
        Queue = type(self.Queue)
        real_write_bind = Queue._write_bind
        calls = {'n': 0}

        def flaky_write_bind(rec, prefer, backup_url=None):
            calls['n'] += 1
            if calls['n'] == 1:
                err = psycopg2.errors.SerializationFailure('could not serialize access due to concurrent update')
                raise err
            return real_write_bind(rec, prefer, backup_url=backup_url)

        with self._mock_get(), self._mock_upload(), \
                patch.object(Queue, '_write_bind', flaky_write_bind), \
                patch.object(Queue, '_rollback_for_retry', lambda rec: None), \
                patch(f'{QUEUE_MODULE}.time.sleep'):
            main.with_context(cron_id=1)._process()
        self.assertEqual((main.state, main.backup_state, main.display_source), ('done', 'ok', 'shopify'),
                         main.error_message)
        self.assertEqual(calls['n'], 2)
        self.assertEqual(len(self.uploads), 1, '重试不重复上传')

        # 不在定时任务里（页面按钮）：不回滚别人的改动，记成失败，之后可以重试
        calls['n'] = 0
        main.write({'state': 'pending'})
        with self._mock_get(), self._mock_upload(), mute_logger(QUEUE_MODULE), \
                patch.object(Queue, '_write_bind', flaky_write_bind):
            main._process()
        self.assertEqual(main.state, 'error')

    def test_08f_parallel_upload_results_are_used_by_main_thread(self):
        """上传在工作线程里做完后，主线程写台账时直接用结果，不再调用上传；
        同一张图（变体图 = 画廊图）只预先上传一次。"""
        source = self._media_source()
        img = 'https://cdn.shopify.com/s/files/1/x/'
        self._run_import(self._csv(
            f'par-bag,Bag,,,,,TRUE,Color,Black,,,P-B,,10,,{img}black.jpg,1,{img}black.jpg,,active',
            f'par-bag,,,,,,,,White,,,P-W,,10,,{img}white.jpg,2,,,',
        ), media_source_id=source.id)
        bag = self._tmpl('par-bag')
        rows = self.Queue.search([('product_tmpl_id', '=', bag.id)], order='id')
        self.assertEqual(len(rows), 3)  # black(画廊) / white(画廊) / black(变体)
        Queue = type(self.Queue)
        from odoo.addons.shopify_csv_import.models import shopify_image_queue as mod
        pre_uploaded = []
        # 工作线程里不能碰测试用的数据库连接：要用到的东西先在主线程里取好
        names = {r.id: r._guess_filename(r.source_url) for r in rows}
        dbname = self.env.cr.dbname

        def fake_thread_upload(model, rec_id, content):
            pre_uploaded.append(rec_id)
            name = names[rec_id]
            mod._UPLOAD_MEMO[(dbname, rec_id)] = {
                'backup_state': 'ok', 'backup_url': f'https://media.example.com/par/{name}',
                'backup_ref': f'/par/{name}', 'backup_size': len(content), 'cdn_error': False,
                'storage': 'cdn', 'alist_target_path': f'par/{name}',
            }

        with self._mock_get(), self._mock_upload(), \
                patch.object(Queue, '_parallel_upload_enabled', lambda model: True), \
                patch.object(Queue, '_upload_in_own_cursor', fake_thread_upload):
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual(set(rows.mapped('state')), {'done'}, rows.mapped('error_message'))
        self.assertEqual(sorted(pre_uploaded), sorted(rows[:2].ids), '变体图和画廊图是同一张，只预先上传一次')
        self.assertEqual(self.uploads, [], '主线程不应再上传')
        self.assertEqual(set(rows.mapped('backup_state')), {'ok'})
        self.assertEqual(rows[2].backup_url, rows[0].backup_url, '变体行复用画廊行的备份')
        self.assertFalse(mod._UPLOAD_MEMO, '用完要清掉')

    def test_09_untrusted_shopify_domain_displays_backup(self):
        """media_picker 的可信域名名单不认 Shopify 时：有备份就直接显示备份。"""
        self.env['ir.config_parameter'].sudo().set_param('media_picker.trusted_domains', 'other.example.org')
        source = self._media_source()
        batch = self._start_import()
        batch.media_source_id = source  # 绕过向导的事先检查，直接测处理逻辑
        batch._run_products()
        mug = self._tmpl('sci-test-mug')
        main = self.Queue.search([('product_tmpl_id', '=', mug.id), ('role', '=', 'main')])
        with self._mock_get(), self._mock_upload():
            main._process()
        self.assertEqual((main.state, main.display_source), ('done', 'backup'), main.error_message)
        bind = self._binds(mug)
        self.assertEqual(bind.url, main.backup_url)
        self.assertEqual(bind.source_id, source)

    @mute_logger(QUEUE_MODULE)
    def test_09b_untrusted_domain_without_backup_is_an_error(self):
        self.env['ir.config_parameter'].sudo().set_param('media_picker.trusted_domains', 'other.example.org')
        source = self._media_source()
        batch = self._start_import()
        batch.media_source_id = source
        batch._run_products()
        main = self.Queue.search([('product_tmpl_id', '=', self._tmpl('sci-test-mug').id), ('role', '=', 'main')])
        with self._mock_get(), self._mock_upload(fail='boom'):
            main._process()
        self.assertEqual(main.state, 'error')
        self.assertIn('cdn.shopify.com', main.error_message)

    def test_10_variant_bind_does_not_overwrite_gallery_bind(self):
        source = self._media_source()
        img = 'https://cdn.shopify.com/s/files/1/x/black.jpg'
        self._run_import(self._csv(
            f'vb-bag,Bag,,,,,TRUE,Color,Black,,,VB-B,,10,,{img},1,{img},,active',
            'vb-bag,,,,,,,,White,,,VB-W,,10,,,,,,',
        ), media_source_id=source.id)
        bag = self._tmpl('vb-bag')
        rows = self.Queue.search([('product_tmpl_id', '=', bag.id)], order='id')
        self.assertEqual(len(rows), 2)
        with self._mock_get(), self._mock_upload():
            for rec in rows:
                rec._process()
        self.assertEqual(set(rows.mapped('state')), {'done'}, rows.mapped('error_message'))
        # 变体图和画廊图是同一张：只上传一次，两行共用同一份备份
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(len(set(rows.mapped('backup_url'))), 1)
        self.assertEqual(set(rows.mapped('backup_state')), {'ok'})
        binds = self._binds(bag)
        self.assertEqual(len(binds), 2)
        self.assertTrue(binds.filtered(lambda b: not b.product_variant_id).is_main)
        variant_bind = binds.filtered('product_variant_id')
        self.assertEqual(variant_bind.product_variant_id, self._variant(bag, {'Color': 'Black'}))
        self.assertFalse(variant_bind.is_main)

    # ------------------------------------------------------------------
    # Shopify 失效 → 对象存储顶上
    # ------------------------------------------------------------------
    def test_10b_failover_when_shopify_link_is_broken(self):
        """media_picker 的链接健康检查把 Shopify 外链判成 broken：换成对象存储的备份。"""
        source, mug, rows, _get = self._sync_mug()
        binds = self._binds(mug)
        binds.write({'health_status': 'broken', 'fail_count': 3})
        self.assertEqual(self.Queue._cron_failover(), 2)
        self.assertEqual(binds.mapped('url'), rows.mapped('backup_url'))
        self.assertEqual(binds.source_id, source, '备份链接的域名由图片源担保')
        self.assertEqual(set(binds.mapped('health_status')), {'unchecked'}, '换了链接要重新检查')
        self.assertEqual(set(rows.mapped('display_source')), {'backup'})
        self.assertEqual(set(rows.mapped('source_state')), {'gone'})
        self.assertEqual(set(rows.mapped('verdict')), {'source_gone'})
        self.assertEqual(self.Queue._cron_failover(), 0, '切过的不会再切')

    def test_10c_failover_without_backup_is_flagged(self):
        _source, mug, rows, _get = self._sync_mug(fail_upload='no token')
        binds = self._binds(mug)
        binds.write({'health_status': 'broken'})
        self.assertEqual(self.Queue._cron_failover(), 0)
        self.assertEqual(binds.mapped('url'), rows.mapped('source_url'), '没有备份可换')
        self.assertEqual(set(rows.mapped('verdict')), {'lost'})

    def test_10d_manual_switch_both_ways(self):
        _source, mug, rows, _get = self._sync_mug()
        main = rows[0]
        main.action_switch_to_backup()
        self.assertEqual((main.display_source, main.bind_id.url), ('backup', main.backup_url))
        main.action_switch_to_shopify()
        self.assertEqual((main.display_source, main.bind_id.url), ('shopify', main.source_url))
        self.assertFalse(main.bind_id.source_id)

    # ------------------------------------------------------------------
    # 对账 / 中继
    # ------------------------------------------------------------------
    def _mock_probe(self, results):
        """results: {url 片段: (HTTP 状态码, Content-Length) 或 异常}"""
        def fake_head(url, **kw):
            for fragment, result in results.items():
                if fragment in url:
                    if isinstance(result, Exception):
                        raise result
                    code, size = result
                    resp = fake_response(status=200)
                    resp.status_code = code
                    resp.headers = {'Content-Length': str(size)} if size else {}
                    return resp
            raise AssertionError(f'unexpected probe {url}')
        # 探测先发 HEAD，遇到 403/405 会改用带 Range 的 GET 再试：两个都要模拟
        stack = contextlib.ExitStack()
        stack.enter_context(patch(f'{QUEUE_MODULE}.requests.head', side_effect=fake_head))
        stack.enter_context(patch(f'{QUEUE_MODULE}.requests.get', side_effect=fake_head))
        return stack

    def _verify(self, rows, results):
        rows.action_verify()
        self.assertEqual(set(rows.mapped('job')), {'verify'})
        with self._mock_probe(results):
            self.Queue._cron_process_pending(limit=30)

    def test_10e_verify_consistent(self):
        _source, _mug, rows, _get = self._sync_mug()
        size = len(self.png)
        self._verify(rows, {'cdn.shopify.com': (200, size), 'media.example.com': (200, size)})
        self.assertEqual(set(rows.mapped('state')), {'done'}, rows.mapped('error_message'))
        self.assertEqual(set(rows.mapped('verdict')), {'ok'})
        self.assertTrue(all(rows.mapped('source_checked_at')) and all(rows.mapped('backup_checked_at')))

    def test_10f_verify_detects_missing_and_mismatched_backup(self):
        _source, mug, rows, _get = self._sync_mug()
        size = len(self.png)
        main, extra = rows
        self._verify(rows, {
            'cdn.shopify.com': (200, size),
            f'{mug.id}_mug-1.jpg': (404, 0),          # 主图的备份文件没了
            f'{mug.id}_mug-2.jpg': (200, size + 10),  # 附加图的备份大小不对
        })
        self.assertEqual((main.backup_state, main.verdict), ('missing', 'backup_missing'))
        self.assertEqual((extra.backup_state, extra.verdict), ('mismatch', 'mismatch'))
        self.assertEqual(set(rows.mapped('display_source')), {'shopify'}, '备份有问题时不切换显示')

    def test_10g_verify_source_gone_fails_over(self):
        """对账时 Shopify 明确返回 404：马上换成对象存储链接。"""
        _source, _mug, rows, _get = self._sync_mug()
        size = len(self.png)
        self._verify(rows, {'cdn.shopify.com': (404, 0), 'media.example.com': (200, size)})
        self.assertEqual(set(rows.mapped('source_state')), {'gone'})
        self.assertEqual(set(rows.mapped('display_source')), {'backup'})
        self.assertEqual(rows.bind_id.mapped('url'), rows.mapped('backup_url'))

    def test_10g2_backup_403_counts_as_missing(self):
        """对象存储对路径不对的文件回 403：对账要判成备份丢失，而不是"说不准"。"""
        _source, _mug, rows, _get = self._sync_mug()
        self._verify(rows, {'cdn.shopify.com': (200, len(self.png)), 'media.example.com': (403, 0)})
        self.assertEqual(set(rows.mapped('backup_state')), {'missing'})
        self.assertEqual(set(rows.mapped('verdict')), {'backup_missing'})

    def test_10h_network_error_is_not_treated_as_gone(self):
        """超时 / 连接失败不能当成"失效"：结论不变，不切换。"""
        _source, _mug, rows, _get = self._sync_mug()
        self._verify(rows, {'cdn.shopify.com': OSError('proxy down'), 'media.example.com': (503, 0)})
        self.assertEqual(set(rows.mapped('source_state')), {'ok'})
        self.assertEqual(set(rows.mapped('backup_state')), {'ok'})
        self.assertEqual(set(rows.mapped('display_source')), {'shopify'})

    def test_10i_push_backup_relays_from_local_when_source_is_gone(self):
        """中继：Shopify 源已经下不到了，主图改用本地那份上传；非主图没有本地副本，报错。"""
        _source, mug, rows, _get = self._sync_mug(fail_upload='no token')
        main, extra = rows
        mug.image_1920 = base64.b64encode(self.png)  # 主图在本地有一份（media_picker 同步进来的）
        rows.action_push_backup()
        self.assertEqual(set(rows.mapped('job')), {'backup'})
        with patch(f'{QUEUE_MODULE}.requests.get', return_value=fake_response(status=404)), \
                self._mock_upload(), mute_logger(QUEUE_MODULE):
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((main.state, main.backup_state, main.verdict), ('done', 'ok', 'ok'))
        self.assertEqual([u['filename'] for u in self.uploads], [f'{mug.id}_mug-1.jpg'])
        self.assertTrue(self.uploads[0]['bytes'])
        self.assertEqual(extra.state, 'error')
        self.assertIn('本地也没有', extra.error_message)

    # ------------------------------------------------------------------
    # 下载 / 批处理
    # ------------------------------------------------------------------
    def test_11_shopify_resized_url(self):
        Queue = type(self.Queue)
        url = 'https://cdn.shopify.com/s/files/1/0889/files/a.jpg?v=1771441417'
        resized = Queue._shopify_resized_url(url)
        self.assertEqual(
            resized, 'https://cdn.shopify.com/s/files/1/0889/files/a.jpg?v=1771441417&width=1920&height=1920')
        self.assertEqual(
            Queue._shopify_resized_url('https://cdn.shopify.com/a.jpg?width=100&v=2'),
            'https://cdn.shopify.com/a.jpg?v=2&width=1920&height=1920')
        other = 'https://img.example.com/a.jpg?v=1'
        self.assertEqual(Queue._shopify_resized_url(other), other)

    def test_12_resized_download_falls_back_to_original(self):
        self._run_import()
        rec = self.Queue.search([], order='id', limit=1)
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return fake_response(status=404) if 'width=' in url else fake_response(self.png)
        with patch(f'{QUEUE_MODULE}.requests.get', side_effect=fake_get):
            rec._process()
        self.assertEqual(rec.state, 'done', rec.error_message)
        self.assertEqual(len(calls), 2)
        self.assertIn('width=1920', calls[0])
        self.assertEqual(calls[1], rec.source_url)

    def test_13_cron_commits_progress_per_image(self):
        self._run_import()
        IrCron = type(self.env['ir.cron'])
        with self._mock_get(), \
                patch.object(IrCron, '_commit_progress', autospec=True, return_value=100.0) as progress:
            processed = self.Queue.with_context(cron_id=1)._cron_process_pending(limit=3)
        self.assertEqual(processed, 3)
        args = [c.args[1:] + tuple(sorted(c.kwargs.items())) for c in progress.call_args_list]
        # 先报告剩余总数（5 张待处理）；每组开始时提交一次状态（页面上显示"正在下载…"），
        # 之后每张图提交一次
        self.assertEqual(args, [(('remaining', 5),), (0,), (1,), (1,), (1,)])
        self.assertEqual(self.Queue.search_count([('state', '=', 'pending')]), 2)

    def test_14_time_budget_stops_batch(self):
        self._run_import()
        with self._mock_get(), \
                patch(f'{QUEUE_MODULE}.DOWNLOAD_WORKERS', 2), \
                patch.object(type(self.Queue), '_time_budget', return_value=0):
            processed = self.Queue._cron_process_pending(limit=30)
        # 预算用完后不再开始新的一组，但至少处理完第一组，保证队列一定往前走
        self.assertEqual(processed, 2)

    def test_14b_parallel_prefetch_downloads_each_image_once(self):
        self._run_import()
        with self._mock_get() as get:
            processed = self.Queue._cron_process_pending(limit=30)
        self.assertEqual(processed, 5)
        self.assertEqual(get.call_count, 5)
        self.assertEqual(set(self.Queue.search([]).mapped('state')), {'done'})

    def test_15_cron_job_budget_exhausted_reports_done(self):
        self._run_import()
        IrCron = type(self.env['ir.cron'])
        with patch.object(IrCron, '_commit_progress', autospec=True, return_value=0.0) as progress, \
                patch.object(type(self.Queue), '_cron_job_deadline', return_value=0.0):
            processed = self.Queue.with_context(cron_id=1)._cron_process_pending(limit=30)
        self.assertEqual(processed, 0)
        self.assertEqual(progress.call_args, call(self.env['ir.cron'], remaining=0))

    def test_16_cron_record_exists(self):
        cron = self.env.ref('shopify_csv_import.ir_cron_shopify_image_sync')
        self.assertTrue(cron.active)
        self.assertEqual((cron.interval_number, cron.interval_type), (1, 'minutes'))
