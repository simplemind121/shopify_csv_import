# media_picker — TEST STUB (dev / CI only)

**Do not deploy this.** It is a minimal test double of the private
`media_picker` addon so `shopify_csv_import`'s test suite can install and
run without access to the real module. It mimics the part of **media_picker 19.0.3.8.x** that `shopify_csv_import`
calls:

- `media.source` (`upload_enabled`, `root_path`, `trusted_domains`, `upload_media()`, `_check_domain_trusted()`)
- `media.bind` (URL, main flag, source, link-health fields, the trusted-domain
  constraint, `upsert_external_media_from_shopify()`)
- `product.template` (`use_external_media`, `external_media_mode`, `media_bind_ids`, `mp_main_sync_status`)

The real module also syncs the main external image into `image_1920` and runs
the link health checks; the stub only keeps the fields.

If your real `media_picker` checkout is available, point `dev/run_tests.sh`
at it instead (`MEDIA_PICKER_DIR=/path/to/media_picker ./dev/run_tests.sh`).
