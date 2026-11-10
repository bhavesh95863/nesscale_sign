# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""Email notification service.

All mail is dispatched through the Frappe Email Queue (``frappe.sendmail``).
Bodies are rendered from editable ``Email Template`` records (seeded by
:mod:`nesscale_sign.email.seed`) using Frappe's own template helper, so wording
can be customised from Desk without code changes. Every send is recorded as an
``NS Notification`` row.
"""

import frappe
from frappe.utils import format_datetime, get_url, now_datetime

from nesscale_sign.email.seed import TEMPLATE_NAMES


def signing_url(token: str) -> str:
	base = frappe.db.get_single_value("NS Settings", "public_base_url") or get_url()
	return f"{base.rstrip('/')}/sign/{token}"


def app_url(path: str = "") -> str:
	base = get_url()
	return f"{base.rstrip('/')}/nesscale-sign/{path.lstrip('/')}"


class NotificationService:
	def __init__(self, envelope: "frappe.Document"):
		self.envelope = envelope
		# Branding is fixed for now; white-label (custom brand/logo) comes later.
		self.brand = "Nesscale Sign"

	# --------------------------------------------------------------- public
	def send_invitation(self, signer):
		ctx = self._signer_context(signer)
		rendered = self._render("Invitation", ctx)
		subject = self.envelope.email_subject or rendered["subject"]
		self._dispatch(signer.signer_email, subject, rendered["message"], "Invitation", signer.signer_email)

	def send_reminder(self, signer):
		ctx = self._signer_context(signer)
		rendered = self._render("Reminder", ctx)
		self._dispatch(signer.signer_email, rendered["subject"], rendered["message"], "Reminder", signer.signer_email)

	def send_completed(self):
		ctx = self._base_context()
		ctx["download_url"] = app_url(f"envelopes/{self.envelope.name}")
		rendered = self._render("Completed", ctx)
		for recipient in self._sender_and_signers():
			self._dispatch(recipient, rendered["subject"], rendered["message"], "Completed", recipient)

	def send_declined(self, declined_by: str | None = None, reason: str | None = None):
		ctx = self._base_context()
		ctx.update({"declined_by": declined_by, "reason": reason})
		rendered = self._render("Declined", ctx)
		for recipient in self._sender_and_signers():
			self._dispatch(recipient, rendered["subject"], rendered["message"], "Declined", recipient)

	def send_expired(self):
		rendered = self._render("Expired", self._base_context())
		for recipient in self._sender_and_signers():
			self._dispatch(recipient, rendered["subject"], rendered["message"], "Expired", recipient)

	def send_voided(self, reason: str | None = None):
		ctx = self._base_context()
		ctx["reason"] = reason
		rendered = self._render("Voided", ctx)
		for recipient in self._sender_and_signers():
			self._dispatch(recipient, rendered["subject"], rendered["message"], "Voided", recipient)

	# --------------------------------------------------------------- internals
	def _render(self, notification_type: str, context: dict) -> dict:
		"""Render an Email Template, falling back to a minimal body if missing."""
		template_name = TEMPLATE_NAMES.get(notification_type)
		try:
			# Not frappe's get_email_template(): it checks the current user can read Email
			# Template, and these mails go out in the signer's request - through the signing
			# link that is Guest, so the invitation to the next signer and the completion mail
			# would raise PermissionError and roll the signature back. The template is ours
			# (TEMPLATE_NAMES), never chosen by the user.
			template = frappe.get_doc("Email Template", template_name)
			return template.get_formatted_email(context)
		except frappe.DoesNotExistError:
			title = context.get("title") or self.envelope.title
			return {
				"subject": f"{notification_type}: {title}",
				"message": f"<p>{notification_type} — {frappe.utils.escape_html(title)}</p>",
			}

	def _dispatch(self, recipient, subject, message, notification_type, signer_email=None):
		notification = frappe.get_doc(
			{
				"doctype": "NS Notification",
				"envelope": self.envelope.name,
				"notification_type": notification_type,
				"recipient": recipient,
				"signer_email": signer_email,
				"subject": subject,
				"status": "Pending",
			}
		)
		notification.flags.ignore_permissions = True
		notification.insert(ignore_permissions=True)
		try:
			frappe.sendmail(
				recipients=[recipient],
				subject=subject,
				message=message,
				reference_doctype="NS Envelope",
				reference_name=self.envelope.name,
				now=False,
			)
			notification.db_set("status", "Sent")
			notification.db_set("sent_on", now_datetime())
		except Exception as exc:  # pragma: no cover - mail backend dependent
			notification.db_set("status", "Failed")
			notification.db_set("error", str(exc)[:140])
			frappe.log_error(
				title="Nesscale Sign: notification failed",
				message=f"Notification {notification.name} failed: {exc}\n\n{frappe.get_traceback()}",
			)

	def _signer_context(self, signer) -> dict:
		ctx = self._base_context()
		ctx.update(
			{
				"signer_name": signer.signer_name,
				"signer_email": signer.signer_email,
				"sign_url": signing_url(signer.token),
				"role": signer.role_label,
			}
		)
		return ctx

	def _base_context(self) -> dict:
		return {
			"brand": self.brand,
			"title": self.envelope.title,
			"sender_name": self.envelope.sender_name,
			"sender_email": self.envelope.sender_email,
			"message": self.envelope.message,
			"expires_on": format_datetime(self.envelope.expires_on) if self.envelope.expires_on else None,
		}

	def _sender_and_signers(self) -> list[str]:
		emails = {s.signer_email for s in self.envelope.signers if s.signer_email}
		if self.envelope.sender_email:
			emails.add(self.envelope.sender_email)
		return sorted(emails)
