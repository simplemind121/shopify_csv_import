# -*- coding: utf-8 -*-
"""升级到 19.0.2.0.0：给旧版本留下的图片记录补上新字段的初始值。"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Queue = env['shopify.image.queue']
    # 旧版本里已经存成本地图片的：前台显示来源是本地图片
    Queue.search([('storage', '=', 'binary'), ('display_source', '=', False)]).write(
        {'display_source': 'local'})
    # 重新计算对账结论（新加的存储字段）
    rows = Queue.search([])
    env.add_to_compute(Queue._fields['verdict'], rows)
    rows.flush_recordset(['verdict'])
