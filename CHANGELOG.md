# Changelog

All notable changes to `shopify_csv_import` are documented here.

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
