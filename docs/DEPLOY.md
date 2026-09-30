# Deploy runbook — shopify_csv_import

## Prerequisites

- Docker-based Odoo 19 (`prod-odoo` or a staging equivalent)
- `media_picker` already installed in the target database (this module
  depends on it for the optional Alist/B2 CDN image path)
- Addons directory mounted and writable by the deploy user (script uses `sudo`)
- Package + script in the **same directory**
- If you want CDN image sync: network access from Odoo to your Alist instance,
  and an existing, working `product.media.source` record (created through
  `media_picker`'s own admin UI)

## Install or upgrade

```bash
chmod +x deploy_shopify_csv_import.sh
sudo ./deploy_shopify_csv_import.sh
```

Recommended for a first run — deploy to staging before touching production:

```bash
sudo ./deploy_shopify_csv_import.sh --target staging
```

Other options:

```bash
sudo ./deploy_shopify_csv_import.sh --container my-odoo   # explicit container name
sudo ./deploy_shopify_csv_import.sh --db mydb              # explicit database
sudo ./deploy_shopify_csv_import.sh --package /path/to/shopify_csv_import-19.0.1.0.0.zip
sudo ./deploy_shopify_csv_import.sh --force                # allow same/older-version reinstall
sudo ./deploy_shopify_csv_import.sh --no-backup            # skip pre-deploy backup (not recommended)
sudo ./deploy_shopify_csv_import.sh --rollback              # restore the most recent backup
```

The script:

1. Locates `shopify_csv_import*.zip` / `*.tar.gz` next to itself (or wherever
   `--package` points), verifies it contains `__manifest__.py` and the
   expected module files, and reads the package's own version
2. Checks that `media_picker` is present in the target addons directory
   (this module won't install without it)
3. Prompts once for the Odoo database password (never written to disk)
4. Backs up the database (`pg_dump -Fc`) and the previous module directory
5. Installs or upgrades the module (`-i` / `-u`, auto-detected from the
   module's current state in `ir_module_module`)
6. Verifies the **real** post-install state in the database — not just the
   command's exit code
7. Restarts the container and polls `/web/health` until it responds
8. Scans the post-restart logs for `ERROR` / `CRITICAL` / tracebacks
9. On any install/upgrade failure, restores the previous module files
   automatically (the database side is protected by Odoo's own transaction)

## Post-deploy checklist

1. Apps → `shopify_csv_import` version = **19.0.1.0.0**
2. Top menu → "Shopify 导入" → "导入商品 CSV" is visible (admin only)
3. Import a small test CSV (a handful of products) without selecting an Alist
   image source first — confirm products/variants/categories/tags appear
   correctly and images land as standard Odoo binary images
4. If you use the CDN path: re-run the import (or a subset) with a
   `product.media.source` selected, confirm entries show up in "图片同步队列"
   as `done`, and that the product's storefront gallery shows the CDN image
5. Re-import the same CSV once more — confirm it **updates** existing
   products (same count of products, no duplicates)

## Rollback

```bash
sudo ./deploy_shopify_csv_import.sh --rollback
```

Restores the most recent pre-deploy backup (module files + full database
dump) taken by the script. You'll be asked to type the database name to
confirm, since this overwrites everything written since that backup.

## Configuration notes

- This module stores no credentials of its own. The Alist connection used
  for CDN image sync is whichever `product.media.source` record you pick in
  the import wizard — manage its URL/token through `media_picker`'s own UI.
- `ir.cron` "Shopify 图片同步" runs every 2 minutes, 30 images per batch, by
  default. Adjust the interval/batch size from Settings → Technical →
  Scheduled Actions if you're importing a very large catalog and want it to
  move faster (or slower, to reduce load).
