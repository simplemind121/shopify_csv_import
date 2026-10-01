# Security Policy

## Supported versions

| Version      | Supported                  |
| ------------ | --------------------------- |
| 19.0.2.x     | Yes (current, media_picker ≥ 3.8) |
| 19.0.1.x     | Fixes only (media_picker 3.4.x) |
| &lt; 19.0.1  | No                            |

## Reporting a vulnerability

Do **not** open a public issue with secrets, credentials, or exploit details.

1. Contact the repository owner via a private channel (GitHub private
   security advisory if enabled, or a direct message).
2. Include: affected version (`VERSION` / release tag), environment
   (Odoo 19 / Community), reproduction steps, impact.
3. Allow reasonable time for a patch release before public disclosure.

## Operational guidance

- This module never stores or reads object-storage credentials. Uploads go
  through `media_picker`'s `media.source.upload_media()`; keep credentials
  configured in media_picker.
- With an object-storage source selected, storefront images are first served
  from Shopify's CDN (`cdn.shopify.com`). If you restrict media_picker's
  trusted domains, that host has to be on the list.
- The CSV import wizard is restricted to `base.group_system` (Settings /
  Technical), same as `media_picker`'s own source configuration —
  don't widen that without a reason.
- Prefer the official `deploy_shopify_csv_import.sh` so the addons path,
  `-i`/`-u` choice, and backups stay consistent. The script never writes a
  database password to disk; it's read interactively and kept only in the
  script's environment for the duration of the run.
- After deploying, confirm the "Shopify 导入" menu and the image sync queue
  are visible only to the intended admin group.
