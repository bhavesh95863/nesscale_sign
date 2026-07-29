# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""Envelope lifecycle service — the orchestration core.

Builds envelopes from templates (or ad-hoc PDFs), sends them, drives the
signing workflow, finalises the immutable signed PDF + certificate, and emits
notifications, audit entries and webhooks at each transition.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from nesscale_sign.services import pdf_service, print_service
from nesscale_sign.services.audit_service import AuditService
from nesscale_sign.services.notification_service import NotificationService
from nesscale_sign.services.template_service import TemplateService, is_print_source
from nesscale_sign.services.workflow_service import WorkflowService
from nesscale_sign.utils import files
from nesscale_sign.utils.constants import (
	DEFAULT_SIGNER_PALETTE,
	AuditAction,
	EnvelopeStatus,
	FieldPlacement,
	SignerStatus,
)

ENVELOPE_FIELD_ATTRS = [
	"field_key", "field_type", "label", "signer_role", "page",
	"placement", "anchor_text",
	"pos_x", "pos_y", "width", "height", "required", "read_only",
	"options", "default_value", "font_size", "font_family", "repeat_group", "mapping_key",
]


class EnvelopeService:
	def __init__(self, envelope: str | None = None):
		self.envelope = envelope

	def _doc(self) -> "frappe.Document":
		return frappe.get_doc("NS Envelope", self.envelope)

	# --------------------------------------------------------------- creation
	@classmethod
	def create_from_template(cls, template: str, data: dict) -> "frappe.Document":
		tmpl = frappe.get_doc("NS Template", template)
		is_print = is_print_source(tmpl)

		# Optional reference document (for field prefill + reversal linkage);
		# mandatory when the PDF is generated from its print format.
		source_doc = _resolve_source_doc(data.get("source_doctype"), data.get("source_name"))
		template_fields = TemplateService(tmpl.name).get_fields()

		pdf_bytes = anchor_positions = None
		if is_print:
			if not source_doc:
				frappe.throw(
					_("Template {0} generates its PDF from a {1} document. Pass source_doctype and source_name.").format(
						tmpl.name, tmpl.ref_doctype
					)
				)
			pdf_bytes = print_service.render_for_template(tmpl, source_doc.doctype, source_doc.name)
			anchors = {
				f.get("anchor_text")
				for f in template_fields
				if f.get("placement") == FieldPlacement.ANCHOR and f.get("anchor_text")
			}
			anchor_positions = pdf_service.find_anchors(pdf_bytes, anchors)
			pdf_bytes = pdf_service.remove_anchor_text(pdf_bytes, anchors)
		elif not tmpl.pdf_file:
			frappe.throw(_("Template has no PDF document."))

		env = frappe.new_doc("NS Envelope")
		env.title = data.get("title") or tmpl.title
		env.organization = tmpl.organization
		env.template = tmpl.name
		env.template_version = tmpl.current_version
		env.routing_type = data.get("routing_type") or tmpl.routing_type
		env.source_pdf = None if is_print else tmpl.pdf_file
		env.page_count = pdf_service.get_page_count(pdf_bytes) if is_print else tmpl.page_count
		env.email_subject = data.get("email_subject") or tmpl.email_subject
		env.email_message = data.get("email_message") or tmpl.email_message
		env.message = data.get("message")
		cls._apply_signers(env, data.get("signers") or [], tmpl)

		if source_doc:
			env.metadata_json = json.dumps(
				{"ref_doctype": source_doc.doctype, "ref_name": source_doc.name}
			)
		env.insert()

		if is_print:
			# Each envelope owns its generated source PDF (unlike static
			# templates, which share the template's file).
			pdf_file = files.save_private_file(
				f"{env.name}-source.pdf", pdf_bytes,
				attached_to_doctype="NS Envelope", attached_to_name=env.name,
				attached_to_field="source_pdf",
			)
			env.db_set("source_pdf", pdf_file.file_url)

		svc = cls(env.name)
		svc._materialise_fields(
			env, template_fields, source_doc=source_doc, anchor_positions=anchor_positions
		)
		AuditService(env.name).log(AuditAction.CREATED, details=f"From template {tmpl.name}")
		return frappe.get_doc("NS Envelope", env.name)

	@classmethod
	def create_adhoc(cls, data: dict) -> "frappe.Document":
		"""Create an envelope from a directly uploaded PDF + inline field layout."""
		if not data.get("pdf_file"):
			frappe.throw(_("A PDF file is required."))
		content = files.read_file_content(data["pdf_file"])

		env = frappe.new_doc("NS Envelope")
		env.title = data.get("title") or _("Untitled Document")
		env.organization = data.get("organization") or _default_org()
		env.routing_type = data.get("routing_type") or "Sequential"
		env.source_pdf = data["pdf_file"]
		env.page_count = pdf_service.get_page_count(content)
		env.email_subject = data.get("email_subject")
		env.email_message = data.get("email_message")
		env.message = data.get("message")
		cls._apply_signers(env, data.get("signers") or [], None)
		env.insert()

		cls(env.name)._materialise_fields(env, data.get("fields") or [])
		AuditService(env.name).log(AuditAction.CREATED, details="Ad-hoc envelope")
		return frappe.get_doc("NS Envelope", env.name)

	@staticmethod
	def _apply_signers(env, signers: list[dict], tmpl):
		env.set("signers", [])
		for idx, s in enumerate(signers):
			env.append(
				"signers",
				{
					"signer_name": s.get("signer_name") or s.get("name"),
					"signer_email": (s.get("signer_email") or s.get("email") or "").strip().lower(),
					"role_key": s.get("role_key"),
					"role_label": s.get("role_label") or s.get("role"),
					"signing_order": cint(s.get("signing_order")) or (idx + 1),
					"color": s.get("color") or DEFAULT_SIGNER_PALETTE[idx % len(DEFAULT_SIGNER_PALETTE)],
					"auth_method": s.get("auth_method") or "None",
					"status": SignerStatus.PENDING,
				},
			)

	def _materialise_fields(self, env, source_fields: list[dict], source_doc=None, anchor_positions=None):
		"""Snapshot template/inline fields into per-envelope ``NS Envelope Field`` rows.

		Fields carrying a ``mapping_key`` are prefilled (and locked) from the
		reference document when one is supplied. Last Page / Anchor placements
		are resolved to absolute page coordinates against the envelope's PDF.
		"""
		role_to_email = {
			(s.role_key or "").lower(): s.signer_email for s in env.signers if s.role_key
		}
		# Fallback: single-signer envelopes bind every field to that signer.
		default_email = env.signers[0].signer_email if len(env.signers) == 1 else None
		page_count = cint(env.page_count)

		for field in source_fields:
			role_key = (field.get("signer_role") or "").lower()
			signer_email = role_to_email.get(role_key) or default_email
			row = frappe.new_doc("NS Envelope Field")
			row.envelope = env.name
			row.signer_email = signer_email
			for attr in ENVELOPE_FIELD_ATTRS:
				if field.get(attr) is not None:
					row.set(attr, field.get(attr))
			_resolve_placement(row, field, anchor_positions or {}, page_count)
			if not row.field_key:
				row.field_key = frappe.generate_hash(length=10)
			if field.get("default_value") and not row.read_only:
				row.value = field.get("default_value")
			mapped = _read_mapped_value(source_doc, field.get("mapping_key"))
			if mapped not in (None, ""):
				row.value = mapped
				row.filled = 1
				row.read_only = 1
			row.flags.ignore_permissions = True
			row.insert(ignore_permissions=True)

	def remap_field_signers(self):
		"""Re-point each envelope field to its role's (possibly newly filled)
		signer email. Used after a Draft envelope's signers are edited."""
		env = self._doc()
		role_to_email = {
			(s.role_key or "").lower(): s.signer_email for s in env.signers if s.role_key
		}
		default_email = env.signers[0].signer_email if len(env.signers) == 1 else None
		for f in frappe.get_all(
			"NS Envelope Field", filters={"envelope": env.name}, fields=["name", "signer_role"]
		):
			email = role_to_email.get((f.signer_role or "").lower()) or default_email
			frappe.db.set_value("NS Envelope Field", f.name, "signer_email", email, update_modified=False)

	# --------------------------------------------------------------- send
	def send(self) -> "frappe.Document":
		env = self._doc()
		self._validate_sendable(env)

		for signer in env.signers:
			if not signer.token:
				signer.token = frappe.generate_hash(length=40)

		if not env.expires_on:
			days = _expiry_days(env)
			if days:
				env.expires_on = add_to_date(now_datetime(), days=days)

		env.status = EnvelopeStatus.SENT
		env.sent_on = now_datetime()

		workflow = WorkflowService(env)
		workflow.envelope.current_order = workflow.current_order() or 0
		to_notify = workflow.signers_to_activate_on_send()
		workflow.mark_sent(to_notify)
		env.save()

		notifier = NotificationService(env)
		for signer in to_notify:
			notifier.send_invitation(signer)

		AuditService(env.name).log(AuditAction.SENT, details=f"Sent to {len(to_notify)} signer(s)")
		return frappe.get_doc("NS Envelope", env.name)

	def _validate_sendable(self, env):
		if env.status != EnvelopeStatus.DRAFT:
			frappe.throw(_("Only draft envelopes can be sent."))
		if not env.signers:
			frappe.throw(_("Add at least one signer before sending."))
		for signer in env.signers:
			if not signer.signer_email:
				frappe.throw(_("Every signer needs an email address."))

	# --------------------------------------------------------------- resend
	def resend(self, signer_email: str) -> "frappe.Document":
		env = self._doc()
		signer = env.get_signer(signer_email)
		if not signer:
			frappe.throw(_("Signer not found."))
		if signer.status == SignerStatus.SIGNED:
			frappe.throw(_("Signer has already signed."))
		NotificationService(env).send_invitation(signer)
		signer.db_set("sent_on", now_datetime())
		AuditService(env.name).log(AuditAction.REMINDED, signer_email=signer_email, details="Manual resend")
		return env

	# --------------------------------------------------------------- void
	def void(self, reason: str | None = None) -> "frappe.Document":
		env = self._doc()
		if env.status in EnvelopeStatus.TERMINAL:
			frappe.throw(_("Envelope is already in a final state."))
		env.status = EnvelopeStatus.VOIDED
		env.voided_on = now_datetime()
		env.void_reason = reason
		env.save()
		NotificationService(env).send_voided(reason)
		AuditService(env.name).log(AuditAction.VOIDED, details=reason)
		return env

	# --------------------------------------------------------------- finalize
	def finalize(self) -> "frappe.Document":
		"""Generate the immutable signed PDF + certificate and complete the envelope."""
		env = self._doc()
		source = files.read_file_content(env.source_pdf)

		fields = frappe.get_all(
			"NS Envelope Field",
			filters={"envelope": env.name},
			fields=["name", "field_key", "field_type", "label", "page", "pos_x",
				"pos_y", "width", "height", "value", "signature", "font_size", "font_family"],
			limit_page_length=0,
		)
		signature_images: dict[str, bytes] = {}
		for field in fields:
			if field.get("signature"):
				img_url = frappe.db.get_value("NS Signature", field["signature"], "signature_image")
				if img_url:
					signature_images[field["field_key"]] = files.read_file_content(img_url)

		filled = pdf_service.generate_filled_pdf(source, fields, signature_images)

		audit_rows = AuditService(env.name).list()
		chain = AuditService(env.name).verify()
		certificate = pdf_service.build_certificate(env, audit_rows, chain)

		# The signed copy is the flattened document only — the certificate of
		# completion is kept as a separate file (certificate_pdf), not appended.
		signed_file = files.save_private_file(
			f"{env.name}-signed.pdf", filled,
			attached_to_doctype="NS Envelope", attached_to_name=env.name,
			attached_to_field="signed_pdf",
		)
		cert_file = files.save_private_file(
			f"{env.name}-certificate.pdf", certificate,
			attached_to_doctype="NS Envelope", attached_to_name=env.name,
			attached_to_field="certificate_pdf",
		)

		env.signed_pdf = signed_file.file_url
		env.certificate_pdf = cert_file.file_url
		env.status = EnvelopeStatus.COMPLETED
		env.completed_on = now_datetime()
		env.recompute_progress()
		env.save()

		AuditService(env.name).log(AuditAction.GENERATED, details="Final signed PDF generated")
		AuditService(env.name).log(AuditAction.COMPLETED, details="All signers completed")
		NotificationService(env).send_completed()
		return frappe.get_doc("NS Envelope", env.name)

	# --------------------------------------------------------------- expiry
	def expire(self) -> "frappe.Document":
		env = self._doc()
		if env.status in EnvelopeStatus.TERMINAL:
			return env
		env.status = EnvelopeStatus.EXPIRED
		env.save()
		NotificationService(env).send_expired()
		AuditService(env.name).log(AuditAction.EXPIRED, details="Envelope expired")
		return env


