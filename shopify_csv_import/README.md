# Shopify CSV 商品导入（Odoo 19）

把 Shopify 后台导出的商品 CSV（Products export）一次性导入到 Odoo 19
website_sale 自建商城。

## 安装

本模块依赖 `media_picker` **19.0.3.8 或更高版本**（用它的 `media.source` 上传接口、
外链健康检查、主图同步）。旧版 media_picker（3.4.x）请用本模块的 19.0.1.1.1。

1. 把这个 `shopify_csv_import` 文件夹整个复制到 Odoo 的 addons 目录
2. 重启 Odoo 容器，或在「设置 -> 技术 -> 应用」里点「更新应用列表」
3. 在「应用」里搜索「Shopify CSV 商品导入」，安装
4. 安装完成后顶部菜单会出现「Shopify 导入」

## 使用

1. 点顶部菜单「Shopify 导入」（打开的是「导入记录」列表），点左上角「导入 Shopify CSV」，上传 Shopify 导出的 CSV
2. 「图片备份到哪个对象存储」：选 media_picker 里已经配置好、并开启了「允许上传」的图片源
   （Alist / S3）。不选就是所有图片直接存成 Odoo 本地图片
3. 点「开始导入」，会进入这次导入的进度页面；商品和图片都在后台处理，可以关掉页面
4. 「Shopify 导入 -> 图片台账」可以看每张图片在各处的状态，并做上传、对账、切换显示来源

## 进度怎么看（关掉页面也没关系）

点「开始导入」后会生成一个**导入批次**并直接打开它的页面，商品导入和图片同步都在
后台定时任务里跑：

- 「Shopify 导入」（默认打开「导入记录」）能看到所有导入，随时点进去看进度
- 批次页面上的进度面板每 3 秒自动刷新：当前阶段、商品进度、图片进度条（已上传
  Alist / 本地存储 / CDN 失败回退本地 / 失败 / 待处理）、当前在做什么、速度、预计剩余、
  最近一次问题；定时任务被停用时会直接提示
- 按钮：暂停 / 继续、立即处理一段、重试失败图片、重新上传回退本地的图片（改好 Alist
  token 后用；上传成功会自动删掉当时存的本地副本）

## 重复导入 = 更新，不会重复建商品

导入逻辑用 Shopify 的 **Handle** 作为唯一键（存在 `product.template.x_shopify_handle`
字段上）。同一个 Handle 再导一次，会更新已有商品而不是新建一个。图片
同样按 Shopify 原始图片 URL 做去重键（`media.bind.shopify_media_id`），
重复导入不会传重复的图。

## 图片存储：接的是你现有的 media_picker，不是我自己另搭一套

选了对象存储之后，每张图这样处理：

1. 从 Shopify 下载**原图**，记下大小、SHA-256、像素尺寸
2. 通过 media_picker 的 `media.source.upload_media()` 上传到对象存储，记下它的 CDN 直链
   （同一个商品里指向同一张图的变体图 / 画廊图只传一次）
3. 前台显示用的链接（media_picker 的 `media.bind`）**先写 Shopify 的 CDN 链接**
4. 主图标成 `is_main`，media_picker 会自动把它同步进商品的本地图片（后台列表、订单、
   邮件等读本地图的地方都能用）；**其余图片不存本地**
5. 上传失败不影响显示（Shopify 链接还在），这张图标成「缺备份」，配置改好后点「补传」

**Shopify 链接失效后**：media_picker 的链接健康检查把某条 Shopify 外链判成 broken，或者
本模块对账时 Shopify 明确返回 404/410，就把这条外链换成对象存储的 CDN 直链。超时、
连接失败这类不算失效。没有备份可换的会标成「Shopify 已失效且无备份」。切过去之后不会
自动切回，需要时在台账里手动「显示改用 Shopify」。

没选对象存储：所有图片下载 1920px 版本后存成 Odoo 本地图片（`image_1920` / `product.image`）。

### 图片台账（三方对照）

「Shopify 导入 -> 图片台账」每张图一行：

