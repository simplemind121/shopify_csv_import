# -*- coding: utf-8 -*-
{
    'name': 'Shopify CSV 商品导入',
    'version': '19.0.1.0.1',
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
- 商品图片异步同步（ir.cron 队列）：默认直接存成 Odoo 标准二进制图片；
  在导入向导里选一个 media_picker 已配置好的 Alist 图片源（product.media.source）
  后，自动改为"下载后转存至 Alist/B2，写入 media.bind 外链画廊"，直接复用
  media_picker 模块自带的 Shopify 导入接入点
  （product.template.upsert_external_media_from_shopify），网站商品页
  会自动走 CDN 直链显示

使用方法：

1. 安装本模块
2. 顶部菜单「Shopify 导入」→「导入商品 CSV」，上传 Shopify 导出的 products_export.csv
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
        'views/shopify_image_queue_views.xml',
        'views/shopify_import_menu.xml',
        'data/ir_cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
