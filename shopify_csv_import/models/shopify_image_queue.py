# -*- coding: utf-8 -*-
import base64
import logging
from urllib.parse import urlparse

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:  # pragma: no cover - requests 是 Odoo 标准依赖，正常都有
    requests = None


class ShopifyImageQueue(models.Model):
    _name = 'shopify.image.queue'
    _description = 'Shopify 商品图片同步队列'
    _order = 'product_tmpl_id, sequence, id'

    product_tmpl_id = fields.Many2one(
        'product.template', string='商品', required=True, ondelete='cascade', index=True)
    product_variant_id = fields.Many2one(
        'product.product', string='对应变体', ondelete='cascade',
        help='仅当 Shopify 该行有 Variant Image 时才会关联到具体变体，否则为空表示商品公共图片。')
    sequence = fields.Integer(string='顺序', default=10)
    role = fields.Selection([
        ('main', '主图'),
        ('extra', '附加图片'),
    ], string='类型', default='extra', required=True)
    source_url = fields.Char(string='Shopify 原始图片URL', required=True)

    # 走 CDN 模式时使用：选了 media_source_id 就会尝试上传到 Alist，
    # 失败或没选就直接回退成 Odoo 本地二进制图片。
    media_source_id = fields.Many2one(
        'product.media.source', string='Alist 图片源',
        help='对应 media_picker 模块里已经配置好的 Alist 连接（product.media.source）。'
             '留空则该图片直接存成 Odoo 本地二进制图片。')
    alist_target_path = fields.Char(
        string='Alist 目标路径',
        help='上传到 Alist 后的目标路径，例如 /b2/shopify-products/123_foo.jpg。')

    state = fields.Selection([
        ('pending', '待处理'),
        ('done', '已完成'),
        ('error', '失败'),
    ], string='状态', default='pending', index=True)
    error_message = fields.Char(string='错误信息')

    # ---------------------------------------------------------------
    # cron 入口
    # ---------------------------------------------------------------
    @api.model
    def _cron_process_pending(self, limit=30):
        records = self.search([('state', '=', 'pending')], limit=limit, order='id')
        for rec in records:
            rec._process()
        return len(records)

    def action_retry(self):
        for rec in self:
            rec.state = 'pending'
            rec.error_message = False
        self._cron_process_pending(limit=len(self))

    # ---------------------------------------------------------------
    # 核心处理逻辑
    # ---------------------------------------------------------------
    def _process(self):
        self.ensure_one()
        try:
            content = self._download(self.source_url)
            if not content:
                raise ValueError('下载失败或返回空内容')

            cdn_url = False
            if self.media_source_id:
                try:
                    cdn_url = self._upload_and_resolve(content)
                except Exception:
                    # Alist 上传/解析失败：记录日志，直接走本地二进制兜底，
                    # 不让图片同步整体失败。
                    _logger.info(
                        'Alist 上传/解析失败，改用本地二进制存储: %s',
                        self.source_url, exc_info=True,
                    )
                    cdn_url = False

            if cdn_url:
                self._save_via_media_bind(cdn_url)
            else:
                self._save_binary(content)

            self.write({'state': 'done', 'error_message': False})
        except Exception as e:
            _logger.exception('图片同步失败: %s', self.source_url)
            self.write({'state': 'error', 'error_message': str(e)[:250]})

    # ---------------------------------------------------------------
    # 方案 A：本地二进制兜底（不依赖 media_picker）
    # ---------------------------------------------------------------
    def _save_binary(self, content):
        b64 = base64.b64encode(content)
        if self.role == 'main':
            self.product_tmpl_id.image_1920 = b64
        else:
            self.env['product.image'].create({
                'product_tmpl_id': self.product_tmpl_id.id,
                'name': self.product_tmpl_id.name,
                'image_1920': b64,
            })
        if self.product_variant_id:
            self.product_variant_id.image_1920 = b64

    # ---------------------------------------------------------------
    # 方案 B：转存 Alist，走 media_picker 的 media.bind 外链画廊
    # （即 product.template.upsert_external_media_from_shopify，
    # 是 media_picker 自己文档里指定的 Shopify 导入接入点）
    # ---------------------------------------------------------------
    def _save_via_media_bind(self, cdn_url):
        media_item = {
            # 用原始 Shopify 图片 URL 当去重键：同一张图重复导入不会建重复记录，
            # 只会更新已有的 media.bind 行。
            'shopify_media_id': self.source_url,
            'url': cdn_url,
            'media_type': 'image',
            'sequence': self.sequence,
            'is_main': self.role == 'main',
        }
        if self.product_variant_id:
            media_item['product_variant_id'] = self.product_variant_id.id

        self.product_tmpl_id.upsert_external_media_from_shopify([media_item])

        if not self.product_tmpl_id.use_external_media:
            self.product_tmpl_id.write({'use_external_media': True})

    def _upload_and_resolve(self, content):
        """上传到 Alist，再用 media_picker 自带的 get_file 解析出最终可信直链。

        没有复用 media_picker 的域名改写逻辑（那是 media.source v2 的功能），
        而是走跟 media_picker 的 product.media.source（v1，产品图片专用连接）
        完全一样的路径：调用 Alist 的 /api/fs/get 拿 raw_url，再用
        source._check_domain_trusted() 校验，这跟你后台"选文件挂到商品"那条
        路径用的是同一套代码、同一份信任逻辑。
        """
        source = self.media_source_id
        path = self.alist_target_path or self._default_alist_path()
        self._alist_put(source, path, content)

        # 复用 media_picker 自己的、已经在生产验证过的解析逻辑
        from odoo.addons.media_picker.models import pem_alist_client as alist_client
        data = alist_client.get_file(source, path)
        raw_url = data.get('raw_url')
        if not raw_url:
            raise ValueError('Alist 未返回 raw_url')

        hostname = urlparse(raw_url).hostname
        if not source._check_domain_trusted(hostname):
            raise ValueError(
                f'解析出的域名 {hostname} 不在该 Alist 图片源的可信域名列表里，'
                f'请检查 product.media.source 的 trusted_domains 配置')
        return raw_url

    def _default_alist_path(self):
        filename = self._guess_filename(self.source_url)
        return f'/b2/shopify-products/{self.product_tmpl_id.id}_{filename}'

    # ---------------------------------------------------------------
    # 下载 / 上传工具方法
    # ---------------------------------------------------------------
    @staticmethod
    def _download(url):
        if requests is None:
            raise RuntimeError('服务器缺少 requests 库，无法下载图片')
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def _guess_filename(url):
        name = url.split('/')[-1].split('?')[0]
        return name or 'image.jpg'

    @staticmethod
    def _alist_put(source, path, content):
        """把字节流 PUT 到 Alist（v3 的 /api/fs/put）。

        media_picker 自带的 pem_alist_client.py 只有 list_dir/get_file
        （只读，供"挑选已存在文件"用），没有上传，这是本模块唯一需要
        自己实现网络调用的地方。如果你实际部署的 Alist 版本上传接口不一样，
        改这一个方法就行，其它代码不用动。
        """
        if requests is None:
            raise RuntimeError('服务器缺少 requests 库，无法上传图片')
        if not source.alist_url:
            raise ValueError('这个 Alist 图片源没有配置 alist_url')

        resp = requests.put(
            f"{source.alist_url.rstrip('/')}/api/fs/put",
            headers={
                'Authorization': source.alist_token or '',
                'File-Path': requests.utils.quote(path, safe=''),
                'Content-Type': 'application/octet-stream',
                'As-Task': 'false',
            },
            data=content,
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 200:
            raise ValueError(f'Alist 上传失败: {data}')
