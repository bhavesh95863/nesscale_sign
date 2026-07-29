# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""Template lifecycle service: PDF management, field persistence, versioning."""

import json

import frappe
from frappe import _
from frappe.utils import cint, slug

from nesscale_sign.services import pdf_service, print_service
from nesscale_sign.utils import files
from nesscale_sign.utils.constants import PdfSource

# Field attributes copied between template fields, version snapshots and
# envelope field instances.
FIELD_ATTRS = [
	"field_key", "field_type", "label", "signer_role", "page",
	"placement", "anchor_text",
	"pos_x", "pos_y", "width", "height", "required", "read_only",
	"placeholder", "default_value", "options", "font_size", "font_family",
	"max_length", "validation", "repeat_group", "mapping_key",
]

TRIGGER_FIELDS = [
	"auto_create", "trigger_doctype", "trigger_event", "trigger_auto_send",
	"trigger_value_field", "trigger_value_to", "trigger_date_field", "trigger_days",
]

PRINT_SOURCE_FIELDS = ["pdf_source", "ref_doctype", "print_format", "print_letterhead"]


class TemplateService:
	def __init__(self, template: str | None = None):
		self.template = template

	# ------------------------------------------------------------------ create
	@classmethod
	def create(cls, data: dict) -> "frappe.Document":
		doc = frappe.new_doc("NS Template")
		doc.title = data.get("title") or _("Untitled Template")
		doc.organization = data.get("organization") or _default_org()
		doc.description = data.get("description")
		doc.routing_type = data.get("routing_type") or "Sequential"
		doc.expiry_days = cint(data.get("expiry_days")) or 30
		doc.email_subject = data.get("email_subject")
		doc.email_message = data.get("email_message")
		for field in TRIGGER_FIELDS + PRINT_SOURCE_FIELDS:
			if field in data:
				doc.set(field, data[field])
		validate_print_source(doc)
		validate_trigger(doc)
		if data.get("pdf_file"):
			doc.pdf_file = data["pdf_file"]
		for role in data.get("signer_roles") or []:
			doc.append("signer_roles", _normalise_role(role))
		doc.insert()
		svc = cls(doc.name)
		if doc.pdf_file:
			svc.refresh_page_count()
		return doc.reload() or frappe.get_doc("NS Template", doc.name)

	def _doc(self) -> "frappe.Document":
		return frappe.get_doc("NS Template", self.template)

	def refresh_page_count(self):
		doc = self._doc()
		if not doc.pdf_file:
			return
		content = files.read_file_content(doc.pdf_file)
		doc.db_set("page_count", pdf_service.get_page_count(content))

	# ------------------------------------------------------------------ pdf
	def set_pdf(self, file_url: str) -> "frappe.Document":
		doc = self._doc()
		doc.pdf_file = file_url
		content = files.read_file_content(file_url)
		doc.page_count = pdf_service.get_page_count(content)
		doc.save()
		return doc

	def page_metrics(self, document: str | None = None) -> list[dict]:
		doc = self._doc()
		if is_print_source(doc):
			try:
				return pdf_service.get_page_metrics(self.render_preview(document))
			except frappe.DoesNotExistError:
				return []
		if not doc.pdf_file:
			return []
		content = files.read_file_content(doc.pdf_file)
		return pdf_service.get_page_metrics(content)

	def render_preview(self, document: str | None = None) -> bytes:
		"""PDF bytes for the designer preview.

		Static templates return their uploaded PDF. Print-format templates render
		the given reference document — or the most recently modified one when no
		document is specified.
		"""
		doc = self._doc()
		if not is_print_source(doc):
			if not doc.pdf_file:
				frappe.throw(_("Template has no PDF document."))
			return files.read_file_content(doc.pdf_file)
		name = document or frappe.db.get_value(
			doc.ref_doctype, {}, "name", order_by="modified desc"
		)
		if not name:
			frappe.throw(
				_("No {0} document exists yet to preview this template.").format(doc.ref_doctype),
				frappe.DoesNotExistError,
			)
		return print_service.render_for_template(doc, doc.ref_doctype, name)

	# ------------------------------------------------------------------ roles
	def save_roles(self, roles: list[dict]) -> "frappe.Document":
		doc = self._doc()
		doc.set("signer_roles", [])
		for role in roles:
			doc.append("signer_roles", _normalise_role(role))
		doc.save()
		return doc

	# ------------------------------------------------------------------ fields
	def get_fields(self) -> list[dict]:
		return frappe.get_all(
			"NS Template Field",
			filters={"template": self.template},
			fields=["name", *FIELD_ATTRS],
			order_by="page asc, creation asc",
			limit_page_length=0,
		)

	def save_fields(self, fields: list[dict]) -> int:
		"""Replace the template's field set atomically (designer save)."""
		existing = frappe.get_all(
			"NS Template Field", filters={"template": self.template}, pluck="name"
		)
		for name in existing:
			frappe.delete_doc("NS Template Field", name, ignore_permissions=True, force=True)

		count = 0
		for field in fields:
			row = frappe.new_doc("NS Template Field")
			row.template = self.template
			_apply_field_attrs(row, field)
			if not row.field_key:
				row.field_key = frappe.generate_hash(length=10)
			row.flags.ignore_permissions = True
			row.insert(ignore_permissions=True)
			count += 1
		return count

	# ------------------------------------------------------------------ version
	def create_version(self, notes: str | None = None) -> "frappe.Document":
		doc = self._doc()
		fields = self.get_fields()
		roles = [
			{k: r.get(k) for k in ("role_label", "role_key", "signing_order", "color", "optional")}
			for r in [row.as_dict() for row in doc.signer_roles]
		]
		version_no = cint(doc.version_count) + 1

		frappe.db.set_value(
			"NS Template Version",
			{"template": self.template, "is_current": 1},
			"is_current",
			0,
			update_modified=False,
		)
		version = frappe.get_doc(
			{
				"doctype": "NS Template Version",
				"template": self.template,
				"version_no": version_no,
				"is_current": 1,
				"pdf_file": doc.pdf_file,
				"page_count": doc.page_count,
				"fields_snapshot": json.dumps(fields, default=str),
				"roles_snapshot": json.dumps(roles, default=str),
				"notes": notes,
			}
		)
		version.insert(ignore_permissions=True)
		doc.db_set("current_version", version.name)
		doc.db_set("version_count", version_no)
		return version

	def publish(self) -> "frappe.Document":
		doc = self._doc()
		if is_print_source(doc):
			validate_print_source(doc)
		elif not doc.pdf_file:
			frappe.throw(_("Cannot publish a template without a PDF."))
		if not doc.signer_roles:
			frappe.throw(_("Add at least one signer role before publishing."))
		validate_trigger(doc)
		self.create_version()
		doc.reload()
		doc.db_set("status", "Active")
		return doc

	def archive(self) -> "frappe.Document":
		doc = self._doc()
		doc.db_set("status", "Archived")
		return doc

	def duplicate(self, new_title: str | None = None) -> "frappe.Document":
		source = self._doc()
		clone = frappe.copy_doc(source)
		clone.title = new_title or f"{source.title} (Copy)"
		clone.status = "Draft"
		clone.current_version = None
		clone.version_count = 0
		clone.insert()
		for field in self.get_fields():
			row = frappe.new_doc("NS Template Field")
			row.template = clone.name
			_apply_field_attrs(row, field)
			row.flags.ignore_permissions = True
			row.insert(ignore_permissions=True)
		return clone


