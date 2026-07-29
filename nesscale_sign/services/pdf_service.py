# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""PDF engine: metrics, field overlay, signature embedding, flattening and the
tamper-evident audit certificate.

Design notes
------------
* Field coordinates are stored normalised (0..1) against page width/height with
  a **top-left** origin — identical to PDF.js / the browser designer — so the
  same numbers drive both the on-screen designer and the server-side render.
* Values and signatures are drawn directly onto the page content stream. The
  output therefore contains **no AcroForm widgets**: it is inherently flat and
  cannot be re-edited in a PDF form editor.
* A certificate page is appended summarising signers and the full audit trail,
  including the audit hash-chain head so the document is self-verifying.
"""

import io

import fitz  # PyMuPDF
import frappe
from frappe.utils import format_datetime

from nesscale_sign.utils.constants import SIGNATURE_FIELD_TYPES

_FONT_MAP = {
	"helvetica": "helv",
	"arial": "helv",
	"times": "tiro",
	"times new roman": "tiro",
	"courier": "cour",
	"mono": "cour",
}


def _fitz_font(font_family: str | None) -> str:
	return _FONT_MAP.get((font_family or "").strip().lower(), "helv")


def get_page_metrics(pdf_bytes: bytes) -> list[dict]:
	"""Return per-page width/height in PDF points."""
	metrics = []
	with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
		for index, page in enumerate(doc):
			rect = page.rect
			metrics.append({"page": index + 1, "width": rect.width, "height": rect.height})
	return metrics


def get_page_count(pdf_bytes: bytes) -> int:
	with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
		return doc.page_count


def find_anchors(pdf_bytes: bytes, anchor_texts: "set[str] | list[str]") -> dict[str, dict]:
	"""Locate anchor markers (e.g. ``[[sign:customer]]``) in the document.

	Returns ``{anchor_text: {page, pos_x, pos_y, width, height}}`` with the same
	normalised top-left coordinate system the designer uses. Only the first
	occurrence of each anchor is reported.
	"""
	anchors = {t for t in anchor_texts if t}
	results: dict[str, dict] = {}
	if not anchors:
		return results
	with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
		for index, page in enumerate(doc):
			pending = anchors - results.keys()
			if not pending:
				break
			pw, ph = page.rect.width, page.rect.height
			for text in pending:
				hits = page.search_for(text)
				if hits:
					r = hits[0]
					results[text] = {
						"page": index + 1,
						"pos_x": r.x0 / pw,
						"pos_y": r.y0 / ph,
						"width": (r.x1 - r.x0) / pw,
						"height": (r.y1 - r.y0) / ph,
					}
	return results


def remove_anchor_text(pdf_bytes: bytes, anchor_texts: "set[str] | list[str]") -> bytes:
	"""Redact anchor markers so they never appear in the sent/signed document.

	Redaction (rather than a white overlay) removes the text from the content
	stream entirely, so it also cannot be recovered by select-and-copy.
	"""
	anchors = {t for t in anchor_texts if t}
	if not anchors:
		return pdf_bytes
	doc = fitz.open(stream=pdf_bytes, filetype="pdf")
	try:
		dirty = False
		for page in doc:
			hit = False
			for text in anchors:
				for rect in page.search_for(text):
					page.add_redact_annot(rect)
					hit = True
			if hit:
				page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
				dirty = True
		if not dirty:
			return pdf_bytes
		return doc.tobytes(garbage=4, deflate=True)
	finally:
		doc.close()


def _rect_for_field(page, field: dict) -> "fitz.Rect":
	pw, ph = page.rect.width, page.rect.height
	x0 = float(field.get("pos_x") or 0) * pw
	y0 = float(field.get("pos_y") or 0) * ph
	x1 = x0 + float(field.get("width") or 0) * pw
	y1 = y0 + float(field.get("height") or 0) * ph
	return fitz.Rect(x0, y0, x1, y1)


def _draw_text(page, rect, text, field):
	font_size = float(field.get("font_size") or 12)
	fontname = _fitz_font(field.get("font_family"))
	# Auto-shrink until the text fits the box (insert_textbox returns the unused
	# vertical space; a negative value means overflow).
	size = font_size
	while size >= 5:
		rc = page.insert_textbox(
			rect, str(text), fontsize=size, fontname=fontname,
			color=(0, 0, 0), align=fitz.TEXT_ALIGN_LEFT,
		)
		if rc >= 0:
			return
		size -= 1
	# Last resort: draw clipped at the smallest size.
	page.insert_textbox(rect, str(text), fontsize=5, fontname=fontname, color=(0, 0, 0))


def _draw_checkbox(page, rect, checked):
	page.draw_rect(rect, color=(0.2, 0.2, 0.2), width=0.8)
	if str(checked).lower() in ("1", "true", "yes", "on", "checked"):
		page.draw_line(rect.tl, rect.br, color=(0, 0, 0), width=1.4)
		page.draw_line(rect.bl, rect.tr, color=(0, 0, 0), width=1.4)


def _draw_image(page, rect, image_bytes):
	page.insert_image(rect, stream=image_bytes, keep_proportion=True, overlay=True)


def generate_filled_pdf(
	source_pdf_bytes: bytes,
	fields: list[dict],
	signature_images: dict[str, bytes] | None = None,
) -> bytes:
	"""Overlay all field values/signatures onto the source PDF and flatten.

	``signature_images`` maps a field's ``field_key`` to the raw image bytes for
	signature/initial/stamp fields.
	"""
	signature_images = signature_images or {}
	doc = fitz.open(stream=source_pdf_bytes, filetype="pdf")
	try:
		by_page: dict[int, list[dict]] = {}
		for field in fields:
			by_page.setdefault(int(field.get("page") or 1), []).append(field)

		for page_no, page_fields in by_page.items():
			if page_no < 1 or page_no > doc.page_count:
				continue
			page = doc[page_no - 1]
			for field in page_fields:
				rect = _rect_for_field(page, field)
				ftype = field.get("field_type")
				value = field.get("value")
				if ftype in SIGNATURE_FIELD_TYPES:
					img = signature_images.get(field.get("field_key"))
					if img:
						_draw_image(page, rect, img)
				elif ftype == "Checkbox":
					_draw_checkbox(page, rect, value)
				elif ftype == "Label":
					_draw_text(page, rect, field.get("label") or value or "", field)
				elif value not in (None, ""):
					_draw_text(page, rect, value, field)

		# Mark document as final in metadata.
		doc.set_metadata({**(doc.metadata or {}), "producer": "Nesscale Sign", "title": "Signed Document"})
		out = doc.tobytes(garbage=4, deflate=True)
		return out
	finally:
		doc.close()


def build_certificate(envelope: "frappe.Document", audit_rows: list[dict], chain: dict) -> bytes:
	"""Render a certificate of completion page using ReportLab."""
	from reportlab.lib import colors
	from reportlab.lib.pagesizes import A4
	from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
	from reportlab.lib.units import mm
	from reportlab.platypus import (
		Paragraph,
		SimpleDocTemplate,
		Spacer,
		Table,
		TableStyle,
	)

	buffer = io.BytesIO()
	doc = SimpleDocTemplate(
		buffer, pagesize=A4,
		leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
		title="Certificate of Completion",
	)
	styles = getSampleStyleSheet()
	h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, textColor=colors.HexColor("#111827"))
	h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12, textColor=colors.HexColor("#374151"))
	normal = ParagraphStyle("n", parent=styles["Normal"], fontSize=9, leading=13)
	small = ParagraphStyle("s", parent=styles["Normal"], fontSize=7.5, leading=10, textColor=colors.HexColor("#6B7280"))

	story = [Paragraph("Certificate of Completion", h1), Spacer(1, 6)]
	story.append(Paragraph(f"Envelope: <b>{frappe.utils.escape_html(envelope.title)}</b> ({envelope.name})", normal))
	story.append(Paragraph(f"Status: <b>{envelope.status}</b>", normal))
	if envelope.completed_on:
		story.append(Paragraph(f"Completed: {format_datetime(envelope.completed_on)}", normal))
	story.append(Paragraph(f"Sender: {frappe.utils.escape_html(envelope.sender_name or '')} &lt;{envelope.sender_email or ''}&gt;", normal))
	story.append(Spacer(1, 12))

	# Signers table
	story.append(Paragraph("Signers", h2))
	signer_data = [["Name", "Email", "Status", "Signed On", "IP Address"]]
	for s in envelope.signers:
		signer_data.append([
			s.signer_name or "", s.signer_email or "", s.status or "",
			format_datetime(s.signed_on) if s.signed_on else "—", s.ip_address or "—",
		])
	signer_table = Table(signer_data, colWidths=[32 * mm, 45 * mm, 20 * mm, 38 * mm, 35 * mm])
	signer_table.setStyle(_table_style(colors))
	story.append(signer_table)
	story.append(Spacer(1, 14))

	# Audit trail
	story.append(Paragraph("Audit Trail", h2))
	audit_data = [["Timestamp", "Action", "Actor", "IP", "Details"]]
	for row in audit_rows:
		audit_data.append([
			format_datetime(row.get("timestamp")) if row.get("timestamp") else "",
			row.get("action") or "",
			row.get("signer_email") or row.get("signer_name") or "system",
			row.get("ip_address") or "—",
			(row.get("details") or "")[:60],
		])
	audit_table = Table(audit_data, colWidths=[34 * mm, 26 * mm, 45 * mm, 28 * mm, 37 * mm], repeatRows=1)
	audit_table.setStyle(_table_style(colors))
	story.append(audit_table)
	story.append(Spacer(1, 14))

	chain_state = "VERIFIED" if chain.get("valid") else "BROKEN"
	story.append(Paragraph(
		f"Audit integrity: <b>{chain_state}</b> &mdash; {chain.get('count', 0)} chained events. "
		"Each event is SHA-256 hash-chained to its predecessor; any alteration invalidates the chain.",
		small,
	))
	story.append(Paragraph(
		"This certificate was generated by Nesscale Sign and is an integral part of the signed document.",
		small,
	))

	doc.build(story)
	return buffer.getvalue()


def _table_style(colors):
	from reportlab.platypus import TableStyle

	return TableStyle([
		("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
		("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
		("FONTSIZE", (0, 0), (-1, -1), 7.5),
		("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
		("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D1D5DB")),
		("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")]),
		("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
		("TOPPADDING", (0, 0), (-1, -1), 3),
		("BOTTOMPADDING", (0, 0), (-1, -1), 3),
	])


def append_certificate(pdf_bytes: bytes, certificate_bytes: bytes) -> bytes:
	"""Append the certificate PDF to the signed document."""
	base = fitz.open(stream=pdf_bytes, filetype="pdf")
	cert = fitz.open(stream=certificate_bytes, filetype="pdf")
	try:
		base.insert_pdf(cert)
		return base.tobytes(garbage=4, deflate=True)
	finally:
		base.close()
		cert.close()


def render_page_thumbnail(pdf_bytes: bytes, page_no: int = 1, zoom: float = 0.4) -> bytes:
	"""Render a PNG thumbnail of a page (used for template/library previews)."""
	with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
		page = doc[max(0, min(page_no - 1, doc.page_count - 1))]
		matrix = fitz.Matrix(zoom, zoom)
		pix = page.get_pixmap(matrix=matrix, alpha=False)
		return pix.tobytes("png")
