"""HubSpot CRM connector: a real system of record behind the same tool boundary.

The connector is an adapter. Scope, policy, approval, dispatch, outcome and
reconciliation stay with the runner; nothing here decides whether a call is allowed.
"""

from __future__ import annotations

# HubSpot's date-versioned API. One place to change when a newer version is adopted.
API_VERSION = "2026-09"
# Token revocation is documented for this version; later ones are not confirmed.
REVOKE_VERSION = "2026-03"

AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
OAUTH_BASE = "https://api.hubspot.com"
API_BASE = "https://api.hubapi.com"

# Least privilege for the tools in `tools.py`, using HubSpot's granular scopes (the
# broad `tickets` scope is legacy). HubSpot has no notes-only scope: creating a note
# requires the contacts write scope.
DEFAULT_SCOPES = (
    "crm.objects.contacts.read",
    "crm.objects.companies.read",
    "crm.objects.tickets.read",
    "crm.objects.contacts.write",
)

# HubSpot-defined association type: note -> ticket.
NOTE_TO_TICKET = 228
