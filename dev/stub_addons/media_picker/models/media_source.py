# -*- coding: utf-8 -*-
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ValidationFailure(Exception):
    """Same role as media_picker.models.media_utils.ValidationFailure."""


def _host_matches(host, domain):
    host, domain = (host or '').lower(), (domain or '').lower()
    return bool(host and domain) and (host == domain or host.endswith('.' + domain))


class MediaSource(models.Model):
    _name = 'media.source'
    _description = 'Media source (stub)'

    name = fields.Char(required=True)
    source_type = fields.Selection(
        [('alist', 'Alist'), ('s3', 'S3 Compatible'), ('mock', 'Mock')], default='alist', required=True)
    active = fields.Boolean(default=True)
    root_path = fields.Char(default='/')
    cdn_base_url = fields.Char()
    trusted_domains = fields.Text()
    upload_enabled = fields.Boolean(default=False)

    def _get_trusted_domain_list(self):
        self.ensure_one()
        domains = [l.strip().lower() for l in (self.trusted_domains or '').splitlines() if l.strip()]
        host = (urlparse(self.cdn_base_url or '').hostname or '').lower()
        if host and host not in domains:
            domains.append(host)
        return domains

    def _check_domain_trusted(self, hostname):
        self.ensure_one()
        return any(_host_matches(hostname, d) for d in self._get_trusted_domain_list())

    def upload_media(self, folder, filename, stream, size=None, content_type=None, timeout=None):
        """Stub: pretend the bytes were stored and return the resolved CDN URL."""
        self.ensure_one()
        if not self.upload_enabled:
            raise ValidationFailure('Uploads are disabled for this source.')
        stream.read()
        path = '/'.join(p.strip('/') for p in (self.root_path or '', folder or '', filename) if p.strip('/'))
        base = (self.cdn_base_url or 'https://media.example.com').rstrip('/')
        return {'url': f'{base}/{path}', 'source_ref': '/' + path, 'name': filename, 'media_type': 'image'}


class MediaBind(models.Model):
    _name = 'media.bind'
    _description = 'External media binding (stub)'
    _order = 'sequence, id'

    res_model = fields.Char()
    res_id = fields.Integer()
    res_field = fields.Char()
    usage = fields.Selection([('gallery', 'Gallery'), ('other', 'Other')], default='gallery')
    name = fields.Char()
    url = fields.Char(required=True)
    media_type = fields.Selection([('image', 'Image'), ('video', 'Video')], default='image')
    alt_text = fields.Char()
    sequence = fields.Integer(default=10)
    is_main = fields.Boolean()
    source_id = fields.Many2one('media.source', ondelete='set null')
    product_tmpl_id = fields.Many2one('product.template', ondelete='cascade', index=True)
    product_variant_id = fields.Many2one('product.product', ondelete='cascade')
    source_ref = fields.Char(index=True)
    shopify_media_id = fields.Char(index=True)
    content_length = fields.Integer()
    health_status = fields.Selection(
        [('unchecked', 'Unchecked'), ('ok', 'OK'), ('degraded', 'Degraded'), ('broken', 'Broken')],
        default='unchecked')
    http_code = fields.Integer()
    fail_count = fields.Integer(default=0)
    last_checked = fields.Datetime()

    @api.constrains('url', 'source_id')
    def _check_url_trusted_domain(self):
        raw = self.env['ir.config_parameter'].sudo().get_param('media_picker.trusted_domains', '') or ''
        global_domains = [l.strip().lower() for l in raw.replace(',', '\n').splitlines() if l.strip()]
        for rec in self:
            host = (urlparse(rec.url or '').hostname or '').lower()
            if not host:
                raise ValidationError('Media URL has no hostname.')
            trusted = bool(rec.source_id and rec.source_id._check_domain_trusted(host))
            trusted = trusted or any(_host_matches(host, d) for d in global_domains)
            if not trusted and not global_domains and not rec.source_id:
                trusted = True
            if not trusted:
                raise ValidationError(
                    'URL host "%s" is not in the trusted domain list for this source.' % host)

    @api.model
    def upsert_external_media_from_shopify(self, product_tmpl_id, media_list, variant_map=None):
        created, updated = self.browse(), self.browse()
        for media in media_list or []:
            key = media.get('shopify_media_id') or media.get('external_reference') or media.get('source_ref')
            if not key or not media.get('url'):
                continue
            existing = self.search([
                ('product_tmpl_id', '=', product_tmpl_id), ('usage', '=', 'gallery'),
                '|', ('shopify_media_id', '=', key), ('source_ref', '=', key),
            ], limit=1)
            vals = {
                'res_model': 'product.template', 'res_id': product_tmpl_id,
                'product_tmpl_id': product_tmpl_id,
                'product_variant_id': media.get('product_variant_id') or False,
                'usage': 'gallery', 'res_field': 'gallery', 'url': media['url'],
                'alt_text': media.get('alt_text') or False,
                'media_type': media.get('media_type') or 'image',
                'sequence': media.get('sequence', 10), 'is_main': bool(media.get('is_main')),
                'shopify_media_id': media.get('shopify_media_id') or False,
                'source_ref': media.get('source_ref') or media.get('external_reference') or key,
                'name': media.get('name') or False,
            }
            if existing:
                existing.write(vals)
                updated |= existing
            else:
                created |= self.create(vals)
        return {'created': created.ids, 'updated': updated.ids}


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    use_external_media = fields.Boolean()
    external_media_mode = fields.Selection([
        ('external_first', 'External first'), ('odoo_first', 'Odoo first'),
        ('external_only', 'External only'),
    ], default='external_first', required=True)
    media_bind_ids = fields.One2many('media.bind', 'product_tmpl_id')
    # 真实模块会把 is_main 的外链图自动同步到 image_1920；替身只保留状态字段
    mp_main_sync_status = fields.Selection(
        [('none', 'Not synced'), ('pending', 'Pending'), ('ok', 'Synced'),
         ('skipped', 'Skipped'), ('error', 'Error')], default='none')

    def upsert_external_media_from_shopify(self, media_list, variant_map=None):
        self.ensure_one()
        return self.env['media.bind'].upsert_external_media_from_shopify(
            self.id, media_list, variant_map=variant_map)
