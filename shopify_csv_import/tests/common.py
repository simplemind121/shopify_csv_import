# -*- coding: utf-8 -*-
import base64
import io
import os
from unittest.mock import MagicMock, patch

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

    # ------------------------------------------------------------------
    # 对象存储（media_picker 的 media.source）
    # ------------------------------------------------------------------
    def _media_source(self, **vals):
        """一个开启了上传的图片源。用 mock 类型：真实 media_picker 和测试替身都有。"""
        return self.env['media.source'].create(dict({
            'name': 'Test Object Storage', 'source_type': 'mock', 'upload_enabled': True,
            'trusted_domains': 'media.example.com',
        }, **vals))

    def _mock_upload(self, fail=None, link_problem=False):
        """替换 media.source.upload_media：不发任何网络请求，记录收到的参数。
        上传后的直链检查也一起替换掉（默认直链可用；link_problem 传问题描述）。"""
        self.uploads = []
        link_check = patch.object(type(self.env['shopify.image.queue']), '_check_backup_link',
                                  lambda self_, url, size: link_problem)
        link_check.start()
        self.addCleanup(link_check.stop)

        def fake_upload(source, folder, filename, stream, size=None, content_type=None, timeout=None):
            data = stream.read()
            self.uploads.append({'folder': folder, 'filename': filename, 'bytes': data,
                                 'size': size, 'content_type': content_type})
            if fail:
                raise Exception(fail)
            path = f"{(folder or '').strip('/')}/{filename}"
            return {'url': f'https://media.example.com/{path}', 'source_ref': f'/{path}',
                    'name': filename, 'media_type': 'image'}
        return patch.object(type(self.env['media.source']), 'upload_media', autospec=True,
                            side_effect=fake_upload)
