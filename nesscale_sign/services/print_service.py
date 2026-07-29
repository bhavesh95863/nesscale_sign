# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""Render reference documents to PDF through their Print Format.

Used by templates whose ``pdf_source`` is "Print Format": instead of a static
uploaded PDF, each envelope generates its own source PDF from the reference
document (e.g. a Sales Invoice) at creation time.
"""

import frappe
from frappe import _

from nesscale_sign.utils.constants import PdfSource


def render_document_pdf(
	doctype: str,
	name: str,
	print_format: str | None = None,
	letterhead: str | None = None,
) -> bytes:
	"""Render a document to PDF bytes using its print format.

	Falls back to the Standard print format when ``print_format`` is empty.
	"""
	if not frappe.db.exists(doctype, name):
		frappe.throw(_("Reference document {0} {1} not found.").format(doctype, name))
	return frappe.get_print(
		doctype,
		name,
		print_format=print_format or None,
		as_pdf=True,
		letterhead=letterhead or None,
	)


def render_for_template(tmpl: "frappe.Document", doctype: str, name: str) -> bytes:
	"""Render the source PDF for a print-format template from its reference document."""
	if (tmpl.get("pdf_source") or PdfSource.STATIC) != PdfSource.PRINT_FORMAT:
		frappe.throw(_("Template {0} does not use a print format source.").format(tmpl.name))
	if doctype != tmpl.ref_doctype:
		frappe.throw(
			_("Template {0} expects a {1} document, got {2}.").format(
				tmpl.name, tmpl.ref_doctype, doctype
			)
		)
	return render_document_pdf(doctype, name, tmpl.print_format, tmpl.print_letterhead)
