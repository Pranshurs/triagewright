"""HTTP client for the few HubSpot CRM endpoints the connector uses.

Its main job is to say truthfully what a failed request means. A read that fails had
no effect. A write that fails is one of two things: definitely not applied (HubSpot
rejected it, or the request never left), or unknown (it was sent and no usable answer
came back). HubSpot documents no idempotency key for creating records, so an unknown
write is never sent again; it is looked for instead (`find_note`).
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from triagewright.hubspot import API_VERSION, NOTE_TO_TICKET
from triagewright.hubspot.oauth import AuthError, HubSpotAuth
from triagewright.tools.base import OutcomeUnknown, ToolError

# Failures that happen before any request bytes are sent.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Answers that mean HubSpot refused the request without processing it.
BACK_OFF = (423, 429, 477)
MAX_SCAN = 500   # associated notes examined by a lookup before it gives up
_SAFE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class HubSpotClient:
    def __init__(self, auth: HubSpotAuth, http: httpx.Client | None = None,
                 lookup_delay: float = 1.0, max_backoff: float = 5.0,
                 max_scan: int = MAX_SCAN,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.auth = auth
        self._http = http or httpx.Client(timeout=10.0)
        self._base = f"{auth.settings.api_base}/crm/objects/{API_VERSION}"
        self.lookup_delay = lookup_delay
        self.max_backoff = max_backoff
        self.max_scan = max_scan
        self._sleep = sleep
        self._clock = clock

    # -- reads -----------------------------------------------------------------------

    def get(self, object_type: str, object_id: str, properties: tuple[str, ...]) -> dict[str, Any]:
        body = self._send("GET", f"/{object_type}/{object_id}",
                          params={"properties": ",".join(properties)})
        props = body.get("properties")
        props = props if isinstance(props, dict) else {}
        return {"id": str(body.get("id", object_id)), **{p: props.get(p) for p in properties}}

    def associated(self, object_type: str, object_id: str, to_type: str,
                   cap: int = 100) -> tuple[list[str], bool]:
        """Ids of associated records, and whether the list is complete."""
        ids: list[str] = []
        after: str | None = None
        while True:
            params = {"limit": "100", **({"after": after} if after else {})}
            body = self._send("GET", f"/{object_type}/{object_id}/associations/{to_type}",
                              params=params)
            results = body.get("results")
            for r in results if isinstance(results, list) else []:
                if isinstance(r, dict) and r.get("toObjectId") is not None:
                    ids.append(str(r["toObjectId"]))
            nxt = body.get("paging", {}).get("next", {}) if isinstance(
                body.get("paging"), dict) else {}
            after = nxt.get("after") if isinstance(nxt, dict) else None
            if not after:
                return ids, True
            if len(ids) >= cap:
                return ids, False

    # -- the one write ---------------------------------------------------------------

    def create_note(self, ticket_id: str, body: str, marker: str) -> dict[str, Any]:
        payload = {
            "properties": {"hs_timestamp": str(int(self._clock() * 1000)),
                           "hs_note_body": render_note(body, marker)},
            "associations": [{"to": {"id": ticket_id}, "types": [
                {"associationCategory": "HUBSPOT_DEFINED",
                 "associationTypeId": NOTE_TO_TICKET}]}],
        }
        created = self._send("POST", "/notes", json=payload, write=True)
        if not created.get("id"):
            raise OutcomeUnknown("MALFORMED_RESPONSE", "created note has no id")
        return {"id": str(created["id"]), "ticket_id": ticket_id,
                "created_at": created.get("createdAt")}

    def find_note(self, ticket_id: str, marker: str) -> dict[str, Any]:
        """The one note on this ticket carrying `marker`. Read-only.

        Reads the ticket's associations directly rather than using search, whose
        index HubSpot says may lag behind recent writes. Absence is still not proof
        that the write did not happen, so anything but exactly one match is unknown.
        """
        if self.lookup_delay:
            self._sleep(self.lookup_delay)
        ids, complete = self.associated("tickets", ticket_id, "notes", cap=self.max_scan)
        matches: list[dict[str, Any]] = []
        for i in range(0, len(ids), 100):
            body = self._send("POST", "/notes/batch/read", json={
                "properties": ["hs_note_body"],
                "inputs": [{"id": n} for n in ids[i:i + 100]]})
            results = body.get("results")
            for n in results if isinstance(results, list) else []:
                props = n.get("properties") if isinstance(n, dict) else None
                if isinstance(props, dict) and marker in str(props.get("hs_note_body") or ""):
                    matches.append(n)
        if len(matches) > 1:
            raise OutcomeUnknown("AMBIGUOUS", f"{len(matches)} notes carry this write's marker")
        if not complete:
            raise OutcomeUnknown("LOOKUP_INCOMPLETE", "too many notes to examine them all")
        if not matches:
            raise OutcomeUnknown("NOT_VISIBLE", "no note carries this write's marker (yet)")
        return {"id": str(matches[0].get("id")), "ticket_id": ticket_id,
                "created_at": matches[0].get("createdAt")}

    # -- transport -------------------------------------------------------------------

    def _send(self, method: str, path: str, *, params: dict[str, str] | None = None,
              json: dict[str, Any] | None = None, write: bool = False) -> dict[str, Any]:
        refreshed = False
        while True:
            try:
                token = self.auth.access_token(force_refresh=refreshed)
            except AuthError as e:
                # Before the request: nothing was sent, whatever kind of call this is.
                raise ToolError("HUBSPOT_" + e.code, e.message, retryable=e.transient) from None
            try:
                r = self._http.request(method, self._base + path, params=params, json=json,
                                       headers={"Authorization": f"Bearer {token}"})
            except NEVER_SENT as e:
                raise ToolError("UPSTREAM_UNREACHABLE", type(e).__name__,
                                retryable=True) from None
            except httpx.HTTPError as e:
                if write:
                    raise OutcomeUnknown("RESPONSE_LOST", type(e).__name__) from None
                raise ToolError("UPSTREAM_NO_RESPONSE", type(e).__name__,
                                retryable=True) from None
            # 401 means the request was refused unprocessed, so one retry with a fresh
            # token cannot duplicate anything.
            if r.status_code == 401 and not refreshed:
                refreshed = True
                continue
            return self._interpret(r, write)

    def _interpret(self, r: httpx.Response, write: bool) -> dict[str, Any]:
        status = r.status_code
        try:
            body = r.json()
        except ValueError:
            body = None
        if 200 <= status < 300:
            if isinstance(body, dict):
                return body
            if write:  # accepted, but we cannot read what was created
                raise OutcomeUnknown("MALFORMED_RESPONSE", f"status {status}, unreadable body")
            raise ToolError("MALFORMED_RESPONSE", f"status {status}, unreadable body",
                            retryable=True)
        what = _describe(status, body)
        if status in BACK_OFF:
            if not write:
                self._sleep(min(_retry_after(r), self.max_backoff))
            raise ToolError("RATE_LIMITED", what, retryable=True)
        if status >= 500:
            if write:  # it may have been applied before the failure
                raise OutcomeUnknown("UPSTREAM_ERROR", what)
            raise ToolError("UPSTREAM_ERROR", what, retryable=True)
        code = {401: "HUBSPOT_UNAUTHORIZED", 403: "HUBSPOT_FORBIDDEN", 404: "NOT_FOUND"}
        raise ToolError(code.get(status, "HUBSPOT_REJECTED"), what)


def render_note(body: str, marker: str) -> str:
    return html.escape(body).replace("\n", "<br>") + f"<br><br>ref: {marker}"


def _describe(status: int, body: Any) -> str:
    """Status plus HubSpot's category and correlation id. Never the message text,
    which can quote record contents."""
    parts = [f"HubSpot answered {status}"]
    if isinstance(body, dict):
        for k in ("category", "correlationId"):
            v = body.get(k)
            if isinstance(v, str) and _SAFE.match(v):
                parts.append(f"{k}={v}")
    return " ".join(parts)


def _retry_after(r: httpx.Response) -> float:
    try:
        return max(0.0, float(r.headers.get("Retry-After", "1")))
    except ValueError:
        return 1.0
