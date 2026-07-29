# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""Print-format PDF source: rendering, anchor resolution, placement fallbacks."""

from unittest.mock import patch

import fitz
import frappe
from frappe.tests.utils import FrappeTestCase

from nesscale_sign.services import pdf_service
from nesscale_sign.services.envelope_service import EnvelopeService
from nesscale_sign.services.template_service import TemplateService
from nesscale_sign.tests.utils import two_signers_single
from nesscale_sign.utils import files

ANCHOR = "[[sign:signer]]"
RENDER_TARGET = "nesscale_sign.services.print_service.render_document_pdf"


def make_anchor_pdf(pages: int = 2, anchor_page: int = 2, at=(100, 400)) -> bytes:
	"""A PDF with an anchor marker on one page, mimicking a print-format render."""
	doc = fitz.open()
	for i in range(pages):
		page = doc.new_page(width=595, height=842)
		page.insert_text((72, 72), f"Rendered page {i + 1}")
		if i + 1 == anchor_page:
			page.insert_text(at, ANCHOR, fontsize=10)
	data = doc.tobytes()
	doc.close()
	return data


def make_print_template(title="Print Tmpl", fields=None, publish=True):
	tmpl = TemplateService.create(
		{
			"title": title,
			"pdf_source": "Print Format",
			"ref_doctype": "ToDo",
			"signer_roles": [{"role_label": "Signer", "role_key": "signer", "signing_order": 1}],
		}
	)
	if fields is None:
		fields = [
			{
				"field_type": "Signature", "label": "Sign", "signer_role": "signer",
				"placement": "Anchor", "anchor_text": ANCHOR,
				"page": 1, "pos_x": 0.5, "pos_y": 0.5, "width": 0.2, "height": 0.06, "required": 1,
			},
			{
				"field_type": "Date Signed", "label": "Date", "signer_role": "signer",
				"placement": "Last Page",
				"page": 1, "pos_x": 0.6, "pos_y": 0.9, "width": 0.2, "height": 0.03,
			},
		]
	TemplateService(tmpl.name).save_fields(fields)
	if publish:
		TemplateService(tmpl.name).publish()
	return frappe.get_doc("NS Template", tmpl.name)


def make_todo() -> "frappe.Document":
	return frappe.get_doc({"doctype": "ToDo", "description": "Sign me"}).insert()


class TestAnchorEngine(FrappeTestCase):
	def test_find_anchors_reports_normalised_position(self):
		pdf = make_anchor_pdf(pages=2, anchor_page=2, at=(100, 400))
		found = pdf_service.find_anchors(pdf, {ANCHOR})
		self.assertIn(ANCHOR, found)
		anchor = found[ANCHOR]
		self.assertEqual(anchor["page"], 2)
		self.assertAlmostEqual(anchor["pos_x"], 100 / 595, delta=0.02)
		self.assertAlmostEqual(anchor["pos_y"], 400 / 842, delta=0.02)

	def test_remove_anchor_text_redacts_marker(self):
		pdf = make_anchor_pdf()
		cleaned = pdf_service.remove_anchor_text(pdf, {ANCHOR})
		self.assertEqual(pdf_service.find_anchors(cleaned, {ANCHOR}), {})
		self.assertEqual(pdf_service.get_page_count(cleaned), 2)

	def test_missing_anchor_is_a_noop(self):
		pdf = make_anchor_pdf()
		self.assertEqual(pdf_service.find_anchors(pdf, {"[[nope]]"}), {})
		self.assertEqual(pdf_service.remove_anchor_text(pdf, {"[[nope]]"}), pdf)


class TestPrintFormatTemplate(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_publish_without_static_pdf(self):
		tmpl = make_print_template("Publishable Print Tmpl")
		self.assertEqual(tmpl.status, "Active")
		self.assertFalse(tmpl.pdf_file)

	def test_ref_doctype_required(self):
		with self.assertRaises(frappe.ValidationError):
			TemplateService.create(
				{"title": "No Ref", "pdf_source": "Print Format"}
			)

	def test_auto_create_syncs_trigger_doctype(self):
		tmpl = TemplateService.create(
			{
				"title": "Synced Trigger",
				"pdf_source": "Print Format",
				"ref_doctype": "ToDo",
				"auto_create": 1,
				"trigger_event": "New",
				"signer_roles": [{"role_label": "Signer", "role_key": "signer"}],
			}
		)
		self.assertEqual(tmpl.trigger_doctype, "ToDo")


class TestPrintFormatEnvelope(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def test_envelope_generates_own_pdf_and_resolves_placements(self):
		tmpl = make_print_template("Env Print Tmpl")
		todo = make_todo()
		rendered = make_anchor_pdf(pages=2, anchor_page=2, at=(100, 400))

		with patch(RENDER_TARGET, return_value=rendered) as render:
			env = EnvelopeService.create_from_template(
				tmpl.name,
				{
					"signers": two_signers_single(),
					"source_doctype": "ToDo",
					"source_name": todo.name,
				},
			)
		render.assert_called_once()

		# The envelope owns a generated (anchor-free) source PDF.
		self.assertTrue(env.source_pdf)
		self.assertEqual(env.page_count, 2)
		content = files.read_file_content(env.source_pdf)
		self.assertEqual(pdf_service.find_anchors(content, {ANCHOR}), {})

		fields = {
			f.label: f
			for f in map(
				frappe._dict,
				frappe.get_all(
					"NS Envelope Field",
					filters={"envelope": env.name},
					fields=["label", "page", "pos_x", "pos_y", "placement"],
				),
			)
		}
		# Anchor field snapped to the marker on page 2.
		self.assertEqual(fields["Sign"].page, 2)
		self.assertAlmostEqual(fields["Sign"].pos_x, 100 / 595, delta=0.02)
		# Last Page field moved from designer page 1 to the final page.
		self.assertEqual(fields["Date"].page, 2)

	def test_source_document_required(self):
		tmpl = make_print_template("Src Required Tmpl")
		with self.assertRaises(frappe.ValidationError):
			EnvelopeService.create_from_template(tmpl.name, {"signers": two_signers_single()})

	def test_wrong_source_doctype_rejected(self):
		tmpl = make_print_template("Wrong Src Tmpl")
		with self.assertRaises(frappe.ValidationError):
			EnvelopeService.create_from_template(
				tmpl.name,
				{
					"signers": two_signers_single(),
					"source_doctype": "User",
					"source_name": "Administrator",
				},
			)

	def test_missing_anchor_falls_back_to_designer_coords(self):
		tmpl = make_print_template("Fallback Tmpl")
		todo = make_todo()
		# Render without the marker: anchor cannot be resolved.
		rendered = make_anchor_pdf(pages=1, anchor_page=0)

		with patch(RENDER_TARGET, return_value=rendered):
			env = EnvelopeService.create_from_template(
				tmpl.name,
				{
					"signers": two_signers_single(),
					"source_doctype": "ToDo",
					"source_name": todo.name,
				},
			)
		sign = frappe._dict(
			frappe.get_all(
				"NS Envelope Field",
				filters={"envelope": env.name, "label": "Sign"},
				fields=["page", "pos_x", "pos_y"],
			)[0]
		)
		# Designer coordinates kept, page clamped to the 1-page document.
		self.assertEqual(sign.page, 1)
		self.assertAlmostEqual(sign.pos_x, 0.5, delta=0.001)
