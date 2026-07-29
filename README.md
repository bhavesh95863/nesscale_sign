# Nesscale Sign

An e-signature app for Frappe. Upload a PDF, place the fields, and send it out for signature.

## Features

- Upload PDF documents and reuse them as templates
- **Generate the PDF from any DocType's Print Format** — each envelope renders its own PDF from the reference document (e.g. a Sales Invoice) at creation time
- Drag and drop fields onto the document (signature, initials, name, email, date, text, checkbox, dropdown)
- Position fields on generated PDFs with anchor markers or last-page placement
- Repeat a field across multiple pages
- Multiple signers with sequential or parallel signing order
- Sign without logging in, using a secure link
- Draw, type, or upload a signature
- Signed PDF with a certificate and audit trail
- Email invitations, reminders, and expiry
- Create documents automatically from a Frappe DocType event (optional)
- Editable email templates
- Dark mode UI

## Print Format templates

Instead of uploading a static PDF, a template can generate its PDF from a
document's Print Format. Set **PDF Source = Print Format** on the template and
pick the **Reference DocType** (plus an optional Print Format and Letter Head).
When an envelope is created — manually via `create_from_document`, or
automatically from a DocType trigger — the reference document is rendered to
PDF and that copy belongs to the envelope.

Because generated PDFs vary in length, each field has a **Placement**:

| Placement | Behaviour |
|---|---|
| Absolute | Fixed page + coordinates (default; same as static templates) |
| Last Page | The designer coordinates are applied to the document's final page |
| Anchor | The field snaps to a text marker in the rendered document |

For anchors, put a marker in the print format HTML where the field should go,
e.g.:

```html
<span style="color: #ffffff;">[[sign:customer]]</span>
```

and set the field's **Anchor Text** to `[[sign:customer]]`. The marker is
redacted from the generated PDF, so it never appears in the sent or signed
document. If a marker is missing from a particular render, the field falls
back to its designer coordinates (clamped to the last page).

Fields with a **Map From Field** key are prefilled from the reference document
as before, and envelopes are voided automatically if the reference document is
cancelled or deleted.

## Screenshots

![Dashboard](docs/images/dashboard.png)

![Field designer](docs/images/field-designer.png)

![Signing](docs/images/signing.png)

## Documentation

Full documentation, with screenshots: https://sign.bhavesh.tech/nesscale-sign-home

## Install

```bash
cd your-bench
bench get-app https://github.com/your-org/nesscale_sign --branch version-16
bench install-app nesscale_sign
```

The app opens at `/nesscale-sign`. Signing links open at `/sign/<token>`.

## Tests

```bash
bench --site <site> run-tests --app nesscale_sign
```

## Support

If this app is useful to you, you can support its development:

[Donate via PayPal](https://paypal.me/nesscale)

## License

AGPL-3.0. See [license.txt](license.txt).

Copyright (c) Nesscale Solutions Pvt Ltd — info@nesscale.com
