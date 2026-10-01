from . import models
from . import wizard


def pre_init_check_media_picker(env):
    """安装前检查 media_picker 的版本：19.0.2.0.0 起对接的是 media_picker 3.8 的
    media.source（上传接口 upload_media、链接健康检查、主图同步）。"""
    if 'media.source' not in env or not hasattr(env['media.source'], 'upload_media'):
        from odoo.exceptions import UserError
        raise UserError(
            'shopify_csv_import 19.0.2.x 需要 media_picker 19.0.3.8 或更高版本'
            '（要用到 media.source.upload_media）。请先升级 media_picker；'
            '如果只能用旧版 media_picker，请安装 shopify_csv_import 19.0.1.1.1。')
