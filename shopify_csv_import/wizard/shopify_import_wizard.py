# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError


class ShopifyImportWizard(models.TransientModel):
    """上传 CSV → 校验 → 建一个导入批次，交给后台执行。

    真正的导入逻辑在 shopify.import.logic 里，由 shopify.import.batch 在后台定时任务中
    分段执行；这里只负责收文件、提前挡掉不是 Shopify 导出的文件。
    """
    _name = 'shopify.import.wizard'
    _description = 'Shopify CSV 商品导入向导'

    csv_file = fields.Binary(string='Shopify 商品导出 CSV', required=True)
    csv_filename = fields.Char(string='文件名')

    media_source_id = fields.Many2one(
        'product.media.source', string='图片存到哪个 Alist 图片源',
        help='选了之后，图片会下载后上传到这个 Alist 连接（media_picker 里已经配置'
             '并测试过的 product.media.source），存到 media.bind 外链画廊，网站'
             '直接读 CDN 直链。留空则所有图片直接存成 Odoo 本地二进制图片。')
    alist_upload_path_prefix = fields.Char(
        string='Alist 上传路径前缀', default='/b2/shopify-products',
        help='图片会上传到 "这个前缀/商品ID_文件名"，例如 /b2/shopify-products/123_foo.jpg。')

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError('请先选择要导入的 Shopify CSV 文件。')
        Batch = self.env['shopify.import.batch']
        # 先在前台把文件解析一遍：编码不对 / 不是 Shopify 导出，马上报错，不用等后台
        groups = Batch._group_rows_by_handle(Batch._read_csv_rows(self.csv_file))
        if not groups:
            raise UserError('CSV 里没有任何带 Handle 的商品行。')

        batch = Batch.create({
            'name': f'{self.csv_filename or "Shopify CSV"} · '
                    f'{fields.Datetime.context_timestamp(self, fields.Datetime.now()):%Y-%m-%d %H:%M}',
            'csv_file': self.csv_file,
            'csv_filename': self.csv_filename,
            'media_source_id': self.media_source_id.id,
            'alist_upload_path_prefix': self.alist_upload_path_prefix,
            'total_products': len(groups),
            'status_message': f'已排队：{len(groups)} 个商品，后台任务马上开始处理',
        })
        batch._trigger_batch_run()
        return {
            'type': 'ir.actions.act_window',
            'name': '导入记录',
            'res_model': 'shopify.import.batch',
            'res_id': batch.id,
            'view_mode': 'form',
            'target': 'current',
        }