# --------------------------------------------------------------------- helpers
def _default_org() -> str | None:
	return frappe.db.get_single_value("NS Settings", "default_organization") or frappe.db.get_value(
		"NS Organization", {"disabled": 0}, "name"
	)


def _normalise_role(role: dict) -> dict:
	label = role.get("role_label") or role.get("label") or "Signer"
	return {
		"role_label": label,
		"role_key": role.get("role_key") or slug(label),
		"signing_order": cint(role.get("signing_order")) or 1,
		"color": role.get("color") or "#2563EB",
		"optional": cint(role.get("optional")),
		"source_email_field": role.get("source_email_field"),
		"source_name_field": role.get("source_name_field"),
		"manual_email": role.get("manual_email"),
		"manual_name": role.get("manual_name"),
	}


def validate_trigger(doc):
	"""Auto-creation must reference a normal DocType (not child/single)."""
	if not doc.get("auto_create"):
		return
	if not doc.get("trigger_doctype"):
		frappe.throw(_("Select a Reference DocType for auto-creation."))
	meta = frappe.get_meta(doc.trigger_doctype)
	if meta.istable or meta.issingle:
		frappe.throw(_("Auto-creation requires a normal DocType (not a child table or single)."))


def is_print_source(doc) -> bool:
	return (doc.get("pdf_source") or PdfSource.STATIC) == PdfSource.PRINT_FORMAT


def validate_print_source(doc):
	"""Print-format templates need a valid reference DocType + matching print format."""
	if not is_print_source(doc):
		return
	if not doc.get("ref_doctype"):
		frappe.throw(_("Select a Reference DocType for the print format source."))
	meta = frappe.get_meta(doc.ref_doctype)
	if meta.istable or meta.issingle:
		frappe.throw(_("The print format source requires a normal DocType (not a child table or single)."))
	if doc.get("print_format"):
		pf_doctype = frappe.db.get_value("Print Format", doc.print_format, "doc_type")
		if pf_doctype and pf_doctype != doc.ref_doctype:
			frappe.throw(
				_("Print Format {0} belongs to {1}, not {2}.").format(
					doc.print_format, pf_doctype, doc.ref_doctype
				)
			)
	if doc.get("auto_create"):
		# The automation trigger must watch the same DocType the PDF is
		# rendered from; default it when unset.
		if not doc.get("trigger_doctype"):
			doc.trigger_doctype = doc.ref_doctype
		elif doc.trigger_doctype != doc.ref_doctype:
			frappe.throw(
				_("Auto-creation trigger DocType must match the print format's Reference DocType.")
			)


def _apply_field_attrs(row: "frappe.Document", field: dict):
	for attr in FIELD_ATTRS:
		if attr in field and field[attr] is not None:
			row.set(attr, field[attr])
