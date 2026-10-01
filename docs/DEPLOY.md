# Deploy runbook — shopify_csv_import

## Prerequisites

- Docker-based Odoo 19 (`prod-odoo` or a staging equivalent)
- `media_picker` **19.0.3.8 or newer** already installed in the target database
  (this module uses its `media.source` upload API, link health checks and
  main-image sync; with media_picker 3.4.x use shopify_csv_import 19.0.1.1.1)
- Addons directory mounted and writable by the deploy user (script uses `sudo`)
- Package + script in the **same directory**
- For object-storage backup: a `media.source` (Alist / S3) configured in
  media_picker with **uploads enabled** and credentials that can write. If
  media_picker's global trusted-domain list (`media_picker.trusted_domains`) is
  set, it must include `cdn.shopify.com`.

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
sudo ./deploy_shopify_csv_import.sh --package /path/to/shopify_csv_import-19.0.2.0.2.zip
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

1. Apps → `shopify_csv_import` version = **19.0.2.0.2**
2. Top menu "Shopify 导入" opens the "导入记录" list, with the "导入 Shopify CSV" button (admin only)
3. Import a small test CSV (a handful of products) without selecting an
   object-storage source first — confirm products/variants/categories/tags appear
   correctly and images land as standard Odoo binary images
4. If you use object storage: import a few products with the source selected.
   In "图片台账" every row should show 对象存储备份 = 已备份 and 前台显示来源 =
   Shopify CDN; the product page shows the local main image followed by the
   Shopify-hosted gallery. Run "对账" on the batch and confirm 对账结论 = 一致
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

- This module stores no credentials of its own. Uploads go through the
  `media.source` you pick in the import wizard — manage its URL, credentials
  and upload setting in media_picker.
- `ir.cron` "Shopify 导入批次" imports the products of uploaded CSVs (it is
  triggered immediately on upload, and checks every minute). Progress for every
  import is at "Shopify 导入" → "导入记录" (the default page of the menu). Both crons are `noupdate`:
  disabling one or changing its interval survives upgrades — the batch page
  warns when a cron a batch needs is disabled.
- `ir.cron` "Shopify 图片失效切换" (every 10 minutes) switches images whose
  Shopify link is confirmed dead to the object-storage backup. The detection
  itself is media_picker's link health check (its own cron and settings).
- `ir.cron` "Shopify 图片同步" starts every minute. Each run downloads 8 images
  in parallel, commits after every image, and stops starting new images once
  it has used half of the cron time limit (`limit_time_real_cron`, falling back
  to `limit_time_real`, 120 s by default) — so a run can't be killed mid-batch
  and roll its work back. While images remain, Odoo re-runs it straight away
  (up to 10 rounds per trigger). Measured on a real 284-product / 1,582-image
  Shopify export: ~40 images/minute, i.e. about 40 minutes for the whole catalog.
  Shopify CDN images are fetched pre-scaled to 1920 px (Odoo keeps at most
  1920 px anyway), with a fallback to the original URL.
