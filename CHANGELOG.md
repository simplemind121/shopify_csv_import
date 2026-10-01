# Changelog

All notable changes to `shopify_csv_import` are documented here.

## [19.0.1.1.1] - 2026-10-01

### Changed

- **"Shopify 导入" now opens the import list first** ("导入记录"), not the
  upload dialog. Previously you had to pick a file before you could get
  anywhere, and checking on a running import meant hunting for a second
  menu. New imports start from the "导入 Shopify CSV" button on the list
  (also still available as the "新建导入" menu); the empty list explains how
  to start.

## [19.0.1.1.0] - 2026-10-01

### Added

- **Import batches with live progress (`shopify.import.batch`).** Uploading a
  CSV now creates a batch and returns immediately; both product import and
  image sync run in background crons, so closing the browser no longer loses
  anything. "Shopify 导入 → 导入批次（进度）" lists every import, and a batch
  page shows a live panel (polls every 3 s, refreshes as soon as the tab
  becomes visible again): phase, overall %, products (created / updated /
  failed / warnings), an image bar split into *uploaded to Alist* / *local* /
  *CDN failed → local fallback* / *failed* / *pending*, what's happening right
  now ("正在并行下载 8 张图片", "正在上传 Alist：xxx.jpg"), speed, ETA and the
  latest problem. It also warns when one of the module's crons is disabled
  (the batch would otherwise just sit there).
- Batch actions: pause / resume, "立即处理一段" (run a slice now), "重试失败图片",
  and "重新上传回退本地的图片" — after fixing the Alist token, re-uploads the
  images that had fallen back to local storage and deletes the local copies
  once they're on the CDN, so the gallery doesn't show them twice.
- Product import runs in resumable slices (time-budgeted, commit every 10
  products), which also removes the HTTP timeout limit on very large CSVs.
  Images of a batch are only synced once its products are all imported.
- New cron "Shopify 导入批次" (triggered immediately on upload).
- Migration: image-queue rows created before this version are grouped into an
  "升级前的导入（历史记录）" batch so their progress is visible too.

### Fixed

- **Odoo created variant combinations that don't exist in Shopify**
  (Odoo builds the cartesian product of attribute values). Combinations that
  aren't in the CSV are archived; they're re-enabled if a later import has them.
- **Re-importing after an image changed in Shopify added a duplicate image.**
  Shopify image URLs carry a `?v=` version; image de-dup now ignores it,
  updates the existing queue row and re-syncs it in place (local images are
  overwritten, `media.bind` rows matched by the same key).
- Excel-mangled exports: leading `'` on SKUs/barcodes is stripped; barcodes in
  scientific notation (`9.78E+12`, digits already lost) are skipped with a
  warning; semicolon- or tab-delimited files are detected.
- Speed / ETA use the last 5 minutes of actual processing (`processed_at`), not
  the whole history — pauses, restarts or a sleeping machine no longer produce
  "about 4 hours" for a 10-minute job.

## [19.0.1.0.3] - 2026-10-01

Verified against the **real `media_picker` 19.0.3.4.3** (all 36 tests pass
with it, not just with the test double) and against a **real Alist server**
in a local Odoo 19 install.

### Fixed

- **CDN upload always failed with 403 when the Alist token is configured via
  `secret_ref`.** `media_picker` resolves the token as
  `os.environ[secret_ref]` first and `alist_token` second; the upload only
  read `alist_token`, so it sent no token and Alist rejected the write
  (`403 permission denied`) while media_picker's own read-only "test
  connection" still succeeded as guest. The upload now resolves the token
  exactly like `pem_alist_client._request`, and refuses to send an
  unauthenticated upload (clear message instead of a 403).
- **CDN failures were invisible.** A failed upload silently fell back to a
  local binary image and the queue row still said "done". Rows now record
  `storage` (CDN / local binary) and `cdn_error` (why the CDN path failed);
  both are shown in the queue list with a "CDN 失败、已回退本地" filter, and the
  "立即同步一批图片" notification warns when fallbacks happened. The failure is
  logged at WARNING instead of INFO.
- 403 from Alist now explains that the token's user lacks write permission
  on the target path.