# --------------------------------------------------------------------- helpers
def _resolve_placement(row, field: dict, anchor_positions: dict, page_count: int):
	"""Convert Last Page / Anchor placements into absolute page coordinates.

	Anchor fields snap their top-left corner to the marker's position; the
	designer's box size wins when set, otherwise the marker's own rect is used.
	A missing marker falls back to the designer coordinates (clamped below).
	"""
	placement = field.get("placement") or FieldPlacement.ABSOLUTE
	if placement == FieldPlacement.ANCHOR:
		anchor = anchor_positions.get(field.get("anchor_text") or "")
		if anchor:
			row.page = anchor["page"]
			row.pos_x = anchor["pos_x"]
			row.pos_y = anchor["pos_y"]
			if not row.width:
				row.width = anchor["width"]
			if not row.height:
				row.height = anchor["height"]
			return
	if placement == FieldPlacement.LAST_PAGE and page_count:
		row.page = page_count
	elif page_count and cint(row.page) > page_count:
		# Generated PDFs can be shorter than the designer preview was.
		row.page = page_count


def _default_org() -> str | None:
	return frappe.db.get_single_value("NS Settings", "default_organization")


def _resolve_source_doc(doctype: str | None, name: str | None):
	if not (doctype and name) or not frappe.db.exists(doctype, name):
		return None
	return frappe.get_doc(doctype, name)


def _read_mapped_value(source_doc, mapping_key: str | None):
	"""Read a (possibly dotted) field path from the reference document."""
	if not source_doc or not mapping_key:
		return None
	value = source_doc
	for part in str(mapping_key).split("."):
		if value is None:
			return None
		value = value.get(part) if hasattr(value, "get") else getattr(value, part, None)
	if value is None:
		return None
	return value if isinstance(value, str) else str(value)


def _expiry_days(env) -> int | None:
	if env.template:
		days = frappe.db.get_value("NS Template", env.template, "expiry_days")
		if days:
			return cint(days)
	if env.organization:
		days = frappe.db.get_value("NS Organization", env.organization, "default_expiry_days")
		if days:
			return cint(days)
	return 30
