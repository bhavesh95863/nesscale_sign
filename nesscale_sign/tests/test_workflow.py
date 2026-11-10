# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""Workflow & signing tests: sequential/parallel routing, completion, decline."""

import frappe
from frappe.tests.utils import FrappeTestCase

from nesscale_sign.services.signing_service import SigningService
from nesscale_sign.services.workflow_service import WorkflowService
from nesscale_sign.tests.utils import make_envelope, make_template, signature_payload

TWO_ROLES = [
	{"role_label": "First", "role_key": "first", "signing_order": 1},
	{"role_label": "Second", "role_key": "second", "signing_order": 2},
]

TWO_FIELDS = [
	{"field_type": "Signature", "label": "Sign 1", "signer_role": "first",
	 "page": 1, "pos_x": 0.1, "pos_y": 0.3, "width": 0.2, "height": 0.06, "required": 1},
	{"field_type": "Signature", "label": "Sign 2", "signer_role": "second",
	 "page": 1, "pos_x": 0.1, "pos_y": 0.5, "width": 0.2, "height": 0.06, "required": 1},
]


def two_signers():
	return [
		{"signer_name": "Alice", "signer_email": "alice@test.com", "role_key": "first", "signing_order": 1},
		{"signer_name": "Bob", "signer_email": "bob@test.com", "role_key": "second", "signing_order": 2},
	]


class TestSequentialWorkflow(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.tmpl = make_template("Seq Tmpl", roles=TWO_ROLES, fields=TWO_FIELDS)

	def test_only_first_signer_notified_on_send(self):
		env = make_envelope(self.tmpl.name, two_signers(), routing="Sequential")
		statuses = {s.role_key: s.status for s in env.signers}
		self.assertEqual(statuses["first"], "Sent")
		self.assertEqual(statuses["second"], "Pending")

	def test_second_signer_cannot_sign_before_first(self):
		env = make_envelope(self.tmpl.name, two_signers(), routing="Sequential")
		second_token = env.get_signer("bob@test.com").token
		with self.assertRaises(frappe.ValidationError):
			SigningService(second_token).submit({}, signature_payload())

	def test_sequential_advances_and_completes(self):
		env = make_envelope(self.tmpl.name, two_signers(), routing="Sequential")
		first_token = env.get_signer("alice@test.com").token

		SigningService(first_token).submit({}, signature_payload())
		env.reload()
		self.assertEqual(env.get_signer("alice@test.com").status, "Signed")
		# Second signer should now be activated.
		self.assertEqual(env.get_signer("bob@test.com").status, "Sent")
		self.assertEqual(env.status, "In Progress")

		second_token = env.get_signer("bob@test.com").token
		result = SigningService(second_token).submit({}, signature_payload())
		self.assertEqual(result["status"], "completed")
		env.reload()
		self.assertEqual(env.status, "Completed")
		self.assertTrue(env.signed_pdf)
		self.assertEqual(env.progress, 100)


class TestSigningLinkAsGuest(FrappeTestCase):
	"""Signers on the public signing link are Guest: signing, the next signer's invitation,
	finalizing and the completion mail must all work without desk permissions."""

	def setUp(self):
		frappe.set_user("Administrator")
		self.tmpl = make_template("Guest Seq Tmpl", roles=TWO_ROLES, fields=TWO_FIELDS)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_guest_signers_complete_a_sequential_envelope(self):
		env = make_envelope(self.tmpl.name, two_signers(), routing="Sequential")
		first_token = env.get_signer("alice@test.com").token
		second_token = env.get_signer("bob@test.com").token

		frappe.set_user("Guest")
		result = SigningService(first_token).submit({}, signature_payload())
		self.assertEqual(result["status"], "signed")
		result = SigningService(second_token).submit({}, signature_payload())
		self.assertEqual(result["status"], "completed")

		frappe.set_user("Administrator")
		env.reload()
		self.assertEqual(env.status, "Completed")
		self.assertTrue(env.signed_pdf)
		self.assertTrue(env.certificate_pdf)
		sent = frappe.get_all(
			"NS Notification", filters={"envelope": env.name}, pluck="notification_type"
		)
		self.assertIn("Invitation", sent)
		self.assertIn("Completed", sent)


class TestParallelWorkflow(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.tmpl = make_template("Par Tmpl", roles=TWO_ROLES, fields=TWO_FIELDS)

	def test_all_signers_active_on_send(self):
		env = make_envelope(self.tmpl.name, two_signers(), routing="Parallel")
		for s in env.signers:
			self.assertEqual(s.status, "Sent")
		wf = WorkflowService(env)
		self.assertEqual(len(wf.active_signers()), 2)

	def test_parallel_completes_after_both(self):
		env = make_envelope(self.tmpl.name, two_signers(), routing="Parallel")
		SigningService(env.get_signer("bob@test.com").token).submit({}, signature_payload())
		env.reload()
		# Still not complete; first signer outstanding.
		self.assertIn(env.status, ("In Progress", "Sent"))
		result = SigningService(env.get_signer("alice@test.com").token).submit({}, signature_payload())
		self.assertEqual(result["status"], "completed")
		env.reload()
		self.assertEqual(env.status, "Completed")


class TestDeclineAndVoid(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.tmpl = make_template("Decline Tmpl", roles=TWO_ROLES, fields=TWO_FIELDS)

	def test_decline_sets_envelope_declined(self):
		env = make_envelope(self.tmpl.name, two_signers(), routing="Sequential")
		token = env.get_signer("alice@test.com").token
		result = SigningService(token).decline("Not interested")
		self.assertEqual(result["status"], "declined")
		env.reload()
		self.assertEqual(env.status, "Declined")
		self.assertEqual(env.get_signer("alice@test.com").status, "Declined")

	def test_void_blocks_signing(self):
		from nesscale_sign.services.envelope_service import EnvelopeService

		env = make_envelope(self.tmpl.name, two_signers(), routing="Parallel")
		EnvelopeService(env.name).void("Cancelled")
		env.reload()
		self.assertEqual(env.status, "Voided")
		with self.assertRaises(Exception):
			SigningService(env.get_signer("alice@test.com").token).submit({}, signature_payload())
