# -*- coding: utf-8 -*-
import base64

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import ShopifyImportCase

COLOR, SIZE = 'SCI Color', 'SCI Size'


@tagged('post_install', '-at_install', 'shopify_csv_import')
class TestShopifyImportWizard(ShopifyImportCase):

    def test_01_counts_and_log(self):
        wizard = self._run_import()
        self.assertEqual(wizard.state, 'done')
        self.assertEqual(
            (wizard.created_count, wizard.updated_count, wizard.error_count), (4, 0, 0),
            wizard.import_log)
        # mug: 2, tee: 2 公共图片 + 1 变体图片, cap/hoodie: 0
        self.assertEqual(wizard.image_queue_count, 5)
        self.assertIn('[新建] sci-test-mug', wizard.import_log)

    def test_02_single_variant_fields(self):
        self._run_import()
        mug = self._tmpl('sci-test-mug')
        self.assertEqual(len(mug), 1)
        self.assertEqual(mug.name, 'SCI Test Ceramic Mug')
        self.assertEqual(len(mug.product_variant_ids), 1)
        self.assertAlmostEqual(mug.list_price, 12.5)
        self.assertAlmostEqual(mug.standard_price, 4.2)
        self.assertAlmostEqual(mug.weight, 0.35)
        self.assertEqual(mug.default_code, 'SCI-MUG-01')
        self.assertEqual(mug.barcode, 'SCI0000000011')
        self.assertTrue(mug.website_published)
        self.assertEqual(mug.x_shopify_status, 'active')
        self.assertIn('Nice mug', mug.description_ecommerce)
        self.assertEqual(mug.website_meta_title, 'Mug SEO')
        # "Title / Default Title" 不能被当成真实属性
        self.assertFalse(mug.attribute_line_ids)

    def test_03_categories(self):
        self._run_import()
        mug = self._tmpl('sci-test-mug')
        self.assertEqual(
            mug.categ_id.complete_name, 'Home & Garden / Kitchen & Dining / Drinkware')
        self.assertEqual(mug.public_categ_ids.name, 'Drinkware')
        self.assertEqual(mug.public_categ_ids.parent_id.name, 'Kitchen & Dining')
        # 空分类 -> 统一的"未分类"节点，且两个商品共用同一个节点
        cap, hoodie = self._tmpl('sci-test-cap'), self._tmpl('sci-test-hoodie')
        self.assertEqual(cap.categ_id.name, '未分类（Shopify导入）')
        self.assertEqual(cap.categ_id, hoodie.categ_id)
        self.assertEqual(cap.public_categ_ids, hoodie.public_categ_ids)

    def test_04_tags_and_vendor_filter(self):
        self._run_import()
        mug = self._tmpl('sci-test-mug')
        self.assertEqual(set(mug.product_tag_ids.mapped('name')), {'Acme Home', 'mug', 'kitchen'})
        cap = self._tmpl('sci-test-cap')
        # Vendor 是 1688 网址 -> 不导入成标签
        self.assertFalse(cap.product_tag_ids)
        self.assertFalse(cap.website_published)
        self.assertEqual(cap.x_shopify_status, 'draft')

    def test_05_multi_variant_option_names_from_first_row(self):
        """Shopify 只在第一行写 Option Name，后续行只有 Value——必须生成全部 4 个变体。"""
        self._run_import()
        tee = self._tmpl('sci-test-tee')
        self.assertEqual(len(tee.product_variant_ids), 4)
        self.assertEqual(set(tee.attribute_line_ids.mapped('attribute_id.name')), {COLOR, SIZE})
        expected = {
            ('Red', 'S'): (10.0, 'SCI-TEE-RS', 'SCI000000020'),
            ('Red', 'M'): (12.0, 'SCI-TEE-RM', 'SCI000000021'),
            ('Blue', 'S'): (11.0, 'SCI-TEE-BS', 'SCI000000022'),
            ('Blue', 'M'): (13.0, 'SCI-TEE-BM', 'SCI000000023'),
        }
        self.assertAlmostEqual(tee.list_price, 10.0)
        for (color, size), (price, sku, barcode) in expected.items():
            v = self._variant(tee, {COLOR: color, SIZE: size})
            self.assertAlmostEqual(v.lst_price, price, msg=f'{color}/{size}')
            self.assertEqual(v.default_code, sku)
            self.assertEqual(v.barcode, barcode)
            self.assertAlmostEqual(v.standard_price, 3.0)
            self.assertAlmostEqual(v.weight, 0.2)

    def test_06_single_attribute_prices(self):
        self._run_import()
        cap = self._tmpl('sci-test-cap')
        self.assertEqual(len(cap.product_variant_ids), 3)
        self.assertAlmostEqual(cap.list_price, 20.0)
        self.assertAlmostEqual(self._variant(cap, {SIZE: 'S'}).lst_price, 20.0)
        self.assertAlmostEqual(self._variant(cap, {SIZE: 'M'}).lst_price, 20.0)
        self.assertAlmostEqual(self._variant(cap, {SIZE: 'L'}).lst_price, 25.0)

    def test_07_non_additive_prices_warn(self):
        wizard = self._run_import()
        hoodie = self._tmpl('sci-test-hoodie')
        self.assertEqual(len(hoodie.product_variant_ids), 4)
        self.assertAlmostEqual(self._variant(hoodie, {COLOR: 'Red', SIZE: 'M'}).lst_price, 12.0)
        self.assertIn('[警告] sci-test-hoodie', wizard.import_log)
        self.assertIn('20.0', wizard.import_log)
        self.assertNotIn('[警告] sci-test-tee', wizard.import_log)

    def test_08_reimport_is_idempotent(self):
        self._run_import()
        n_tmpl = self.Template.search_count([('x_shopify_handle', 'like', 'sci-test-%')])
        n_queue = self.Queue.search_count([])
        n_variants = len(self._tmpl('sci-test-tee').product_variant_ids)

        wizard = self._run_import()
        self.assertEqual((wizard.created_count, wizard.updated_count), (0, 4))
        self.assertEqual(wizard.image_queue_count, 0)
        self.assertEqual(
            self.Template.search_count([('x_shopify_handle', 'like', 'sci-test-%')]), n_tmpl)
        self.assertEqual(self.Queue.search_count([]), n_queue)
        self.assertEqual(len(self._tmpl('sci-test-tee').product_variant_ids), n_variants)

    def test_09_reimport_updates_values_and_adds_variant(self):
        self._run_import(self._csv(
            'upd-shirt,Old Name,,,,,TRUE,Size,S,,,UPD-S,,5,,,,,,active',
            'upd-shirt,,,,,,,,M,,,UPD-M,,6,,,,,,',
        ))
        tmpl = self._tmpl('upd-shirt')
        self.assertEqual(len(tmpl.product_variant_ids), 2)
        self._run_import(self._csv(
            'upd-shirt,New Name,,,,,FALSE,Size,S,,,UPD-S,,7,,,,,,active',
            'upd-shirt,,,,,,,,M,,,UPD-M,,8,,,,,,',
            'upd-shirt,,,,,,,,L,,,UPD-L,,9,,,,,,',
        ))
        self.assertEqual(len(self._tmpl('upd-shirt')), 1)
        self.assertEqual(tmpl.name, 'New Name')
        self.assertFalse(tmpl.website_published)
        self.assertEqual(len(tmpl.product_variant_ids), 3)
        self.assertAlmostEqual(self._variant(tmpl, {'Size': 'S'}).lst_price, 7.0)
        self.assertAlmostEqual(self._variant(tmpl, {'Size': 'L'}).lst_price, 9.0)

    def test_10_image_queue_rows(self):
        self._run_import()
        mug = self._tmpl('sci-test-mug')
        rows = self.Queue.search([('product_tmpl_id', '=', mug.id)], order='sequence')
        self.assertEqual(rows.mapped('role'), ['main', 'extra'])
        self.assertTrue(all(r.state == 'pending' for r in rows))
        self.assertFalse(rows.media_source_id)
        self.assertFalse(any(rows.mapped('alist_target_path')))

        tee = self._tmpl('sci-test-tee')
        variant_row = self.Queue.search(
            [('product_tmpl_id', '=', tee.id), ('product_variant_id', '!=', False)])
        self.assertEqual(len(variant_row), 1)
        self.assertEqual(variant_row.product_variant_id, self._variant(tee, {COLOR: 'Blue', SIZE: 'M'}))
        self.assertTrue(variant_row.source_url.endswith('tee-blue.jpg'))

    def test_11_media_source_sets_alist_path(self):
        source = self.env['product.media.source'].create({'name': 'Test Alist'})
        self._run_import(media_source_id=source.id, alist_upload_path_prefix='/b2/test/')
        mug = self._tmpl('sci-test-mug')
        rows = self.Queue.search([('product_tmpl_id', '=', mug.id)], order='sequence')
        self.assertEqual(rows.media_source_id, source)
        self.assertEqual(rows[0].alist_target_path, f'/b2/test/{mug.id}_mug-1.jpg')

    @mute_logger('odoo.addons.shopify_csv_import.wizard.shopify_import_wizard', 'odoo.sql_db')
    def test_12_one_bad_product_does_not_break_batch(self):
        # 两个商品用同一个条码：Odoo 的条码唯一约束会让第二个失败，但第一个和第三个要照常导入
        wizard = self._run_import(self._csv(
            'bad-a,A,,,,,TRUE,Title,Default Title,,,BAD-A,,1,DUPBARCODE1,,,,,active',
            'bad-b,B,,,,,TRUE,Title,Default Title,,,BAD-B,,1,DUPBARCODE1,,,,,active',
            'bad-c,C,,,,,TRUE,Title,Default Title,,,BAD-C,,1,,,,,,active',
        ))
        self.assertEqual((wizard.created_count, wizard.error_count), (2, 1), wizard.import_log)
        self.assertIn('[失败] bad-b', wizard.import_log)
        self.assertTrue(self._tmpl('bad-a'))
        self.assertFalse(self._tmpl('bad-b'))
        self.assertTrue(self._tmpl('bad-c'))

    def test_13_rejects_non_shopify_csv(self):
        wizard = self.Wizard.create({'csv_file': base64.b64encode(b'foo,bar\n1,2\n')})
        with self.assertRaisesRegex(UserError, 'Handle'):
            wizard.action_import()

    def test_14_gbk_encoded_csv(self):
        csv_bytes = ('\n'.join([
            'Handle,Title,Variant Price',
            'gbk-mug,中文杯子,9.9',
        ]) + '\n').encode('gb18030')
        wizard = self._run_import(csv_bytes)
        self.assertEqual(wizard.created_count, 1, wizard.import_log)
        self.assertEqual(self._tmpl('gbk-mug').name, '中文杯子')

    def test_15_existing_no_variant_attribute_not_reused(self):
        # 后台已经有一个同名但"不生成变体"的属性时，导入必须另建一个会生成变体的属性
        self.env['product.attribute'].create({'name': SIZE, 'create_variant': 'no_variant'})
        self._run_import()
        cap = self._tmpl('sci-test-cap')
        self.assertEqual(len(cap.product_variant_ids), 3)
        self.assertEqual(cap.attribute_line_ids.attribute_id.create_variant, 'always')

    def test_16_process_images_now_returns_notification(self):
        wizard = self._run_import()
        self.Queue.search([]).write({'state': 'done'})
        action = wizard.action_process_images_now()
        self.assertEqual(action['tag'], 'display_notification')

    def test_17_variant_image_reusing_gallery_image(self):
        """真实 Shopify 导出里 Variant Image 基本都同时是某张 Image Src：
        画廊里要有这张图，变体上也要挂上它（以前按 URL 去重会把变体关联丢掉）。"""
        img = 'https://cdn.shopify.com/s/files/1/x/'
        csv_bytes = self._csv(
            f'vi-bag,Bag,,,,,TRUE,Color,Black,,,VI-B,,10,,{img}black.jpg,1,{img}black.jpg,,active',
            f'vi-bag,,,,,,,,White,,,VI-W,,10,,{img}white.jpg,2,{img}white.jpg,,',
            # 单变体商品上的 Variant Image 没有意义，不单独排队
            f'vi-one,One,,,,,TRUE,Title,Default Title,,,VI-1,,5,,{img}one.jpg,1,{img}one.jpg,,active',
        )
        wizard = self._run_import(csv_bytes)
        self.assertEqual(wizard.image_queue_count, 5, wizard.import_log)
        bag = self._tmpl('vi-bag')
        rows = self.Queue.search([('product_tmpl_id', '=', bag.id)])
        gallery = rows.filtered(lambda q: not q.product_variant_id)
        per_variant = rows - gallery
        self.assertEqual(sorted(gallery.mapped('role')), ['extra', 'main'])
        self.assertEqual(len(per_variant), 2)
        white = self._variant(bag, {'Color': 'White'})
        self.assertEqual(per_variant.filtered(lambda q: q.product_variant_id == white).source_url,
                         f'{img}white.jpg')
        one = self._tmpl('vi-one')
        self.assertFalse(self.Queue.search([('product_tmpl_id', '=', one.id), ('product_variant_id', '!=', False)]))

        # 重复导入不会再排队
        self.assertEqual(self._run_import(csv_bytes).image_queue_count, 0)
