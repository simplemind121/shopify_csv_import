# Security Policy

## Supported versions

| Version      | Supported                  |
| ------------ | --------------------------- |
| 19.0.1.x     | Yes (current)                |
| &lt; 19.0.1  | No                            |

## Reporting a vulnerability

Do **not** open a public issue with secrets, credentials, or exploit details.

1. Contact the repository owner via a private channel (GitHub private
   security advisory if enabled, or a direct message).
2. Include: affected version (`VERSION` / release tag), environment
   (Odoo 19 / Community), reproduction steps, impact.
3. Allow reasonable time for a patch release before public disclosure.

## Operational guidance

- This module never stores Alist/S3/B2 credentials itself — it reads an
  existing `product.media.source` record that belongs to the `media_picker`
  module, and only for the duration of an upload/resolve call. Keep those
  credentials configured through `media_picker`'s own admin UI, not in this
  module's code or config.
- The CSV import wizard is restricted to `base.group_system` (Settings /
  Technical), same as `media_picker`'s own `product.media.source` access —
  don't widen that without a reason.
- Prefer the official `deploy_shopify_csv_import.sh` so the addons path,
  `-i`/`-u` choice, and backups stay consistent. The script never writes a
  database password to disk; it's read interactively and kept only in the
  script's environment for the duration of the run.
- After deploying, confirm the "Shopify 导入" menu and the image sync queue
  are visible only to the intended admin group.
