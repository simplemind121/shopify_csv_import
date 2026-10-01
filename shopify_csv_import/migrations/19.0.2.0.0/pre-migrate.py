# -*- coding: utf-8 -*-
"""升级到 19.0.2.0.0：图片源从旧版 media_picker 的 product.media.source 换成 3.8 的
media.source。旧字段里存的是 product.media.source 的 id，不能当成 media.source 的 id 用，
所以先把旧列改名留档，让 ORM 重新建一列空的。"""
import logging

_logger = logging.getLogger(__name__)

TABLES = ('shopify_image_queue', 'shopify_import_batch')


def migrate(cr, version):
    for table in TABLES:
        cr.execute("""
            SELECT ccu.table_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
             WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name = %s
               AND kcu.column_name = 'media_source_id'
        """, (table,))
        row = cr.fetchone()
        if not row or row[0] == 'media_source':
            continue  # 没有这一列，或者已经指向新模型
        cr.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s "
                   "AND column_name = 'legacy_media_source_id'", (table,))
        if cr.fetchone():
            cr.execute(f'ALTER TABLE {table} DROP COLUMN media_source_id')
        else:
            cr.execute(f'ALTER TABLE {table} RENAME COLUMN media_source_id TO legacy_media_source_id')
        _logger.info('shopify_csv_import: %s.media_source_id（旧 product.media.source）已改名留档', table)