- The image-sync cron is `noupdate`: enabling/disabling it or changing its
  interval in Settings is no longer reset by a module upgrade.

## [19.0.1.0.2] - 2026-09-30

Verified against a **real Shopify export** (284 products, 1,597 rows, 1,582
images): all 284 products imported with 0 failures in ~23 s; a field-by-field
comparison against the CSV (name, publish state, prices, per-variant prices,
cost, SKU, barcode, weight, categories, tags, description) found no
differences; re-importing the same file updated all 284 in place with no
duplicates; all images downloaded from the Shopify CDN through the real cron
runner.

### Fixed

- **Variant images were never linked to their variant.** In real Shopify
  exports `Variant Image` is (almost) always also one of the product's
  `Image Src` gallery images, and the queue de-duplicated by URL alone, so the
  variant row was always skipped. The de-dup key is now *(URL, variant)*.
  Variant images on single-variant products are skipped (nothing to link).
  On the CDN path the variant's `media.bind` gets its own `shopify_media_id`
  key so it doesn't overwrite the gallery entry for the same picture.
- **Image cron could stall forever on a slow network.** A batch ran in one
  transaction; real downloads take ~3 s each, so 30 images could exceed Odoo's
  cron time limit, get killed and rolled back, and restart on the same batch.
  The cron now commits after every image (`ir.cron._commit_progress`), stops
  starting new images after half the cron time limit, and reports the queue
  size so Odoo keeps draining it within the same trigger. The "立即同步一批图片"
  button uses the same time budget, so it can't hit the HTTP request timeout.

### Changed

- Images are downloaded 8 at a time in parallel (HTTP only; database writes stay
  sequential) and Shopify CDN images are requested pre-scaled to 1920 px
  (Odoo stores at most 1920 px; falls back to the original URL). Cron interval
  1 minute (was 2). Measured throughput went from ~5 to ~40 images/minute (all 1,589 images of the real export in about 40 minutes).

## [19.0.1.0.1] - 2026-09-30

First version verified end-to-end on a real Odoo 19.0 database (see
`dev/run_tests.sh`). 19.0.1.0.0 **could not be installed on Odoo 19** and
mis-imported every multi-variant product; upgrade directly to this version.

### Fixed

- **Install failure on Odoo 19**: the image-queue search view used
  `<group expand="0" string="...">`, which Odoo 19's view schema rejects
  (`Invalid view shopify.image.queue.search definition`).
- **Install failure on Odoo 19**: `data/ir_cron.xml` set `numbercall`, a field
  that no longer exists on `ir.cron`.
- **Multi-variant products collapsed into a single variant**: Shopify only
  writes `Option1/2/3 Name` on a product's first row; follow-up variant rows
  carry only the values. Option names are now taken from the whole row group,
  so e.g. a Color × Size product gets all 4 variants instead of 1.
- **Per-variant prices were never applied**: writing `product.product.lst_price`
  in Odoo just rewrites the template's `list_price` (every variant ends up with
  the last row's price). Prices are now mapped to Odoo's pricing model:
  template `list_price` = lowest variant price, the rest as
  `product.template.attribute.value.price_extra`. Price grids that can't be
  expressed that way (one combination priced independently) are imported as
  closely as possible and flagged with a `[警告]` line in the import log.
- **Variant images silently dropped** (consequence of the variant bug above);
  variant-specific images are now attached to the variant only, not duplicated
  into the shared product gallery.
- **"立即同步一批图片" undid its own work**: it reported the result by raising
  `UserError`, which rolls back the transaction, including the images it had
  just processed. It now returns a notification instead.
- **Queue "重试" button could skip the selected record**: it processed the
  first *N* pending rows by id instead of the rows you selected.
- A database error while storing one image (e.g. a corrupt file failing Odoo's
  image validation) could abort the whole cron batch; each image now runs in
  its own savepoint.
