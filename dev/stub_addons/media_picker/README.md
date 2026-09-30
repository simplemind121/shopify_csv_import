# media_picker — TEST STUB (dev / CI only)

**Do not deploy this.** It is a minimal test double of the private
`media_picker` addon so `shopify_csv_import`'s test suite can install and
run without access to the real module. It only implements the API surface
`shopify_csv_import` calls:

- `product.media.source` (`alist_url`, `alist_token`, `trusted_domains`, `_check_domain_trusted()`)
- `odoo.addons.media_picker.models.pem_alist_client.get_file(source, path)`
- `media.bind` + `product.template.upsert_external_media_from_shopify()` / `use_external_media`

If your real `media_picker` checkout is available, point `dev/run_tests.sh`
at it instead (`MEDIA_PICKER_DIR=/path/to/media_picker ./dev/run_tests.sh`).
