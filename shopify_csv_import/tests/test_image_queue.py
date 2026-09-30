# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import ShopifyImportCase, fake_response, png_bytes

QUEUE_MODULE = 'odoo.addons.shopify_csv_import.models.shopify_image_queue'
ALIST_CLIENT = 'odoo.addons.media_picker.models.pem_alist_client.get_file'


@tagged('post_install', '-at_install', 'shopify_csv_import')
class TestShopifyImageQueue(ShopifyImportCase):

    def setUp(self):
        super().setUp()
        self.png = png_bytes()

    def _mock_get(self, url_map=None, default=None):
        url_map = url_map or {}
        default = default or fake_response(self.png)
        return patch(f'{QUEUE_MODULE}.requests.get',
                     side_effect=lambda url, **kw: url_map.get(url, default))

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

    def test_06_retry_processes_selected_record_only(self):
        self._run_import()
        rows = self.Queue.search([], order='id')
        target = rows[-1]
        target.write({'state': 'error', 'error_message': 'boom'})
        with self._mock_get():
            target.action_retry()
        self.assertEqual(target.state, 'done')
        self.assertFalse(target.error_message)
        # 其它待处理记录没有被"顺带"处理
        self.assertEqual(self.Queue.search_count([('state', '=', 'pending')]), len(rows) - 1)

    # ------------------------------------------------------------------
    # Alist / media_picker 路径
    # ------------------------------------------------------------------
    def _alist_source(self, trusted='media.example.com'):
        return self.env['product.media.source'].create({
            'name': 'Test Alist',
            'alist_url': 'https://alist.example.com/',
            'alist_token': 'test-token',
            'trusted_domains': trusted,
        })

    def test_07_alist_upload_and_media_bind(self):
        source = self._alist_source()
        self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        rows = self.Queue.search([('product_tmpl_id', '=', mug.id)], order='sequence')

        put_resp = fake_response(json_data={'code': 200, 'message': 'success'})
        with self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put', return_value=put_resp) as put, \
                patch(ALIST_CLIENT, side_effect=lambda src, path: {
                    'raw_url': f'https://media.example.com/d{path}'}):
            for rec in rows:
                rec._process()

        self.assertEqual(set(rows.mapped('state')), {'done'}, rows.mapped('error_message'))
        self.assertEqual(put.call_count, 2)
        url, kwargs = put.call_args_list[0].args[0], put.call_args_list[0].kwargs
        self.assertEqual(url, 'https://alist.example.com/api/fs/put')
        self.assertEqual(kwargs['headers']['Authorization'], 'test-token')
        self.assertEqual(kwargs['headers']['File-Path'],
                         f'%2Fb2%2Fshopify-products%2F{mug.id}_mug-1.jpg')

        binds = self.env['media.bind'].search([('product_tmpl_id', '=', mug.id)], order='sequence')
        self.assertEqual(len(binds), 2)
        self.assertTrue(binds[0].is_main)
        self.assertEqual(binds[0].url, f'https://media.example.com/d/b2/shopify-products/{mug.id}_mug-1.jpg')
        self.assertEqual(binds[0].shopify_media_id, rows[0].source_url)
        self.assertTrue(mug.use_external_media)
        self.assertFalse(mug.image_1920, 'CDN 成功时不应再存本地二进制')

        # 再处理一次：按 shopify_media_id 去重，不会多出 media.bind
        with self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put', return_value=put_resp), \
                patch(ALIST_CLIENT, return_value={'raw_url': 'https://media.example.com/d/x.jpg'}):
            rows.action_retry()
        self.assertEqual(self.env['media.bind'].search_count([('product_tmpl_id', '=', mug.id)]), 2)

    def test_08_untrusted_domain_falls_back_to_binary(self):
        source = self._alist_source(trusted='media.example.com')
        self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        main = self.Queue.search([('product_tmpl_id', '=', mug.id), ('role', '=', 'main')])
        put_resp = fake_response(json_data={'code': 200})
        with self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put', return_value=put_resp), \
                patch(ALIST_CLIENT, return_value={'raw_url': 'https://evil.example.net/x.jpg'}):
            main._process()
        self.assertEqual(main.state, 'done')
        self.assertFalse(self.env['media.bind'].search([('product_tmpl_id', '=', mug.id)]))
        self.assertTrue(mug.image_1920)

    def test_09_alist_error_code_falls_back_to_binary(self):
        source = self._alist_source()
        self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        main = self.Queue.search([('product_tmpl_id', '=', mug.id), ('role', '=', 'main')])
        put_resp = fake_response(json_data={'code': 403, 'message': 'permission denied'})
        with self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put', return_value=put_resp), \
                patch(ALIST_CLIENT) as get_file:
            main._process()
        get_file.assert_not_called()
        self.assertEqual(main.state, 'done')
        self.assertTrue(mug.image_1920)
        self.assertFalse(mug.use_external_media)

    def test_10_cron_record_exists(self):
        cron = self.env.ref('shopify_csv_import.ir_cron_shopify_image_sync')
        self.assertTrue(cron.active)
        self.assertEqual((cron.interval_number, cron.interval_type), (2, 'minutes'))
