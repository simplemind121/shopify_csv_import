# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.addons.shopify_csv_import.models.shopify_image_queue import SHOPIFY_CDN_HOSTS
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
        'media.source', string='图片备份到哪个对象存储',
        domain="[('upload_enabled', '=', True)]",
        help='media_picker 里已经配置好、并开启了「允许上传」的图片源（Alist / S3）。\n'
             '选了之后：所有图片上传到这个对象存储做备份；前台先用 Shopify 的 CDN 链接显示，'
             'Shopify 链接失效后自动换成对象存储的 CDN 直链；主图另外同步一份到 Odoo 本地。\n'
             '留空：所有图片直接存成 Odoo 本地图片，不走外链。')
    alist_upload_path_prefix = fields.Char(
        string='上传文件夹', default='shopify-products',
        help='图片源根目录下的文件夹。图片会存成 "文件夹/商品ID_文件名"，'
             '例如 shopify-products/123_foo.jpg。')

    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError('请先选择要导入的 Shopify CSV 文件。')
        Batch = self.env['shopify.import.batch']
        # 先在前台把文件解析一遍：编码不对 / 不是 Shopify 导出，马上报错，不用等后台
        groups = Batch._group_rows_by_handle(Batch._read_csv_rows(self.csv_file))
        if not groups:
            raise UserError('CSV 里没有任何带 Handle 的商品行。')
        self._check_media_source()

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

    def _check_media_source(self):
        """选了对象存储时，开始前先把会让每张图都失败的配置问题挡下来。"""
        source = self.media_source_id
        if not source:
            return
        if not source.upload_enabled:
            raise UserError(
                f'图片源「{source.display_name}」没有开启「允许上传」，图片传不上去。'
                f'请先在 media_picker 的图片源设置里开启。')
        # 前台先用 Shopify 的链接显示。media_picker 配了全局可信域名名单时，
        # 名单里必须有 Shopify 的 CDN 域名，否则这些外链写不进去。
        raw = self.env['ir.config_parameter'].sudo().get_param('media_picker.trusted_domains', '') or ''
        trusted = [d.strip().lower() for d in raw.replace(',', '\n').splitlines() if d.strip()]
        missing = [h for h in SHOPIFY_CDN_HOSTS
                   if trusted and not any(h == d or h.endswith('.' + d) for d in trusted)]
        if missing:
            raise UserError(
                'media_picker 的可信域名名单（系统参数 media_picker.trusted_domains）里没有 '
                f'{", ".join(missing)}，Shopify 的图片链接会被拒绝。请先把它加进名单。')
