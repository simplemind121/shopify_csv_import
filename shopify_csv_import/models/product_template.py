# -*- coding: utf-8 -*-
from odoo import models, fields


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # Shopify 的 Handle 是商品的唯一标识，用它做导入去重/更新的键
    x_shopify_handle = fields.Char(
        string='Shopify Handle',
        copy=False,
        index=True,
        help='来自 Shopify CSV 的 Handle 字段，导入模块用它判断新建还是更新商品。',
    )
    x_shopify_status = fields.Char(
        string='Shopify 状态',
        copy=False,
        help='Shopify 原始的 Status 字段（active/draft/unlisted），仅作记录，'
             '实际是否在网站发布以 Published 字段（website_published）为准。',
    )
