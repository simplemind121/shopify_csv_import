# -*- coding: utf-8 -*-
"""升级到 19.0.1.1.0（新增导入批次）：把升级前就存在的图片队列记录归到一个
"历史导入"批次里，这样升级后也能在「导入批次」页面看到它们的同步进度。"""
import logging

from odoo import SUPERUSER_ID, api, fields

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Queue = env['shopify.image.queue']
    orphans = Queue.search([('batch_id', '=', False)])
    if not orphans:
        return
    sources = orphans.mapped('media_source_id')
    products = orphans.mapped('product_tmpl_id')
    first = min(orphans.mapped('create_date'))
    pending = any(q.state == 'pending' for q in orphans)
    batch = env['shopify.import.batch'].create({
        'name': '升级前的导入（历史记录）',
        'media_source_id': sources[:1].id if len(sources) == 1 else False,
        'state': 'syncing' if pending else 'done',
        'total_products': len(products),
        'next_index': len(products),
        'started_at': first,
        'products_done_at': first,
        'images_started_at': fields.Datetime.now(),
        'import_log': f'升级到 19.0.1.1.0 前导入的 {len(products)} 个商品 / {len(orphans)} 张图片。'
                      f'商品导入的详细日志在旧版本里没有保存。',
        'status_message': '升级前的图片记录，已归入本批次',
    })
    orphans.write({'batch_id': batch.id})
    batch._refresh_image_state()
    _logger.info('shopify_csv_import: %s 条历史图片记录归入批次 %s', len(orphans), batch.id)
