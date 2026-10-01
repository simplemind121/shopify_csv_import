# shopify_csv_import

**Odoo 19 · Bulk-import a Shopify product CSV export into `website_sale` · optional Alist/B2 CDN images via `media_picker`**

| Field | Value |
|---|---|
| **Latest version** | [`19.0.1.1.1`](./CHANGELOG.md#19111---2026-10-01) |
| **Module name** | `shopify_csv_import` |
| **Odoo** | 19 Community |
| **Depends on** | `website_sale`, `product`, `media_picker` |
| **License (module)** | LGPL-3 (see package `__manifest__.py`) |
| **Repository** | Open source — full module source + deploy script |

中文说明：[README.zh-CN.md](./README.zh-CN.md) · Pin file: [`VERSION`](./VERSION) · Full history: [`CHANGELOG.md`](./CHANGELOG.md) · Deploy runbook: [`docs/DEPLOY.md`](./docs/DEPLOY.md) · Security: [`SECURITY.md`](./SECURITY.md)

---

## Overview

Shopify's product CSV export is one file with 60+ columns and one-or-more
rows per product (variant rows + extra-image rows). `shopify_csv_import`
parses that export directly inside Odoo and turns it into real
`product.template` / `product.product` records on your `website_sale`
storefront — no spreadsheet wrangling, no manual field mapping through
Odoo's generic Import screen.

It's built specifically to pair with [`media_picker`](https://github.com/simplemind121/media_picker)
(if you have a private/separate repo for that) for CDN-backed product
images, but works standalone with plain Odoo binary images too.

### Goals

- One-click import of a full Shopify catalog: products, variants, categories,
  tags, prices
- Re-running the same CSV **updates** existing products (matched on Shopify's
  `Handle`) instead of duplicating them
- Image sync that can't time out the import request, with an optional path
  straight to your existing Alist/B2 CDN setup
- No reinvented image-display logic — CDN images go through `media_picker`'s
  own, already-in-production `media.bind` gallery

### Non-goals

- Ongoing two-way sync with Shopify (this is a one-shot / repeatable **import**,
  not a live connector)
- Order/inventory sync
- Any Shopify **write** access — the module only ever reads the CSV file you
  give it

---

## What's in this repository

| Path | Purpose |
|---|---|
| `shopify_csv_import/` | The installable Odoo module source |
| `shopify_csv_import-<version>.zip` *(release asset)* | Installable archive, attached to GitHub Releases |
| `deploy_shopify_csv_import.sh` | Interactive, backup-then-verify install/upgrade script for Docker VPS layouts |
| `VERSION` | Single-line current recommended version |
| `CHANGELOG.md` | User-facing change history |
| `docs/` | Deploy & operational notes |
| `shopify_csv_import/tests/` | Odoo test suite + a sample Shopify CSV export |
| `dev/run_tests.sh` | Runs the test suite on a throwaway Odoo 19 database |
| `dev/stub_addons/media_picker/` | Test double of `media_picker`'s API — **dev/CI only, never deploy** |

---

## Feature matrix (current: 19.0.1.1.1)

| Area | Status |
|---|---|
| Product / variant / price / cost / SKU / barcode / weight import | Supported |
| Per-variant prices | Supported as Odoo *base price + attribute extra*; grids that can't be expressed that way are flagged in the import log |
| Multi-level website + internal category mapping | Supported |
| Vendor → brand tag (URL-junk filtered) + Shopify Tags → tags | Supported |
| Idempotent re-import (dedup by Handle) | Supported |
| Background import batches with live progress page (pause / resume / retry) | Supported |
| Async image sync via `ir.cron` queue | Supported |
| Binary image fallback (no Alist configured) | Supported |
| Alist/B2 CDN image sync via `media_picker`'s `media.bind` | Supported |
| Compare-at-price / gift cards / SEO metafields | Not mapped (not needed for the initial use case) |
| Chunked commits for very large catalogs | Supported (resumable, time-budgeted slices) |
| Automated tests on a real Odoo 19 DB | Yes — 53 tests + verified on a real 284-product Shopify export, see below |

---

## Quick start

```bash
# 1) Clone this repo, or download the Release assets:
#    shopify_csv_import-19.0.1.1.1.zip
#    deploy_shopify_csv_import.sh

# 2) Same directory on the VPS
chmod +x deploy_shopify_csv_import.sh
sudo ./deploy_shopify_csv_import.sh --target staging   # test first
sudo ./deploy_shopify_csv_import.sh                     # then production

# 3) Odoo → "Shopify 导入" → button "导入 Shopify CSV" → upload your export
#    You land on the batch page; progress keeps running if you close it.
#    Come back any time: "Shopify 导入" opens the import list (导入记录)
```

Full procedure, options, and rollback: **[docs/DEPLOY.md](./docs/DEPLOY.md)**.

---

## How the image pipeline works

1. The import wizard queues every image URL from the CSV into a
   `shopify.image.queue` row (main image / extra image / variant image).
2. An `ir.cron` job processes the queue in small batches so a 1,500-image
   catalog can't time out the HTTP request that started the import.
3. If you picked a `product.media.source` (an Alist connection already set
   up through `media_picker`) in the wizard: the image is downloaded,
   `PUT`-uploaded to Alist, resolved to a trusted direct URL via
   `media_picker`'s own `pem_alist_client.get_file`, and handed to
   `product.template.upsert_external_media_from_shopify` — the exact
   integration point `media_picker` defines for Shopify-style imports.
4. Otherwise (or if step 3 fails for a given image), it falls back to a
   standard Odoo binary image, so the catalog is never left without pictures.

No QWeb template overrides live in this module — the storefront gallery
display is entirely `media_picker`'s own, already-verified code.

---

## Running the tests

```bash
./dev/run_tests.sh                 # Docker: throwaway postgres:16 + odoo:19.0
```

No Docker Hub access? Point it at a local Odoo 19 source checkout instead:

```bash
ODOO_SRC=~/src/odoo-19 PYTHON=~/venvs/odoo19/bin/python \
DB_HOST=127.0.0.1 DB_PORT=5432 DB_USER=odoo DB_PASSWORD=odoo ./dev/run_tests.sh
```

The suite installs the module next to `dev/stub_addons/media_picker` (a test
double; set `MEDIA_PICKER_DIR=/path/to/media_picker` to test against the real
addon) and mocks all network calls (Shopify image downloads, Alist upload,
`get_file`). The same script runs in GitHub Actions on every push / PR.

---

## Versioning policy

Odoo-style five-segment versions: `19.0.<minor>.<patch>.<hotfix>`.

**Upgrade:** always `-u shopify_csv_import` via the deploy script. Re-running
`docker exec odoo -u ...` by hand works too, but skips the backup/verify
steps the script gives you for free.

---

## Compatibility

| Component | Expectation |
|---|---|
| Odoo | 19.0 Community, `website_sale` installed |
| Required dependency | `media_picker` (for the `media.bind` / `product.media.source` API this module calls; the image pipeline falls back to binary storage if you never select a source, but the module still won't install without the addon present) |

---

## Security

- No Alist/S3/B2 credentials are stored by this module — see
  [`SECURITY.md`](./SECURITY.md)
- Report issues privately, not as public GitHub issues

---

## License

Module license: **LGPL-3** (declared in the Odoo manifest). Deploy tooling in
this repository is provided as-is.
