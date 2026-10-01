# -*- coding: utf-8 -*-
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import ShopifyImportCase, fake_response, png_bytes

QUEUE_MODULE = 'odoo.addons.shopify_csv_import.models.shopify_image_queue'
ALIST_CLIENT = 'odoo.addons.media_picker.models.pem_alist_client.get_file'


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

    def test_09_retry_cdn_fallback_replaces_local_copy(self):
        """token 没配对时回退本地；改好后「重新上传回退本地的图片」：
        上到 Alist，并删掉当时存的本地附加图，画廊里不重复。"""
        source = self.env['product.media.source'].create({
            'name': 'Test Alist', 'alist_url': 'https://alist.example.com',
            'trusted_domains': 'media.example.com',
        })
        batch = self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        with self._mock_get():  # 没 token：回退本地
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((batch.state, batch.img_fallback, batch.img_cdn), ('done', 5, 0))
        self.assertEqual(len(mug.product_template_image_ids), 1)

        source.alist_token = 'fixed-token'
        batch.action_retry_cdn_fallback()
        self.assertEqual(batch.state, 'syncing')
        put_resp = fake_response(json_data={'code': 200})
        with self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put', return_value=put_resp), \
                patch(ALIST_CLIENT, return_value={'raw_url': 'https://media.example.com/d/x.jpg'}):
            self.Queue._cron_process_pending(limit=30)
        self.assertEqual((batch.state, batch.img_cdn, batch.img_fallback), ('done', 5, 0))
        self.assertFalse(mug.product_template_image_ids, '本地附加图副本应被删除')
        self.assertEqual(len(self.env['media.bind'].search([('product_tmpl_id', '=', mug.id)])), 2)

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
        action = self.env['shopify.import.batch'].action_open_import_wizard()
        self.assertEqual((action['res_model'], action['target']), ('shopify.import.wizard', 'new'))
