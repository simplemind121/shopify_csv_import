# -*- coding: utf-8 -*-
{
    'name': 'Shopify CSV 商品导入',
    'version': '19.0.2.0.1',
    'category': 'Sales/Sales',
    'summary': '将 Shopify 导出的商品 CSV 一键导入到 Odoo 网站商城',
    'description': """
Shopify CSV 商品导入
====================

将 Shopify 后台标准导出的商品 CSV（Products export）一次性导入到 Odoo 19
website_sale（自建商城）：

- 按 Handle 分组解析多行 CSV（一个商品对应多行：变体行 + 额外图片行）
- 自动创建/更新商品基础信息、网站分类（多级）、内部分类、标签、供应商品牌标签
- 自动创建/复用变体属性（Option1/2/3），按属性组合把价格/成本/条码/SKU/重量
  写到对应的 product.product 变体上
- 以 Shopify 的 Handle 作为唯一键，重复导入 = 更新，不会重复建商品（幂等）
- 商品图片后台同步，进度可视：默认存成 Odoo 本地图片；在导入时选一个 media_picker
  已配置好、允许上传的图片源（media.source，Alist / S3）后，所有图片上传到对象存储做备份，
  前台先用 Shopify 的 CDN 链接显示，Shopify 链接失效后自动换成对象存储的 CDN 直链；
  主图由 media_picker 同步一份到本地，其余图片只走外链
- 图片台账：每张图的 Shopify 源 / 对象存储备份 / 本地图片状态、对账、中继上传、
  手动切换显示来源

需要 media_picker 19.0.3.8 或更高版本。

使用方法：

1. 安装本模块
2. 顶部菜单「Shopify 导入」（默认打开导入记录）→「导入 Shopify CSV」，上传 Shopify 导出的 products_export.csv
3. 点击「开始导入」，商品/变体/分类/标签会立即建好
4. 图片会进入「图片同步队列」，由后台定时任务在几分钟内陆续同步完成，
   也可以在向导里点「立即同步一批图片」手动触发
""",
    'author': 'Custom',
    'license': 'LGPL-3',
    'depends': ['website_sale', 'product', 'media_picker'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/shopify_import_wizard_views.xml',
        'views/shopify_import_batch_views.xml',
        'views/shopify_image_queue_views.xml',
        'views/shopify_import_menu.xml',
        'data/ir_cron.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'shopify_csv_import/static/src/batch_progress/*',
        ],
    },
    'pre_init_hook': 'pre_init_check_media_picker',
    'installable': True,
    'application': False,
    'auto_install': False,
}
