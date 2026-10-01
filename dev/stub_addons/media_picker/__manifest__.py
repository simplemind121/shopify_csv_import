# -*- coding: utf-8 -*-
# 仅供开发/CI 测试使用的 media_picker 替身（test double），不要部署到生产！
# 模拟的是 media_picker 19.0.3.8.x 里 shopify_csv_import 实际用到的那一小部分接口：
#   - media.source（upload_enabled / root_path / trusted_domains / upload_media / _check_domain_trusted）
#   - media.bind（url / is_main / source_id / source_ref / shopify_media_id / 健康检查字段 /
#     可信域名约束 / upsert_external_media_from_shopify）
#   - product.template（use_external_media / external_media_mode / media_bind_ids /
#     mp_main_sync_status / upsert_external_media_from_shopify）
{
    'name': 'media_picker (TEST STUB)',
    'version': '19.0.3.8.0',
    'category': 'Hidden',
    'summary': 'Test double for media_picker 3.8 - dev/CI only, never deploy',
    'author': 'shopify_csv_import tests',
    'depends': ['product'],
    'data': ['security/ir.model.access.csv'],
    'installable': True,
    'license': 'LGPL-3',
}
