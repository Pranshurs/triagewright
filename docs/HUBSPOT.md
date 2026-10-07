# HubSpot connector

Triagewright's tools are simulated by default. This connector puts one real system of
record, HubSpot CRM, behind the same tool boundary, reached through OAuth. It is an
adapter: scope, policy, approval, dispatch, outcome and reconciliation stay with the
runner, and there is no connector-specific safety path.

It is optional. Without `TRIAGEWRIGHT_HUBSPOT_CLIENT_ID` and
`TRIAGEWRIGHT_HUBSPOT_CLIENT_SECRET` the catalogue, API and behaviour are unchanged.

## What it talks to

HubSpot's date-versioned API, version `2026-09` (one constant in
`src/triagewright/hubspot/__init__.py`).

| Purpose | Request |
|---------|---------|
| Consent | `GET https://app.hubspot.com/oauth/authorize` |
| Code exchange, refresh | `POST https://api.hubspot.com/oauth/2026-09/token` (form body) |
| Revoke on disconnect | `POST https://api.hubspot.com/oauth/2026-03/token/revoke` |
| Read a record | `GET /crm/objects/2026-09/{tickets,contacts,companies}/{id}` |
| List associations | `GET /crm/objects/2026-09/{type}/{id}/associations/{toType}` |
| Create a note | `POST /crm/objects/2026-09/notes` (associated to the ticket, type 228) |
| Read notes | `POST /crm/objects/2026-09/notes/batch/read` |

Revocation is documented for `2026-03`; a `2026-09` path was not confirmed, so the
older one is used. Disconnect forgets the local tokens whether or not HubSpot confirms.

Scopes requested: `oauth` (HubSpot's base scope for OAuth apps),
`crm.objects.contacts.read`, `crm.objects.companies.read`,
`crm.objects.tickets.read`, `crm.objects.contacts.write`. HubSpot has no notes-only
scope; creating a note needs the contacts write scope. An app that still uses the
legacy `tickets` scope can set `TRIAGEWRIGHT_HUBSPOT_SCOPES`.

## Tools

| Tool | Class | Scope comes from |
|------|-------|------------------|
| `hubspot_get_ticket` | read | the ticket's companies |
| `hubspot_get_contact` | read | the contact's companies |
| `hubspot_get_company` | read | the company |
| `hubspot_add_ticket_note` | approval required | the ticket's companies |

Tenant scope is resolved by the runner from HubSpot, not from anything the agent says.
An operator declares which HubSpot company is which account
(`triagewright hubspot link <account_id> <company_id>`). A record belongs to an account
only if all of its companies are linked to that one account. Anything else, including a
record that does not exist or a HubSpot outage, is denied with the same scope message
the simulated tools use.

## OAuth

- Authorization-code flow. `GET /api/hubspot/connect` redirects to HubSpot;
  `GET /api/hubspot/callback` completes it.
- `state` is 256 random bits, kept server-side, single-use, and expires after ten
  minutes. It is checked before anything else; a callback with a bad state never
  reaches the token endpoint.
- The grant must include every required scope or nothing is stored.
- Access tokens (30 minutes) are refreshed a minute before expiry. A 401 from the API
  triggers one refresh and one retry.
- `invalid_grant` on refresh means the grant was revoked: tokens are dropped, the
  connection reports `needs_reauthorization`, and tools fail closed until an operator
  connects again.
- `POST /api/hubspot/disconnect` or `triagewright hubspot disconnect` revokes and
  forgets the tokens. Account links survive.

## Where secrets live

Client id and secret are read from the environment and never written. Tokens live in
one file, `~/.config/triagewright/hubspot.json` by default
(`TRIAGEWRIGHT_HUBSPOT_STORE`), created `0600` in a `0700` directory, outside the runs
directory. They do not enter case state, the trace, the case record, scores, telemetry
or logs. Errors from HubSpot are reduced to status, category and correlation id; the
message text is dropped because it can quote tokens and record contents. A test plants
recognisable secrets and searches every output surface for them.

## A write whose answer is lost

HubSpot documents no idempotency key for creating a record, so a note cannot be made
safe to repeat. The connector therefore never repeats one.

1. The runner mints its idempotency key and persists it before dispatch, as for every
   write.
2. The note body ends with a marker: an HMAC of that key under a secret held in the
   connection store. The key itself never leaves, and the marker cannot be derived
   from anything the agent can see.
3. The client classifies each failure:

   | What happened | Outcome |
   |---------------|---------|
   | 2xx with a note id | succeeded |
   | 4xx, 423, 429, 477, or the connection could not be opened | failed (no effect) |
   | Sent, then timeout, dropped connection, 5xx, or an unreadable 2xx | **unknown** |

4. Reconciling an unknown note is a lookup only. It lists the ticket's associated
   notes (a direct read; the search index may lag behind recent writes) and looks for
   the marker.

   | Lookup finds | Result |
   |--------------|--------|
   | exactly one note | succeeded, settled as the original effect |
   | more than one | stays unknown |
   | none | stays unknown |
   | lookup failed or could not examine every note | stays unknown |

"None" stays unknown because HubSpot documents no read-after-write guarantee, so
absence is not proof. The case then ends `needs_attention`, an identical write is
refused while the first is unknown, and an operator can look again later with
`triagewright recheck <case> <action> --operator <name>` (or
`POST /api/cases/{id}/actions/{action_id}/recheck`). A recheck is the same lookup; it
cannot create anything. The agent has no access to it.

Simulated tools are unchanged: their upstream keeps an idempotency store, so they
still reconcile by replaying the same key.

## Tests

`tests/test_hubspot.py` runs the connector through `CaseService` and the runner against
`tests/fake_hubspot.py`, a local HTTP server that implements only the paths above. No
network or account is needed. The fake can answer with a status, return an unreadable
body, or drop the connection before or after applying a write.

## Live check against a developer test account

Not part of the automated suite. Needs a HubSpot developer account, a test account
with one ticket associated to a company and a contact, and an app with the scopes
above and redirect URL `http://localhost:8000/api/hubspot/callback`.

```bash
export TRIAGEWRIGHT_HUBSPOT_CLIENT_ID=...       # from the app's Auth settings
export TRIAGEWRIGHT_HUBSPOT_CLIENT_SECRET=...
triagewright serve
```

1. Open `http://localhost:8000/api/hubspot/connect` and approve the test account.
2. `triagewright hubspot status` shows `connected: true` and the granted scopes.
3. `triagewright hubspot link acc_halvard <companyId>`.
4. `triagewright hubspot open <ticketId>` prints a case id. Run it and approve the
   note in the console. The note appears on the ticket in HubSpot.
5. `triagewright hubspot disconnect`, then delete the note and uninstall the app from
   the test account.

## Limits

- One connected HubSpot account per installation; one write tool.
- A transient HubSpot failure while resolving scope reads as a scope denial.
- A lookup examines at most 500 notes on a ticket; beyond that it does not conclude.
- The API has no authentication of its own (see `INTEGRATION.md`): the connect and
  disconnect routes are for a local operator, not for exposure.
- HubSpot case runs are not part of `triagewright eval`; there is no simulated ground
  truth to score them against.
