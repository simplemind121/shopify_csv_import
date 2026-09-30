# -*- coding: utf-8 -*-
# 仅供开发/CI 测试使用的 media_picker 替身（test double），不要部署到生产！
# 只实现了 shopify_csv_import 实际调用到的那一小部分接口：
#   - product.media.source（alist_url / alist_token / trusted_domains / _check_domain_trusted）
#   - models/pem_alist_client.get_file(source, path)
#   - media.bind + product.template.upsert_external_media_from_shopify / use_external_media
{
    'name': 'media_picker (TEST STUB)',
    'version': '19.0.0.0.1',
    'category': 'Hidden',
    'summary': 'Test double for media_picker - dev/CI only, never deploy',
    'depends': ['product'],
    'data': ['security/ir.model.access.csv'],
    'installable': True,
    'license': 'LGPL-3',
}
