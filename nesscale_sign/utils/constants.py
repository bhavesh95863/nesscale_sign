# Copyright (c) 2026, Nesscale Solutions Pvt Ltd and contributors
# For license information, please see license.txt
"""Shared enums and constants for Nesscale Sign."""


class EnvelopeStatus:
	DRAFT = "Draft"
	SENT = "Sent"
	IN_PROGRESS = "In Progress"
	COMPLETED = "Completed"
	DECLINED = "Declined"
	VOIDED = "Voided"
	EXPIRED = "Expired"

	TERMINAL = frozenset({COMPLETED, DECLINED, VOIDED, EXPIRED})
	ACTIVE = frozenset({SENT, IN_PROGRESS})


class SignerStatus:
	PENDING = "Pending"
	SENT = "Sent"
	VIEWED = "Viewed"
	SIGNED = "Signed"
	DECLINED = "Declined"


class AuditAction:
	CREATED = "Created"
	SENT = "Sent"
	VIEWED = "Viewed"
	OPENED = "Opened"
	SIGNED = "Signed"
	DECLINED = "Declined"
	COMPLETED = "Completed"
	DOWNLOADED = "Downloaded"
	VOIDED = "Voided"
	EXPIRED = "Expired"
	REMINDED = "Reminded"
	FIELD_FILLED = "Field Filled"
	GENERATED = "Generated"


class RoutingType:
	SEQUENTIAL = "Sequential"
	PARALLEL = "Parallel"


class PdfSource:
	STATIC = "Static PDF"
	PRINT_FORMAT = "Print Format"


class FieldPlacement:
	ABSOLUTE = "Absolute"
	LAST_PAGE = "Last Page"
	ANCHOR = "Anchor"


# Field types that require a captured signature artifact rather than text input.
SIGNATURE_FIELD_TYPES = {"Signature", "Initial", "Stamp"}

# Field types that the system fills automatically at signing time.
AUTO_FIELD_TYPES = {"Date Signed"}

# Field types that simply render static content and need no signer input.
STATIC_FIELD_TYPES = {"Label"}

DEFAULT_SIGNER_PALETTE = [
	"#2563EB", "#DC2626", "#059669", "#D97706", "#7C3AED",
	"#DB2777", "#0891B2", "#65A30D", "#EA580C", "#4F46E5",
]