- Variant attributes: an existing same-name attribute with
  `create_variant != 'always'` is no longer reused (it can't generate variants).
- Deploy script: the package manifest lookup is pinned to
  `shopify_csv_import/__manifest__.py`, so passing the repo bundle
  (`shopify_csv_import-repo-bundle.tar.gz`, which matches the package glob) can
  never pick up another module's manifest.
- Manifest description: fixed an RST list formatting warning.

### Added

- CSV import hardening: clear error if the file has no `Handle` column (not a
  Shopify export); GB18030 fallback for exports re-saved by Excel.
- Downloaded image URLs that return a non-image `Content-Type` (e.g. an
  expired Shopify link returning HTML) now fail with a clear message.
- **Automated test suite** (`shopify_csv_import/tests/`, 26 tests) running on a
  real Odoo 19 database: CSV parsing, single/multi-variant import, price
  mapping, categories, tags/vendor filtering, idempotent re-import, per-product
  error isolation, the image queue (binary fallback, error handling, retry), and
  the Alist/`media_picker` path (upload, trusted-domain check, `media.bind`
  de-dup, fallbacks) with the network mocked.
- `dev/run_tests.sh` (Docker `odoo:19.0` or a local Odoo source checkout),
  `dev/stub_addons/media_picker` (a test double of the private `media_picker`
  API, **dev/CI only**), and a GitHub Actions workflow.

## [19.0.1.0.0] - 2026-09-30

### Added

- Initial release.
- **Import wizard**: parses Shopify's standard `products_export.csv`, groups rows
  by `Handle` (variant rows + extra-image rows), creates/updates `product.template`
  (name, sales/website description, `website_published`, price, cost, SKU,
  barcode, weight).
- **Variants**: auto-creates/reuses `product.attribute` / `product.attribute.value`
  from the Option1/2/3 columns, generates the variant combinations, and writes
  per-variant price/cost/SKU/barcode/weight to the matching `product.product`.
- **Categories**: splits Shopify's `Product Category` on `>` into a matching
  `product.public.category` tree (website) and `product.category` tree
  (internal), reusing existing nodes instead of duplicating them.
- **Tags**: Vendor (URL-looking junk values filtered out) and Shopify `Tags`
  imported as `product.tag`.
- **Idempotent re-import**: matches on the Shopify `Handle`, stored in
  `product.template.x_shopify_handle`. Re-importing the same CSV updates
  existing products instead of duplicating them.
- **Asynchronous image sync** via a `shopify.image.queue` model processed by an
  `ir.cron` (every 2 minutes, 30 images/batch by default), so large catalogs
  don't time out the import HTTP request. A "process a batch now" button is
  available on the wizard for immediate testing.
- **Optional CDN image path**: when a `product.media.source` (an Alist
  connection already configured through `media_picker`) is picked in the
  wizard, each image is downloaded, uploaded to Alist, resolved to a trusted
  URL via `media_picker`'s own `pem_alist_client.get_file`, and stored through
  `product.template.upsert_external_media_from_shopify` — the integration
  point `media_picker` itself defines for Shopify-style imports. No extra
  display fields, no custom QWeb overrides: the storefront gallery is entirely
  `media_picker`'s existing code.
- **Binary fallback**: when no Alist source is selected, or upload/resolution
  fails for a given image, that image is stored as a standard Odoo binary
  image (`image_1920` / `product.image`) instead, so the catalog always ends
  up with visible images either way.
- `deploy_shopify_csv_import.sh`: interactive-password, mount-auto-detecting
  install/upgrade script for Docker-based Odoo 19 deployments — pre-deploy
  backup, automatic `-i`/`-u` selection, post-deploy DB-state verification,
  restart, health check, log scan, and `--rollback`.

### Known limitations

- No chunked/paged commits for very large CSVs (thousands of products). Each
  product import runs inside its own savepoint, so one bad row can't roll
  back the whole batch, but there's no batched-commit strategy yet.
- The "does this Vendor value look like a URL" filter is a simple regex;
  unusual junk-data formats may need extra rules.
- The Alist upload call (`PUT /api/fs/put` in `_alist_put`) is implemented
  against the documented Alist v3 API. It hasn't been exercised against a real
  Alist instance yet — everything else in the image pipeline (`get_file`,
  domain trust check, `upsert_external_media_from_shopify`) reuses
  `media_picker` code that's already running in production.
