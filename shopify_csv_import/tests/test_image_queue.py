# -*- coding: utf-8 -*-
import os
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
        self.assertEqual(set(rows.mapped('storage')), {'cdn'})
        self.assertFalse(any(rows.mapped('cdn_error')))
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
        # 回退本地必须在记录上看得出来
        self.assertEqual(main.storage, 'binary')
        self.assertIn('403', main.cdn_error)

    def test_09b_token_from_secret_ref_env_var(self):
        """和 media_picker 一样：secret_ref 是环境变量名，token 从环境变量读。"""
        source = self._alist_source()
        source.write({'alist_token': False, 'secret_ref': 'SCI_TEST_ALIST_TOKEN'})
        self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        main = self.Queue.search([('product_tmpl_id', '=', mug.id), ('role', '=', 'main')])
        put_resp = fake_response(json_data={'code': 200})
        with patch.dict(os.environ, {'SCI_TEST_ALIST_TOKEN': 'token-from-env'}), self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put', return_value=put_resp) as put, \
                patch(ALIST_CLIENT, return_value={'raw_url': 'https://media.example.com/d/a.jpg'}):
            main._process()
        self.assertEqual(put.call_args.kwargs['headers']['Authorization'], 'token-from-env')
        self.assertEqual(main.storage, 'cdn')

    def test_09c_missing_token_falls_back_with_reason(self):
        """没 token 时不发请求（发了也只会 403），直接回退本地并写明原因。"""
        source = self._alist_source()
        source.write({'alist_token': False, 'secret_ref': 'SCI_TEST_UNSET_VAR'})
        self._run_import(media_source_id=source.id)
        mug = self._tmpl('sci-test-mug')
        main = self.Queue.search([('product_tmpl_id', '=', mug.id), ('role', '=', 'main')])
        with patch.dict(os.environ, {}, clear=False), self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put') as put:
            os.environ.pop('SCI_TEST_UNSET_VAR', None)
            main._process()
        put.assert_not_called()
        self.assertEqual((main.state, main.storage), ('done', 'binary'))
        self.assertIn('token', main.cdn_error)
        self.assertTrue(mug.image_1920)

    def test_10_variant_bind_does_not_overwrite_gallery_bind(self):
        source = self._alist_source()
        img = 'https://cdn.shopify.com/s/files/1/x/black.jpg'
        self._run_import(self._csv(
            f'vb-bag,Bag,,,,,TRUE,Color,Black,,,VB-B,,10,,{img},1,{img},,active',
            'vb-bag,,,,,,,,White,,,VB-W,,10,,,,,,',
        ), media_source_id=source.id)
        bag = self._tmpl('vb-bag')
        rows = self.Queue.search([('product_tmpl_id', '=', bag.id)], order='id')
        self.assertEqual(len(rows), 2)
        put_resp = fake_response(json_data={'code': 200})
        with self._mock_get(), \
                patch(f'{QUEUE_MODULE}.requests.put', return_value=put_resp), \
                patch(ALIST_CLIENT, return_value={'raw_url': 'https://media.example.com/d/black.jpg'}):
            for rec in rows:
                rec._process()
        binds = self.env['media.bind'].search([('product_tmpl_id', '=', bag.id)])
        self.assertEqual(len(binds), 2)
        gallery = binds.filtered(lambda b: not b.product_variant_id)
        self.assertTrue(gallery.is_main)
        self.assertEqual(binds.filtered('product_variant_id').product_variant_id,
                         self._variant(bag, {'Color': 'Black'}))

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
        # 先报告剩余总数（5 张待处理），再每张图提交一次
        self.assertEqual(args, [(('remaining', 5),), (1,), (1,), (1,)])
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
