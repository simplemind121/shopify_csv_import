# -*- coding: utf-8 -*-
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import ShopifyImportCase, fake_response, png_bytes

QUEUE_MODULE = 'odoo.addons.shopify_csv_import.models.shopify_image_queue'


@tagged('post_install', '-at_install', 'shopify_csv_import')
class TestShopifyImportBatch(ShopifyImportCase):
    """导入批次：后台分段执行、进度、暂停、重试。"""

    def setUp(self):
        super().setUp()
        self.png = png_bytes()

    def _mock_get(self):
        return patch(f'{QUEUE_MODULE}.requests.get', return_value=fake_response(self.png))

    def test_01_wizard_creates_queued_batch_and_triggers_cron(self):
        cron = self.env.ref('shopify_csv_import.ir_cron_shopify_import_batch')
        before = self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)])
        batch = self._start_import()
        self.assertEqual(batch.state, 'queued')
        self.assertEqual(batch.total_products, 4)
        self.assertTrue(batch.csv_file)
        self.assertFalse(self._tmpl('sci-test-mug'), '商品导入应该在后台做，向导里不做')
        self.assertGreater(self.env['ir.cron.trigger'].search_count([('cron_id', '=', cron.id)]), before)

    def test_02_products_resume_across_runs(self):
        """时间预算用完就停，下一轮从 next_index 接着做，不重复导入。"""
        batch = self._start_import()
        finished = batch._run_products(deadline=time.monotonic() - 1)
        self.assertFalse(finished)
        self.assertEqual((batch.state, batch.next_index), ('importing', 1))
        self.assertEqual(batch.created_count, 1)
        self.assertTrue(batch._run_products())
        self.assertEqual(batch.next_index, 4)
        self.assertEqual((batch.created_count, batch.updated_count), (4, 0))
        self.assertEqual(batch.state, 'syncing')
        self.assertEqual(batch.import_log.count('[新建]'), 4)

    def test_03_cron_run_processes_queued_batches(self):
        batch = self._start_import()
        self.env['shopify.import.batch']._cron_run()
        self.assertEqual(batch.state, 'syncing')
        self.assertEqual(batch.created_count, 4)

    def test_04_images_wait_until_products_done(self):
        batch = self._start_import()
        batch._run_products(deadline=time.monotonic() - 1)  # 导入了 1 个商品（2 张图已排队）
        self.assertTrue(batch.queue_ids)
        with self._mock_get():
            self.assertEqual(self.Queue._cron_process_pending(limit=30), 0)
        batch._run_products()
        with self._mock_get():
            self.assertEqual(self.Queue._cron_process_pending(limit=30), 5)

    def test_05_progress_and_completion(self):
        batch = self._run_import()
        snap = batch.get_progress_snapshot(batch.id)
        self.assertEqual((snap['img_total'], snap['img_pending'], snap['img_done']), (5, 5, 0))
        self.assertEqual(snap['product_progress'], 100.0)
        self.assertEqual(snap['progress'], 20.0)
        self.assertTrue(snap['active'])
        with self._mock_get():
            self.Queue._cron_process_pending(limit=2)
        batch.invalidate_recordset()
        self.assertEqual((batch.img_done, batch.img_pending), (2, 3))
        self.assertAlmostEqual(batch.image_progress, 40.0)
        self.assertTrue(batch.speed_text)
        self.assertTrue(batch.eta_text)
        self.assertIn('正在', batch.status_message)
        with self._mock_get():
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual(batch.state, 'done')
        self.assertTrue(batch.finished_at)
        snap = batch.get_progress_snapshot(batch.id)
        self.assertEqual((snap['progress'], snap['active']), (100.0, False))
        self.assertEqual(snap['img_binary'], 5)

    def test_06_pause_and_resume(self):
        batch = self._run_import()
        batch.action_pause()
        with self._mock_get():
            self.assertEqual(self.Queue._cron_process_pending(limit=30), 0)
        self.assertEqual(batch.get_progress_snapshot(batch.id)['eta_text'], '已暂停')
        batch.action_resume()
        with self._mock_get():
            self.assertEqual(self.Queue._cron_process_pending(limit=30), 5)

    def test_07_paused_batch_skipped_by_product_cron(self):
        batch = self._start_import()
        batch.action_pause()
        self.env['shopify.import.batch']._cron_run()
        self.assertEqual(batch.state, 'queued')

    @mute_logger(QUEUE_MODULE)
    def test_08_retry_errors(self):
        batch = self._run_import()
        with patch(f'{QUEUE_MODULE}.requests.get', return_value=fake_response(status=404)):
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((batch.state, batch.img_error), ('done_errors', 5))
        self.assertIn('404', batch.last_error)
        batch.action_retry_errors()
        self.assertEqual(batch.state, 'syncing')
        with self._mock_get():
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((batch.state, batch.img_error, batch.img_done), ('done', 0, 5))

    def test_09_push_missing_backups(self):
        """图片源没配好时图片缺备份（照样能显示）；配好后「补传缺备份的图片」把它们传上去。"""
        source = self._media_source()
        batch = self._run_import(media_source_id=source.id)
        with self._mock_get(), self._mock_upload(fail='401 token invalid'):
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((batch.state, batch.img_fallback, batch.img_cdn), ('done', 5, 0))
        self.assertEqual(batch.img_display_shopify, 5, '缺备份不影响显示')
        self.assertIn('401', batch.last_error)

        batch.action_retry_cdn_fallback()
        self.assertEqual(batch.state, 'syncing')
        self.assertEqual(set(batch.queue_ids.mapped('job')), {'backup'})
        with self._mock_get(), self._mock_upload():
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((batch.state, batch.img_cdn, batch.img_fallback), ('done', 5, 0))
        self.assertEqual(set(batch.queue_ids.mapped('verdict')), {'ok'})

    def test_09b_local_images_move_to_object_storage(self):
        """旧数据迁移：本地模式导入的图片，之后改走对象存储——附加图的本地副本要删掉。"""
        batch = self._run_import()  # 没选图片源：全部存本地
        with self._mock_get():
            self.Queue._cron_process_pending(limit=30)
        mug = self._tmpl('sci-test-mug')
        self.assertEqual(len(mug.product_template_image_ids), 1)
        self.assertEqual(batch.img_binary, 5)
        self.assertEqual(set(batch.queue_ids.mapped('verdict')), {'local'})

        source = self._media_source()
        batch.media_source_id = source
        batch.queue_ids.write({'media_source_id': source.id})
        batch.action_retry_cdn_fallback()
        with self._mock_get(), self._mock_upload():
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((batch.state, batch.img_cdn, batch.img_binary), ('done', 5, 0))
        self.assertFalse(mug.product_template_image_ids, '附加图的本地副本应被删除')
        self.assertTrue(mug.image_1920, '主图的本地那份保留')
        self.assertEqual(len(self.env['media.bind'].search([('product_tmpl_id', '=', mug.id)])), 2)

    def test_09c_verify_and_failover_from_batch(self):
        source = self._media_source()
        batch = self._run_import(media_source_id=source.id)
        with self._mock_get(), self._mock_upload():
            self.Queue._cron_process_pending(limit=30)
        self.env['media.bind'].search([('product_tmpl_id', '=', self._tmpl('sci-test-mug').id)]).write(
            {'health_status': 'broken'})
        action = batch.action_failover()
        self.assertEqual(action['tag'], 'display_notification')
        batch.invalidate_recordset()
        self.assertEqual((batch.img_display_backup, batch.img_display_shopify, batch.img_source_gone), (2, 3, 2))
        snap = batch.get_progress_snapshot(batch.id)
        self.assertEqual((snap['img_display_backup'], snap['img_source_gone'], snap['img_lost']), (2, 2, 0))
        batch.action_verify()
        self.assertEqual(batch.state, 'syncing')
        self.assertEqual(set(batch.queue_ids.mapped('job')), {'verify'})

    def test_10_batch_without_images_finishes_immediately(self):
        batch = self._run_import(self._csv('noimg,No Image,,,,,TRUE,Title,Default Title,,,N-1,,5,,,,,,active'))
        self.assertEqual(batch.state, 'done')
        self.assertEqual(batch.get_progress_snapshot(batch.id)['progress'], 100.0)

    def test_11_eta_uses_recent_speed_not_whole_history(self):
        """同步中途停过很久（暂停、重启、机器休眠），预计剩余时间不能被拉成几个小时。"""
        batch = self._run_import()
        with self._mock_get():
            self.Queue._cron_process_pending(limit=2)
        batch.images_started_at = fields.Datetime.now() - timedelta(hours=5)
        batch.invalidate_recordset()
        self.assertEqual(batch.img_pending, 3)
        self.assertNotIn('小时', batch.eta_text)

    def test_12_no_recent_activity_shows_no_fake_eta(self):
        """刚归入批次 / 还没开始处理：不显示速度，也不硬算预计时间。"""
        batch = self._run_import()
        snap = batch.get_progress_snapshot(batch.id)
        self.assertFalse(snap['speed_text'])
        self.assertEqual(snap['eta_text'], '暂未处理（等待后台任务）')
        # 重新排队之类的操作会改 write_date，但不能被算成"刚处理完"
        batch.queue_ids.write({'state': 'pending'})
        self.assertFalse(batch.get_progress_snapshot(batch.id)['speed_text'])

    def test_13_disabled_cron_is_reported(self):
        batch = self._run_import()
        self.assertFalse(batch.get_progress_snapshot(batch.id)['cron_warning'])
        self.env.ref('shopify_csv_import.ir_cron_shopify_image_sync').active = False
        warning = batch.get_progress_snapshot(batch.id)['cron_warning']
        self.assertIn('图片同步', warning)
        self.assertIn('计划任务', warning)
        queued = self._start_import()
        self.env.ref('shopify_csv_import.ir_cron_shopify_import_batch').active = False
        self.assertIn('商品导入', queued.get_progress_snapshot(queued.id)['cron_warning'])

    def test_14_menu_opens_import_list_first(self):
        """进「Shopify 导入」先看到导入记录，不是必须先上传文件的弹窗。"""
        root = self.env.ref('shopify_csv_import.menu_shopify_import_root')
        first = root.child_id.sorted('sequence')[:1]
        self.assertEqual(first.action, self.env.ref('shopify_csv_import.action_shopify_import_batch'))

    def test_15_list_button_opens_upload_dialog(self):
        """走按钮真实的调用路径（和网页点击一样：把勾选的 id 列表作为参数传进来）。"""
        from odoo.service.model import call_kw
        Batch = self.env['shopify.import.batch']
        for selected_ids in ([], self._start_import().ids):
            action = call_kw(Batch, 'action_open_import_wizard', [selected_ids], {})
            self.assertEqual((action['res_model'], action['target']), ('shopify.import.wizard', 'new'))

    def test_16_every_view_button_is_callable_like_a_click(self):
        """所有视图里的按钮都按"网页点击"的方式调用一遍，防止再出现签名对不上的方法。"""
        import re
        from odoo.service.model import call_kw
        batch = self._run_import()
        row = batch.queue_ids[:1]
        cases = {
            'shopify.import.batch': (batch, [
                'shopify_csv_import.view_shopify_import_batch_list',
                'shopify_csv_import.view_shopify_import_batch_form']),
            'shopify.image.queue': (row, [
                'shopify_csv_import.view_shopify_image_queue_list',
                'shopify_csv_import.view_shopify_image_queue_form']),
        }
        called = []
        with self._mock_get():
            for model, (record, xmlids) in cases.items():
                names = set()
                for xmlid in xmlids:
                    names |= set(re.findall(r'<button[^>]*name="(\w+)"[^>]*type="object"', self.env.ref(xmlid).arch))
                    names |= set(re.findall(r'<button[^>]*type="object"[^>]*name="(\w+)"', self.env.ref(xmlid).arch))
                self.assertTrue(names, model)
                for name in sorted(names):
                    call_kw(self.env[model], name, [record.ids], {})
                    called.append(f'{model}.{name}')
        self.assertIn('shopify.import.batch.action_open_import_wizard', called)
        self.assertGreaterEqual(len(called), 12)