| 列 | 含义 |
|---|---|
| Shopify 源 | 原图链接还在不在 |
| 对象存储备份 | 已备份 / 上传失败 / 备份文件不见了 / 大小不一致 |
| 本地图片 | 只有主图存本地 |
| 前台显示来源 | 现在显示的是 Shopify 链接还是对象存储链接 |
| 对账结论 | 一致 / 缺备份 / 备份丢失 / 不一致 / Shopify 已失效（备份已接替）/ 已失效且无备份 |

点开一行是三栏对照（Shopify 源、对象存储备份、Odoo 本地），各有预览图、大小、检查时间。

勾选图片后可以做的操作（都在后台执行，进度在导入记录页面看）：

- **上传到对象存储**：中继。Shopify 源还在就从 Shopify 拉原图；已经失效的主图改用本地那份
- **对账**：检查 Shopify 源和对象存储备份是否都在、备份大小和原图是否一致
- **重新拉取原图**：Shopify 那边换了图之后用，覆盖备份和显示链接
- **显示改用对象存储 / 显示改用 Shopify**：手动切换前台显示来源

导入记录页面上有批次级的按钮：补传缺备份的图片、对账、切换失效链接。

**设置填错了怎么办**：改好设置后**重新导入同一份 CSV** 就行——还没按新设置备份好的图片会被
新批次接手、用新的上传文件夹重新处理，已经备份好的不会重复上传。

**配置出错时的保护**：导入前会检查上传文件夹在 Alist 里有没有对应的存储；上传后会检查
返回的直链能不能打开；一个批次连续 5 张图上传失败会自动暂停并显示原因。上传文件夹填错了
可以在导入记录页面上改，再点「应用到未备份的图片」。

**图片源（media_picker 3.8）怎么配**：

- token 放在服务器的**环境变量**里，图片源的 Secret Reference 填**变量名**（不是 token 本身）。
  填成 token 本身时，选图浏览照常能用，但上传会被拒绝（permission denied）
- 「CDN strip path prefix」是生成 CDN 链接时要从路径里**去掉**的那一段（一般是 `/d`），
  不是上传文件夹
- Alist 根目录下挂了多个存储时，上传文件夹要带上存储的挂载路径，例如 `b2/shopify-products`

**用之前确认**：图片源要开启「允许上传」，token 要有写权限；如果 media_picker 配了全局
可信域名名单（系统参数 `media_picker.trusted_domains`），里面要有 `cdn.shopify.com`
（没有的话导入向导会直接提示）。

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
| Image Src（按 Image Position 排序） | 进图片台账，后台同步，见上文 |
| Variant Image | 少量有值的会挂到对应变体 |
| Tags | `product.tag`（和 Vendor 一起，脏数据也原样导入，你说了自己后台清理） |
| Gift Card / SEO Description / Google Shopping / 各类 metafields | 未使用（数据基本为空，用不上） |

## 测试

仓库根目录执行 `./dev/run_tests.sh`，会在一个临时 Odoo 19 数据库里跑
`tests/` 下的 80 个用例（CSV 解析、单/多变体、价格换算、分类、标签、重复导入、
单个商品失败隔离、图片队列、Alist/media_picker 链路，网络全部 mock）。
详见仓库根目录 README 的「运行测试」一节。

## 已知限制 / 后续可以优化的点

- 对象存储上传在真实的 Alist（B2）上跑通过（文件能传上去、对账能发现坏链接）；S3 类型的
  图片源还没实测。第一次用请先拿几个商品试，在图片台账里确认「对象存储备份」是「已备份」
- Shopify 失效切到对象存储后，media_picker 要重新同步一次主图，才会重新把本地主图和
  外链主图认成同一张；这之前主图可能在画廊里短暂出现两次
- 变体价格只能表达成"基础价 + 属性加价"，只有某一个组合单独加价的价格表会按最接近的
  结果导入并在导入日志里标 `[警告]`
- 划线价（Compare At Price）、税率（Variant Taxable）、库存没有映射
- Vendor 判定"是不是网址"用的是简单正则，CSV 里如果出现别的脏数据格式需要再补规则
- 对账比的是文件大小（备份和下载到的原图）；逐字节 / 视觉指纹的深度对账还没做
