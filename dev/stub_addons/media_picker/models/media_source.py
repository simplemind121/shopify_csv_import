# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ProductMediaSource(models.Model):
    _name = 'product.media.source'
    _description = 'Alist media source (stub)'

    name = fields.Char(required=True)
    alist_url = fields.Char()
    alist_token = fields.Char()
    trusted_domains = fields.Char(help='Comma separated host names')

    def _check_domain_trusted(self, hostname):
        self.ensure_one()
        if not hostname:
            return False
        raw = self.trusted_domains or self.env['ir.config_parameter'].sudo().get_param(
            'media_picker.trusted_domains', '')
        domains = [d.strip().lower() for d in raw.split(',') if d.strip()]
        hostname = hostname.lower()
        return any(hostname == d or hostname.endswith('.' + d) for d in domains)


class MediaBind(models.Model):
    _name = 'media.bind'
    _description = 'External media binding (stub)'
    _order = 'sequence, id'

    product_tmpl_id = fields.Many2one('product.template', required=True, ondelete='cascade')
    product_variant_id = fields.Many2one('product.product', ondelete='cascade')
    url = fields.Char(required=True)
    media_type = fields.Char(default='image')
    sequence = fields.Integer(default=10)
    is_main = fields.Boolean()
    shopify_media_id = fields.Char(index=True)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    use_external_media = fields.Boolean()
    media_bind_ids = fields.One2many('media.bind', 'product_tmpl_id')

    def upsert_external_media_from_shopify(self, media_items):
        self.ensure_one()
        Bind = self.env['media.bind']
        for item in media_items:
            vals = dict(item, product_tmpl_id=self.id)
            existing = Bind.search([
                ('product_tmpl_id', '=', self.id),
                ('shopify_media_id', '=', item.get('shopify_media_id')),
            ], limit=1)
            if existing:
                existing.write(vals)
            else:
                Bind.create(vals)
        return True
