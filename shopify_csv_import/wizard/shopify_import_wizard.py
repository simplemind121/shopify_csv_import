# -*- coding: utf-8 -*-
import base64
import csv
import io
import logging
import re
from collections import OrderedDict

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round

_logger = logging.getLogger(__name__)

# Vendor 字段里出现网址（1688/阿里巴巴等脏数据）就不当品牌标签导入
VENDOR_URL_RE = re.compile(r'https?://|www\.|1688\.com|alibaba\.com', re.IGNORECASE)

OPTION_PAIRS = (
    ('Option1 Name', 'Option1 Value'),
    ('Option2 Name', 'Option2 Value'),
    ('Option3 Name', 'Option3 Value'),
)


class ShopifyImportWizard(models.TransientModel):
    _name = 'shopify.import.wizard'
    _description = 'Shopify CSV 商品导入向导'

    csv_file = fields.Binary(string='Shopify 商品导出 CSV', required=True)
    csv_filename = fields.Char(string='文件名')

    media_source_id = fields.Many2one(
        'product.media.source', string='图片存到哪个 Alist 图片源',
        help='选了之后，图片会下载后上传到这个 Alist 连接（media_picker 里已经配置'
             '并测试过的 product.media.source），存到 media.bind 外链画廊，网站'
             '直接读 CDN 直链。留空则所有图片直接存成 Odoo 本地二进制图片。')
    alist_upload_path_prefix = fields.Char(
        string='Alist 上传路径前缀', default='/b2/shopify-products',
        help='图片会上传到 "这个前缀/商品ID_文件名"，例如 /b2/shopify-products/123_foo.jpg。')

    state = fields.Selection([
        ('draft', '待导入'),
        ('done', '已完成'),
    ], default='draft')

    created_count = fields.Integer(string='新建商品数', readonly=True)
    updated_count = fields.Integer(string='更新商品数', readonly=True)
    error_count = fields.Integer(string='失败商品数', readonly=True)
    image_queue_count = fields.Integer(string='排队待同步图片数', readonly=True)
    import_log = fields.Text(string='导入日志', readonly=True)

    # =================================================================
    # 入口
    # =================================================================
    def action_import(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError('请先选择要导入的 Shopify CSV 文件。')

        rows = self._read_csv_rows(self.csv_file)
        groups = self._group_rows_by_handle(rows)

        log_lines = []
        created = 0
        updated = 0
        errors = 0
        image_queue_total = 0

        for handle, group_rows in groups.items():
            try:
                with self.env.cr.savepoint():
                    tmpl, is_new, n_images, warnings = self._import_one_product(handle, group_rows)
                image_queue_total += n_images
                if is_new:
                    created += 1
                    log_lines.append(f'[新建] {handle} -> {tmpl.name}（排队图片 {n_images} 张）')
                else:
                    updated += 1
                    log_lines.append(f'[更新] {handle} -> {tmpl.name}（排队图片 {n_images} 张）')
                log_lines += [f'[警告] {handle}: {w}' for w in warnings]
            except Exception as e:
                errors += 1
                _logger.exception('导入商品失败: handle=%s', handle)
                log_lines.append(f'[失败] {handle}: {e}')

        self.write({
            'state': 'done',
            'created_count': created,
            'updated_count': updated,
            'error_count': errors,
            'image_queue_count': image_queue_total,
            'import_log': '\n'.join(log_lines),
        })

        return {
            'type': 'ir.actions.act_window',
            'name': '导入 Shopify 商品',
            'res_model': 'shopify.import.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_process_images_now(self):
        """手动立即处理一批排队图片，方便导入后马上看到效果，不用等 cron。"""
        self.ensure_one()
        Queue = self.env['shopify.image.queue']
        # _cron_process_pending 自己会按请求超时时间控制处理时长，不会拖到网页超时
        processed = Queue._cron_process_pending(limit=50)
        remaining = Queue.search_count([('state', '=', 'pending')])
        failed = Queue.search_count([('state', '=', 'error')])
        # 注意：不能用 raise UserError 来显示结果——UserError 会让整个请求事务回滚，
        # 刚处理完的图片也会跟着被撤销。这里改成返回一个前端通知。
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': '图片同步',
                'message': f'本次处理 {processed} 张图片；剩余待处理 {remaining} 张，'
                           f'累计失败 {failed} 张（剩余的会由后台任务每几分钟继续处理）。',
                'type': 'warning' if failed else 'success',
                'sticky': False,
            },
        }

    # =================================================================
    # CSV 读取 / 分组
    # =================================================================
    @staticmethod
    def _read_csv_rows(csv_file_b64):
        raw = base64.b64decode(csv_file_b64)
        # Shopify 导出通常是 UTF-8（可能带 BOM），用 utf-8-sig 兼容两种情况；
        # 被 Excel 另存过的文件可能变成 GBK，再兜底试一次。
        for encoding in ('utf-8-sig', 'gb18030'):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise UserError('无法识别 CSV 文件编码，请用 Shopify 后台原始导出的 UTF-8 文件。')
        reader = csv.DictReader(io.StringIO(text, newline=''))
        header = [h.strip() for h in (reader.fieldnames or [])]
        if 'Handle' not in header:
            raise UserError('这不是 Shopify 商品导出 CSV：表头里没有 "Handle" 列。')
        reader.fieldnames = header
        return list(reader)

    @staticmethod
    def _group_rows_by_handle(rows):
        groups = OrderedDict()
        for row in rows:
            handle = (row.get('Handle') or '').strip()
            if not handle:
                continue
            groups.setdefault(handle, []).append(row)
        return groups

    # =================================================================
    # 单个商品导入
    # =================================================================
    def _import_one_product(self, handle, rows):
        main_row = rows[0]

        # 变体行：带 Option 值或者 Variant Price 的行
        variant_rows = [
            r for r in rows
            if (r.get('Variant Price') or '').strip()
            or any((r.get(v) or '').strip() for _, v in OPTION_PAIRS)
        ]
        # 图片行：任何带 Image Src 的行（可能和变体行是同一行）
        image_rows = [r for r in rows if (r.get('Image Src') or '').strip()]

        # Shopify 只在每个商品的第一行写 "Option1 Name" 等属性名，后续变体行只有
        # "Option1 Value"。所以属性名要从整组行里取，而不能逐行读。
        option_names = self._option_names(rows)
        if self._is_default_variant_only(option_names, variant_rows):
            # 单变体商品（Option1 = Title / Default Title，或者只有一行变体）
            option_names = []

        Template = self.env['product.template']
        tmpl = Template.search([('x_shopify_handle', '=', handle)], limit=1)
        is_new = not tmpl

        vals = self._build_template_vals(main_row, variant_rows, has_variants=bool(option_names))

        if is_new:
            tmpl = Template.create(vals)
        else:
            tmpl.write(vals)

        warnings = self._apply_variants(tmpl, variant_rows, option_names)
        self._apply_tags(tmpl, main_row)
        n_images = self._queue_images(tmpl, image_rows, variant_rows, option_names)

        return tmpl, is_new, n_images, warnings

    @staticmethod
    def _option_names(rows):
        """返回 [(列序号, 属性名), ...]，属性名取该列在整组行里第一个非空值。"""
        names = []
        for idx, (name_key, _value_key) in enumerate(OPTION_PAIRS):
            name = next((
                (r.get(name_key) or '').strip() for r in rows if (r.get(name_key) or '').strip()
            ), '')
            if name:
                names.append((idx, name))
        return names

    @staticmethod
    def _is_default_variant_only(option_names, variant_rows):
        if len(variant_rows) <= 1:
            return True
        if len(option_names) == 1 and option_names[0][1] == 'Title':
            values = {(r.get(OPTION_PAIRS[0][1]) or '').strip() for r in variant_rows}
            return values <= {'Default Title', ''}
        return False

    @staticmethod
    def _row_combo(row, option_names):
        """这一行对应的属性组合：{属性名: 属性值}（空值的属性不计入）。"""
        combo = {}
        for idx, attr_name in option_names:
            value = (row.get(OPTION_PAIRS[idx][1]) or '').strip()
            if value:
                combo[attr_name] = value
        return combo

    def _build_template_vals(self, main_row, variant_rows, has_variants=False):
        tmpl_fields = self.env['product.template']._fields
        vals = {
            'x_shopify_handle': main_row.get('Handle', '').strip(),
            'name': main_row.get('Title', '').strip() or main_row.get('Handle', '').strip(),
            'sale_ok': True,
        }

        status = (main_row.get('Status') or '').strip()
        if status:
            vals['x_shopify_status'] = status

        published = (main_row.get('Published') or '').strip().lower()
        if 'website_published' in tmpl_fields:
            vals['website_published'] = (published == 'true')

        body_html = (main_row.get('Body (HTML)') or '').strip()
        if body_html:
            if 'description_ecommerce' in tmpl_fields:
                vals['description_ecommerce'] = body_html
            elif 'description_sale' in tmpl_fields:
                vals['description_sale'] = body_html

        seo_title = (main_row.get('SEO Title') or '').strip()
        if seo_title and 'website_meta_title' in tmpl_fields:
            vals['website_meta_title'] = seo_title
        seo_desc = (main_row.get('SEO Description') or '').strip()
        if seo_desc and 'website_meta_description' in tmpl_fields:
            vals['website_meta_description'] = seo_desc

        category_path = (main_row.get('Product Category') or '').strip()
        if 'public_categ_ids' in tmpl_fields:
            public_categ = self._get_or_create_public_category(category_path)
            if public_categ:
                vals['public_categ_ids'] = [(6, 0, [public_categ.id])]

        internal_categ = self._get_or_create_internal_category(category_path)
        if internal_categ:
            vals['categ_id'] = internal_categ.id

        # 单变体商品（占绝大多数）：价格/成本/SKU/条码/重量直接写模板；
        # 多变体商品的价格在 _apply_variants 里按"基础价 + 属性加价"写入。
        if not has_variants:
            row = variant_rows[0] if variant_rows else main_row
            self._apply_price_fields(vals, row, tmpl_fields)

        return vals

    @staticmethod
    def _apply_price_fields(vals, row, avail_fields):
        price = ShopifyImportWizard._to_float(row.get('Variant Price'))
        if price is not None:
            vals['list_price'] = price
        cost = ShopifyImportWizard._to_float(row.get('Cost per item'))
        if cost is not None and 'standard_price' in avail_fields:
            vals['standard_price'] = cost
        sku = (row.get('Variant SKU') or '').strip()
        if sku:
            vals['default_code'] = sku
        barcode = (row.get('Variant Barcode') or '').strip()
        if barcode:
            vals['barcode'] = barcode
        grams = ShopifyImportWizard._to_float(row.get('Variant Grams'))
        if grams and 'weight' in avail_fields:
            vals['weight'] = grams / 1000.0

    # =================================================================
    # 分类
    # =================================================================
    def _get_or_create_public_category(self, path):
        parts = self._split_category_path(path)
        Categ = self.env['product.public.category']
        parent_id = False
        categ = Categ
        for part in parts:
            domain = [('name', '=', part), ('parent_id', '=', parent_id)]
            categ = Categ.search(domain, limit=1)
            if not categ:
                categ = Categ.create({'name': part, 'parent_id': parent_id})
            parent_id = categ.id
        return categ

    def _get_or_create_internal_category(self, path):
        parts = self._split_category_path(path)
        Categ = self.env['product.category']
        parent_id = False
        categ = Categ
        for part in parts:
            domain = [('name', '=', part), ('parent_id', '=', parent_id)]
            categ = Categ.search(domain, limit=1)
            if not categ:
                categ = Categ.create({'name': part, 'parent_id': parent_id})
            parent_id = categ.id
        return categ

    @staticmethod
    def _split_category_path(path):
        path = (path or '').strip()
        if not path or path.lower() == 'uncategorized':
            path = '未分类（Shopify导入）'
        return [p.strip() for p in path.split('>') if p.strip()]

    # =================================================================
    # 标签（供应商品牌 + Shopify Tags）
    # =================================================================
    def _apply_tags(self, tmpl, main_row):
        tmpl_fields = self.env['product.template']._fields
        if 'product_tag_ids' not in tmpl_fields:
            return  # 当前 Odoo 版本没有这个字段就跳过，不影响其它导入

        tag_names = []
        vendor = (main_row.get('Vendor') or '').strip()
        if vendor and not VENDOR_URL_RE.search(vendor):
            tag_names.append(vendor)

        tags_field = (main_row.get('Tags') or '').strip()
        if tags_field:
            tag_names += [t.strip() for t in tags_field.split(',') if t.strip()]

        if not tag_names:
            return

        Tag = self.env['product.tag']
        tag_ids = []
        for name in tag_names:
            tag = Tag.search([('name', '=', name)], limit=1)
            if not tag:
                tag = Tag.create({'name': name})
            tag_ids.append(tag.id)

        existing = tmpl.product_tag_ids.ids
        merged = list(set(existing) | set(tag_ids))
        tmpl.write({'product_tag_ids': [(6, 0, merged)]})

    # =================================================================
    # 变体 / 属性
    # =================================================================
    def _apply_variants(self, tmpl, variant_rows, option_names):
        """建属性/属性值/属性行，生成变体，并把每行的价格等写到对应变体上。

        返回需要写进导入日志的警告列表。
        """
        warnings = []
        if not option_names:
            # 单变体：价格等已经在 _build_template_vals 里写过模板了，
            # 但 SKU/条码在多公司/多变体场景下 Odoo 是写在 product.product 上的，
            # 这里再兜底写一次到隐式变体，保证生效。
            row = variant_rows[0] if variant_rows else {}
            variant = tmpl.product_variant_ids[:1]
            if variant and row:
                v_vals = {}
                sku = (row.get('Variant SKU') or '').strip()
                if sku:
                    v_vals['default_code'] = sku
                barcode = (row.get('Variant Barcode') or '').strip()
                if barcode:
                    v_vals['barcode'] = barcode
                if v_vals:
                    variant.write(v_vals)
            return warnings

        Attribute = self.env['product.attribute']
        AttrValue = self.env['product.attribute.value']
        AttrLine = self.env['product.template.attribute.line']

        # 收集这个商品所有出现过的属性值（按 CSV 里出现的顺序）
        attr_values_map = OrderedDict((name, OrderedDict()) for _, name in option_names)
        for row in variant_rows:
            for attr_name, value in self._row_combo(row, option_names).items():
                attr_values_map[attr_name][value] = True

        for attr_name, values in attr_values_map.items():
            if not values:
                continue
            # 只复用"会生成变体"的同名属性；同名但 create_variant 不同的属性
            # （比如后台手工建的"仅显示"属性）不能拿来生成变体
            attribute = Attribute.search(
                [('name', '=', attr_name), ('create_variant', '=', 'always')], limit=1)
            if not attribute:
                attribute = Attribute.create({
                    'name': attr_name,
                    'create_variant': 'always',
                })
            value_ids = []
            for v in values.keys():
                val = AttrValue.search(
                    [('name', '=', v), ('attribute_id', '=', attribute.id)], limit=1)
                if not val:
                    val = AttrValue.create({'name': v, 'attribute_id': attribute.id})
                value_ids.append(val.id)

            line = AttrLine.search([
                ('product_tmpl_id', '=', tmpl.id),
                ('attribute_id', '=', attribute.id),
            ], limit=1)
            if line:
                missing = [vid for vid in value_ids if vid not in line.value_ids.ids]
                if missing:
                    line.write({'value_ids': [(4, vid) for vid in missing]})
            else:
                AttrLine.create({
                    'product_tmpl_id': tmpl.id,
                    'attribute_id': attribute.id,
                    'value_ids': [(6, 0, value_ids)],
                })

        # 属性行写完后 Odoo 会自动生成 product.product 变体组合；
        # 部分版本在批量写入时需要显式触发一下，做个防御性调用。
        create_variants = getattr(tmpl, '_create_variant_ids', None)
        if callable(create_variants):
            create_variants()
        tmpl.invalidate_recordset(['product_variant_ids'])

        warnings += self._apply_variant_prices(tmpl, variant_rows, option_names)

        # 把每一行的成本/SKU/条码/重量按属性值组合写到对应变体
        for row in variant_rows:
            combo = self._row_combo(row, option_names)
            if not combo:
                continue
            variant = self._find_variant(tmpl, combo)
            if not variant:
                _logger.warning(
                    '找不到匹配的变体，跳过该行: handle=%s combo=%s',
                    tmpl.x_shopify_handle, combo)
                warnings.append(f'找不到属性组合 {combo} 对应的变体，该行已跳过')
                continue

            v_vals = {}
            cost = self._to_float(row.get('Cost per item'))
            if cost is not None:
                v_vals['standard_price'] = cost
            sku = (row.get('Variant SKU') or '').strip()
            if sku:
                v_vals['default_code'] = sku
            barcode = (row.get('Variant Barcode') or '').strip()
            if barcode:
                v_vals['barcode'] = barcode
            grams = self._to_float(row.get('Variant Grams'))
            if grams:
                v_vals['weight'] = grams / 1000.0

            if v_vals:
                variant.write(v_vals)
        return warnings

    def _apply_variant_prices(self, tmpl, variant_rows, option_names):
        """把 Shopify 每个变体的售价换算成 Odoo 的"模板基础价 + 属性值加价"。

        Odoo 标准版没有"每个变体独立售价"：变体价 = 模板 list_price +
        所选属性值 price_extra 之和（直接写 product.product.lst_price 其实会
        改掉模板价格，所有变体一起变）。所以这里：
          - 模板 list_price = 所有变体里的最低价
          - 按属性顺序逐个求出每个属性值的加价（取该值出现的各行里"剩余差价"的最小值）
          - 最后逐行校验；Shopify 价格表无法拆成"加法"形式（比如只有某一个组合
            单独加价）时，写入最接近的结果并在导入日志里给出警告
        """
        precision = self.env['decimal.precision'].precision_get('Product Price')
        priced = []
        for row in variant_rows:
            price = self._to_float(row.get('Variant Price'))
            combo = self._row_combo(row, option_names)
            if price is not None and combo:
                priced.append((combo, price))
        if not priced:
            return []

        base = min(p for _, p in priced)
        tmpl.write({'list_price': base})

        extras = {}  # (属性名, 属性值) -> 加价
        done_attrs = set()
        for _idx, attr_name in option_names:
            for combo, price in priced:
                value = combo.get(attr_name)
                if value is None:
                    continue
                # 只扣掉"前面已经算完的属性"的加价
                assigned = sum(
                    extras.get((a, v), 0.0) for a, v in combo.items() if a in done_attrs)
                remaining = float_round(price - base - assigned, precision_digits=precision)
                key = (attr_name, value)
                extras[key] = min(extras.get(key, remaining), remaining)
            done_attrs.add(attr_name)

        ptavs = tmpl.valid_product_template_attribute_line_ids.product_template_value_ids
        for ptav in ptavs:
            key = (ptav.attribute_id.name, ptav.product_attribute_value_id.name)
            if key in extras and float_compare(
                    ptav.price_extra, extras[key], precision_digits=precision):
                ptav.price_extra = extras[key]

        warnings = []
        for combo, price in priced:
            computed = base + sum(extras.get((a, v), 0.0) for a, v in combo.items())
            if float_compare(computed, price, precision_digits=precision):
                warnings.append(
                    f'变体 {combo} 在 Shopify 的价格是 {price}，但 Odoo 只能按'
                    f'"基础价 + 属性加价"计算，实际为 {computed}，请在后台手动调整')
        return warnings

    @staticmethod
    def _find_variant(tmpl, combo):
        """按 {属性名: 属性值} 精确匹配变体（属性名 + 值一起比较，避免不同属性同名值串号）。"""
        wanted = set(combo.items())
        for variant in tmpl.product_variant_ids:
            have = {
                (ptav.attribute_id.name, ptav.product_attribute_value_id.name)
                for ptav in variant.product_template_attribute_value_ids
            }
            if wanted <= have:
                return variant
        return tmpl.env['product.product']

    # =================================================================
    # 图片排队
    # =================================================================
    def _queue_images(self, tmpl, image_rows, variant_rows, option_names):
        Queue = self.env['shopify.image.queue']
        # 去重键是 (图片URL, 变体)：Shopify 的 Variant Image 基本都同时是该商品
        # 画廊里的某张 Image Src，只按 URL 去重会把变体图片的关联整个丢掉
        existing = {
            (q.source_url, q.product_variant_id.id or False)
            for q in Queue.search([('product_tmpl_id', '=', tmpl.id)])
        }
        count = 0
        base_vals = self._queue_base_vals(tmpl)

        for idx, row in enumerate(image_rows):
            url = (row.get('Image Src') or '').strip()
            if not url or (url, False) in existing:
                continue
            position_raw = (row.get('Image Position') or '').strip()
            try:
                position = int(position_raw)
            except ValueError:
                position = idx + 1
            role = 'main' if position == 1 else 'extra'
            Queue.create({
                **base_vals,
                'product_tmpl_id': tmpl.id,
                'sequence': position,
                'role': role,
                'source_url': url,
                'alist_target_path': self._alist_target_path(tmpl, url),
            })
            existing.add((url, False))
            count += 1

        # 变体专属图片（Variant Image 列）。单变体商品不用处理：唯一的变体
        # 直接显示商品图片，Variant Image 只是重复指向其中一张。
        if not option_names:
            return count
        for row in variant_rows:
            v_url = (row.get('Variant Image') or '').strip()
            if not v_url:
                continue
            variant = self._find_variant(tmpl, self._row_combo(row, option_names))
            if not variant or (v_url, variant.id) in existing:
                continue
            Queue.create({
                **base_vals,
                'product_tmpl_id': tmpl.id,
                'product_variant_id': variant.id,
                'sequence': 1,
                'role': 'extra',
                'source_url': v_url,
                'alist_target_path': self._alist_target_path(tmpl, v_url),
            })
            existing.add((v_url, variant.id))
            count += 1

        return count

    def _queue_base_vals(self, tmpl):
        vals = {}
        if self.media_source_id:
            vals['media_source_id'] = self.media_source_id.id
        return vals

    def _alist_target_path(self, tmpl, url):
        if not self.media_source_id:
            return False
        prefix = (self.alist_upload_path_prefix or '/b2/shopify-products').rstrip('/')
        filename = url.split('/')[-1].split('?')[0] or 'image.jpg'
        return f'{prefix}/{tmpl.id}_{filename}'

    # =================================================================
    # 工具方法
    # =================================================================
    @staticmethod
    def _to_float(value):
        value = (value or '').strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None
