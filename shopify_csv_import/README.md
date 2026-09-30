# Shopify CSV 商品导入（Odoo 19）

把 Shopify 后台导出的商品 CSV（Products export）一次性导入到 Odoo 19
website_sale 自建商城。

## 安装

本模块依赖你现有的 `media_picker`（19.0.3.3.0），要先装 `media_picker`
再装本模块。

1. 把这个 `shopify_csv_import` 文件夹整个复制到
   `/opt/prod-apps/odoo/addons/`（对应容器内的 `/mnt/extra-addons`）
2. 重启 Odoo 容器，或在「设置 -> 技术 -> 应用」里点「更新应用列表」
3. 在「应用」里搜索「Shopify CSV 商品导入」，安装
4. 安装完成后顶部菜单会出现「Shopify 导入」

## 使用

1. 「Shopify 导入」->「导入商品 CSV」，上传 Shopify 导出的 CSV
2. 想让图片直接走你的 Alist/B2 CDN，就在「图片存到哪个 Alist 图片源」里选
   你后台已经配置好的那个 `product.media.source` 记录（就是你现在
   media_picker 后台里手动选图用的那个连接）；不选就是所有图片直接存成
   Odoo 本地二进制图片，两种都能正常显示，CDN 只是可选项
3. 点「开始导入」——几秒到几十秒内 284 个商品的基础信息、变体、分类、
   标签都会建好，可以立刻去「网站 -> 电商」里看
4. 图片不会在这一步里同步完（避免请求超时），而是进入「图片同步队列」，
   后台定时任务每 2 分钟处理一批（默认 30 张）；也可以在导入结果页点
   「立即同步一批图片」手动催一下
5. 「Shopify 导入」->「图片同步队列」可以看每张图片的同步状态，失败的
   点开记录点「重试」

## 重复导入 = 更新，不会重复建商品

导入逻辑用 Shopify 的 **Handle** 作为唯一键（存在 `product.template.x_shopify_handle`
字段上）。同一个 Handle 再导一次，会更新已有商品而不是新建一个。图片
同样按 Shopify 原始图片 URL 做去重键（`media.bind.shopify_media_id`），
重复导入不会传重复的图。

## 图片存储：接的是你现有的 media_picker，不是我自己另搭一套

上一版我曾经打算自己加字段存外链 URL，但发现你这个 media_picker 包
（19.0.3.3.0）里已经有专门给 Shopify 导入用的接口，所以现在直接复用它，
没有再造轮子：

- 选了「Alist 图片源」之后，每张图片会：下载字节 → `PUT` 到 Alist（
  `models/shopify_image_queue.py` 里的 `_alist_put`，这是本模块唯一
  自己写的网络调用，因为 media_picker 本身只有"挑选已存在文件"，没有
  上传） → 用 media_picker 自带、已经在生产验证过的 `pem_alist_client.get_file`
  解析出最终直链，并用该图片源自己的 `_check_domain_trusted()` 校验域名
  → 调 `product.template.upsert_external_media_from_shopify(...)`
  写入 `media.bind`，并把 `use_external_media` 置为 `True`
- 网站商品页怎么显示外链图片，完全是 media_picker 自己现成的逻辑
  （`_get_images` / `_get_website_main_image_source` 等），本模块不用碰
  任何 QWeb 模板
- 没选图片源，或者上传/解析失败，就直接下载存成 Odoo 标准二进制图片
  （`image_1920` / `product.image`），保证图片总归能正常显示

**用之前确认一下**：你选的那个 `product.media.source` 记录上的
`trusted_domains` 字段（或者全局系统参数 `media_picker.trusted_domains`）
要包含 `media.051288888.xyz`，不然域名校验会失败，自动回退成本地二进制——
这个如果你现有商品图片已经在正常显示外链，大概率已经配好了，不用改。

## CSV 字段映射（供核对）

| Shopify 字段 | Odoo 字段 / 处理方式 |
|---|---|
| Handle | `x_shopify_handle`（唯一键） |
| Title | `name` |
| Body (HTML) | `description_ecommerce`（网站商品描述） |
| Vendor | 过滤掉网址格式（1688/alibaba/http），其余当 `product.tag` 品牌标签 |
| Product Category | 按 `>` 拆分层级，自动建/复用 `product.public.category`（网站分类）+ `product.category`（内部分类） |
| Published | `website_published` |
| Status | `x_shopify_status`（仅记录，不影响上架逻辑） |
| Option1/2/3 Name+Value | 属性名取该商品第一行（Shopify 只在第一行写 Name），自动建/复用 `product.attribute`（仅复用"生成变体"类型的同名属性）+ `product.attribute.value`，生成变体 |
| Variant SKU | `default_code` |
| Variant Price | `list_price`（单变体）；多变体：模板 `list_price` = 最低价，其余差价写成属性值加价 `price_extra`（Odoo 标准的变体定价方式）。Shopify 里只有某一个组合单独加价、无法拆成"属性加价"的，会按最接近的结果导入并在导入日志里标 `[警告]` |
| Variant Compare At Price | 未使用（按你的要求跳过） |
| Cost per item | `standard_price` |
| Variant Barcode | `barcode` |
| Variant Grams | `weight`（换算 kg） |
| Image Src（按 Image Position 排序） | 进图片同步队列，见上文 |
| Variant Image | 少量有值的会挂到对应变体 |
| Tags | `product.tag`（和 Vendor 一起，脏数据也原样导入，你说了自己后台清理） |
| Gift Card / SEO Description / Google Shopping / 各类 metafields | 未使用（数据基本为空，用不上） |

## 测试

仓库根目录执行 `./dev/run_tests.sh`，会在一个临时 Odoo 19 数据库里跑
`tests/` 下的 26 个用例（CSV 解析、单/多变体、价格换算、分类、标签、重复导入、
单个商品失败隔离、图片队列、Alist/media_picker 链路，网络全部 mock）。
详见仓库根目录 README 的「运行测试」一节。

## 已知限制 / 后续可以优化的点

- **上传接口是我按 Alist v3 文档写的（`PUT /api/fs/put`，Header 带
  `File-Path` + `Authorization`），没在真实环境跑过**——media_picker 自己
  的代码里只有读（list/get），没有现成的上传可以照抄。如果你的 Alist
  版本这个接口不一样，第一次导入图片会在「图片同步队列」里显示上传失败，
  改 `models/shopify_image_queue.py` 的 `_alist_put` 方法就行，改完不影响
  已经导入的商品/变体数据
- 每个商品的导入用了 savepoint（`with self.env.cr.savepoint()`），单个
  商品失败不会拖垮整批；但没有做分页/分批 commit，284 个商品量级没问题，
  以后 CSV 涨到几千个商品建议改成分批 commit
- Vendor 判定"是不是网址"用的是简单正则（含 `http`/`www.`/`1688.com`/
  `alibaba.com`），CSV 里如果出现别的脏数据格式需要再补规则
- 图片同步失败（比如 Shopify 图片链接过期，或者上面说的 Alist 接口对不上）
  会停在「图片同步队列」里显示失败原因，需要人工看一下要不要重试或者换图
