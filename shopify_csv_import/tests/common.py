# -*- coding: utf-8 -*-
import base64
import io
import os
from unittest.mock import MagicMock

from odoo.tests.common import TransactionCase

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
SAMPLE_CSV = os.path.join(DATA_DIR, 'products_export_sample.csv')

HEADER = (
    'Handle,Title,Body (HTML),Vendor,Product Category,Tags,Published,'
    'Option1 Name,Option1 Value,Option2 Name,Option2 Value,Variant SKU,Variant Grams,'
    'Variant Price,Variant Barcode,Image Src,Image Position,Variant Image,Cost per item,Status'
)


def png_bytes(color=(255, 0, 0)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), color).save(buf, format='PNG')
    return buf.getvalue()


def fake_response(content=b'', content_type='image/png', json_data=None, status=200):
    resp = MagicMock()
    resp.content = content
    resp.headers = {'Content-Type': content_type}
    resp.status_code = status
    resp.json.return_value = json_data or {}
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f'HTTP {status}')
    else:
        resp.raise_for_status.return_value = None
    return resp


class ShopifyImportCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['shopify.import.wizard']
        cls.Template = cls.env['product.template']
        cls.Queue = cls.env['shopify.image.queue']

    def _start_import(self, csv_bytes=None, **wizard_vals):
        """走向导建批次（和用户在界面上点「开始导入」一样），返回批次，但还没执行。"""
        if csv_bytes is None:
            with open(SAMPLE_CSV, 'rb') as f:
                csv_bytes = f.read()
        wizard = self.Wizard.create(dict(
            csv_file=base64.b64encode(csv_bytes), csv_filename='products_export.csv', **wizard_vals))
        action = wizard.action_import()
        self.assertEqual(action['res_model'], 'shopify.import.batch')
        return self.env['shopify.import.batch'].browse(action['res_id'])

    def _run_import(self, csv_bytes=None, **wizard_vals):
        """建批次并把商品导入阶段跑完（相当于后台任务执行完一轮），返回批次。"""
        batch = self._start_import(csv_bytes, **wizard_vals)
        self.assertTrue(batch._run_products())
        return batch

    @staticmethod
    def _csv(*lines):
        return ('\n'.join((HEADER,) + lines) + '\n').encode('utf-8')

    def _tmpl(self, handle):
        return self.Template.search([('x_shopify_handle', '=', handle)])

    def _variant(self, tmpl, combo):
        for variant in tmpl.product_variant_ids:
            have = {
                (ptav.attribute_id.name, ptav.product_attribute_value_id.name)
                for ptav in variant.product_template_attribute_value_ids
            }
            if set(combo.items()) <= have:
                return variant
        self.fail(f'variant {combo} not found on {tmpl.display_name}')
