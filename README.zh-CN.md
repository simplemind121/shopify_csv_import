# shopify_csv_import

**Odoo 19 · 把 Shopify 导出的商品 CSV 一键导入 `website_sale`（自建商城）· 可选接入 `media_picker` 走 Alist/B2 CDN 图片**

| 项目 | 内容 |
|---|---|
| **最新版本** | [`19.0.1.1.1`](./CHANGELOG.md#19111---2026-10-01) |
| **模块技术名** | `shopify_csv_import` |
| **Odoo** | 19 社区版 |
| **依赖** | `website_sale`、`product`、`media_picker` |
| **模块许可证** | LGPL-3（见包内 `__manifest__.py`） |
| **仓库性质** | 开源——完整模块源码 + 部署脚本 |

English: [README.md](./README.md) · 版本锚点：[`VERSION`](./VERSION) · 完整变更记录：[`CHANGELOG.md`](./CHANGELOG.md) · 部署手册：[`docs/DEPLOY.md`](./docs/DEPLOY.md) · 安全说明：[`SECURITY.md`](./SECURITY.md)

---

## 这个模块做什么

Shopify 后台导出的商品 CSV 是一个 60 多列、一个商品对应多行（变体行+
额外图片行）的文件。`shopify_csv_import` 直接在 Odoo 里解析这份 CSV，
落成 `website_sale` 商城里真正的 `product.template` / `product.product`
记录——不用手动拆表格，不用在 Odoo 自带的通用 Import 界面里一个个对字段。

设计上是配合 `media_picker` 做图片 CDN 用的，但没有 `media_picker` 也能用
（会自动退化成 Odoo 标准二进制图片存储）。

### 目标

- 一键导入整份 Shopify 商品目录：商品、变体、分类、标签、价格
- 同一份 CSV 重复导入 = **更新**已有商品（按 Shopify 的 `Handle` 匹配），
  不会重复建
- 图片同步不会拖垮导入请求本身，可选直接接到你现有的 Alist/B2 CDN
- 不重新造"图片怎么显示"这个轮子——CDN 图片走的是 `media_picker` 自己
  那套已经在生产环境验证过的 `media.bind` 画廊逻辑

### 不做什么

- 不是和 Shopify 的持续双向同步，是一次性/可重复执行的**导入**，不是
  实时连接器
- 不做订单/库存同步
- 不会对 Shopify 做任何写操作——只读你给它的那份 CSV 文件

---

## 仓库里有什么

| 路径 | 说明 |
|---|---|
| `shopify_csv_import/` | 可安装的 Odoo 模块源码 |
| `shopify_csv_import-<版本号>.zip`（Release 附件） | 可安装压缩包，挂在 GitHub Releases 上 |
| `deploy_shopify_csv_import.sh` | 交互式、部署前备份+部署后校验的安装/升级脚本，适配 Docker VPS |
| `VERSION` | 当前推荐版本号（单行） |
| `CHANGELOG.md` | 面向用户的变更历史 |
| `docs/` | 部署 / 运维说明 |
| `shopify_csv_import/tests/` | Odoo 测试用例 + 一份 Shopify 导出 CSV 样例 |
| `dev/run_tests.sh` | 在临时 Odoo 19 数据库上跑测试 |
| `dev/stub_addons/media_picker/` | media_picker 接口的测试替身——**只用于开发/CI，不要部署** |

---

## 功能矩阵（当前版本 19.0.1.1.1）

| 功能 | 状态 |
|---|---|
| 商品/变体/价格/成本/SKU/条码/重量导入 | 支持 |
| 变体独立售价 | 支持，换算成 Odoo 的"基础价 + 属性加价"；无法这样表达的价格表会在导入日志里给出警告 |
| 多级网站分类 + 内部分类映射 | 支持 |
| Vendor → 品牌标签（过滤网址垃圾数据）+ Shopify Tags → 标签 | 支持 |
| 幂等重复导入（按 Handle 去重） | 支持 |
| 后台导入批次 + 实时进度页面（暂停 / 继续 / 重试） | 支持 |
| 基于 `ir.cron` 队列的异步图片同步 | 支持 |
| 二进制图片兜底（没配 Alist 时） | 支持 |
| 经 `media_picker` 的 `media.bind` 走 Alist/B2 CDN 图片同步 | 支持 |
| 划线原价 / 礼品卡 / SEO metafields | 未映射（当前场景用不上） |
| 超大商品目录的分批提交 | 支持（按时间预算分段执行，可续跑） |
| 在真实 Odoo 19 数据库上的自动化测试 | 有——53 个用例，并用真实的 284 个商品的 Shopify 导出实测过，见下文 |

---

## 快速开始

```bash
# 1) clone 这个仓库，或者从 Release 里下载：
#    shopify_csv_import-19.0.1.1.1.zip
#    deploy_shopify_csv_import.sh

# 2) 两个文件放同一目录，在 VPS 上执行
chmod +x deploy_shopify_csv_import.sh
sudo ./deploy_shopify_csv_import.sh --target staging   # 先在 staging 测
sudo ./deploy_shopify_csv_import.sh                     # 确认没问题再上生产

# 3) Odoo 后台 → 顶部菜单「Shopify 导入」→ 按钮「导入 Shopify CSV」→ 上传你的导出文件
#    之后会进入这次导入的进度页面；关掉也没关系，再点「Shopify 导入」就能看到所有导入记录
```

完整流程、参数说明、回滚：**[docs/DEPLOY.md](./docs/DEPLOY.md)**。

---

## 图片这条链路是怎么走的

1. 导入向导把 CSV 里每张图片的 URL 存进一条 `shopify.image.queue` 记录
   （主图 / 附加图片 / 变体图片）。
2. `ir.cron` 定时任务小批量处理这个队列，避免上千张图片把发起导入的那次
   HTTP 请求拖到超时。
3. 如果向导里选了一个 `product.media.source`（`media_picker` 里已经配好
   的 Alist 连接）：下载图片 → `PUT` 上传到 Alist → 用 `media_picker` 自带的
   `pem_alist_client.get_file` 解析出可信直链 → 交给
   `product.template.upsert_external_media_from_shopify`——这正是
   `media_picker` 自己为 Shopify 类型导入定义的接入点。
4. 否则（或者某张图第 3 步失败），直接兜底存成 Odoo 标准二进制图片，
   保证商品目录不会因为这个环节而缺图。

这个模块里没有任何 QWeb 模板 override——商城前台怎么显示图片，完全是
`media_picker` 自己那套已经验证过的代码。

---

## 运行测试

```bash
./dev/run_tests.sh                 # Docker：临时起 postgres:16 + odoo:19.0，跑完自动删除
```

拉不到 Docker Hub 镜像时，可以指向本机的 Odoo 19 源码：

```bash
ODOO_SRC=~/src/odoo-19 PYTHON=~/venvs/odoo19/bin/python \
DB_HOST=127.0.0.1 DB_PORT=5432 DB_USER=odoo DB_PASSWORD=odoo ./dev/run_tests.sh
```

测试会把本模块和 `dev/stub_addons/media_picker`（media_picker 接口的测试替身，
**只用于开发/CI，不要部署**；想用真实模块测就设 `MEDIA_PICKER_DIR=/path/to/media_picker`）
一起装进一个临时数据库，所有网络调用（下载 Shopify 图片、Alist 上传、`get_file`）
都是 mock 的。GitHub Actions 每次 push / PR 都会跑同一个脚本。

---

## 版本号规则

跟 Odoo 一样的五段式版本号：`19.0.<minor>.<patch>.<hotfix>`。

**升级方式：**始终用部署脚本执行 `-u shopify_csv_import`。手动
`docker exec odoo -u ...` 也能跑，但会跳过脚本自带的备份/校验步骤。

---

## 兼容性

| 组件 | 要求 |
|---|---|
| Odoo | 19.0 社区版，已装 `website_sale` |
| 必需依赖 | `media_picker`（本模块调的是它的 `media.bind` / `product.media.source` 接口；就算你一直不选图片源、图片走二进制兜底，装模块时 `media_picker` 这个插件本身也必须已经在 addons 目录里） |

---

## 安全

- 本模块不存储任何 Alist/S3/B2 凭证——详见 [`SECURITY.md`](./SECURITY.md)
- 发现问题请私下反馈，不要开公开 issue

---

## 许可证

模块许可证：**LGPL-3**（声明在 Odoo manifest 里）。仓库里的部署脚本按现状提供。
