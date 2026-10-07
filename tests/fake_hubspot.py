"""A local stand-in for the few HubSpot endpoints the connector calls.

It implements the documented request and response shapes for those paths only, keeps
a journal of the notes it really created, and can be told to misbehave on the next
matching request: answer with a status, return an unreadable body, or drop the
connection before or after applying a write.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

CLIENT_ID = "app-client-id"
CLIENT_SECRET = "shh-client-secret-do-not-leak"
V = "2026-09"
CRM = f"/crm/objects/{V}/"


class FakeHubSpot:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.codes: dict[str, tuple[str, list[str]]] = {}   # code -> (redirect_uri, scopes)
        self.refresh_tokens: dict[str, list[str]] = {}      # token -> scopes
        self.access_tokens: dict[str, list[str]] = {}
        self.grant_scopes: list[str] | None = None          # override what a consent grants
        self.deny = False
        self.hub_id: Any = 4242
        self.stuck_paging = False                           # never-ending association pages
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            "tickets": {}, "contacts": {}, "companies": {}, "notes": {}}
        self.assoc: dict[tuple[str, str, str], list[str]] = {}
        self.journal: list[dict[str, Any]] = []             # notes actually created
        self.requests: list[tuple[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.faults: list[tuple[str, str, str]] = []        # (method, path fragment, kind)
        self._n = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- test controls ---------------------------------------------------------------

    def fail(self, method: str, fragment: str, *kinds: str) -> None:
        """Queue faults for the next requests matching method and path fragment.
        Kinds: "status:503", "status_after:500", "malformed", "drop_before", "drop_after"."""
        self.faults += [(method, fragment, k) for k in kinds]

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    def revoke_grant(self) -> None:
        self.refresh_tokens.clear()
        self.access_tokens.clear()

    def add(self, kind: str, oid: str, **props: Any) -> None:
        self.objects[kind][oid] = dict(props)

    def associate(self, a: str, aid: str, b: str, bid: str) -> None:
        self.assoc.setdefault((a, aid, b), []).append(bid)
        self.assoc.setdefault((b, bid, a), []).append(aid)

    def posts(self, fragment: str) -> int:
        return sum(1 for m, p in self.requests if m == "POST" and fragment in p)

    # -- plumbing --------------------------------------------------------------------

    def _mint(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}-{uuid.uuid4().hex}"

    def _fault(self, method: str, path: str) -> str | None:
        for i, (m, frag, kind) in enumerate(self.faults):
            if m == method and frag in path:
                del self.faults[i]
                return kind
        return None

    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        url = urlsplit(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        with self.lock:
            self.requests.append((method, url.path))
            fault = self._fault(method, url.path)
            if fault == "drop_before":
                return _drop(h)
            if fault and fault.startswith("status:"):
                return _send(h, int(fault.split(":")[1]), _error("RATE_LIMITS"),
                             {"Retry-After": "7"})
            status, body, headers = self._route(h, method, url.path, parse_qs(url.query), raw)
            if fault == "drop_after":
                return _drop(h)
            if fault == "malformed":
                return _send_raw(h, status, b"<html>gateway says hello</html>")
            if fault and fault.startswith("status_after:"):
                return _send(h, int(fault.split(":")[1]), _error("INTERNAL"))
            _send(h, status, body, headers)

    def _route(self, h: BaseHTTPRequestHandler, method: str, path: str,
               q: dict[str, list[str]], raw: bytes) -> tuple[int, Any, dict[str, str]]:
        if path == "/oauth/authorize":
            return self._authorize(q)
        if path == f"/oauth/{V}/token" and method == "POST":
            return self._token({k: v[0] for k, v in parse_qs(raw.decode()).items()})
        if path == "/oauth/2026-03/token/revoke" and method == "POST":
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            self.refresh_tokens.pop(form.get("token", ""), None)
            self.access_tokens.clear()
            return 200, {}, {}
        if not path.startswith(CRM):
            return 404, _error("NOT_FOUND"), {}
        token = (h.headers.get("Authorization") or "").removeprefix("Bearer ")
        scopes = self.access_tokens.get(token)
        if scopes is None:
            return 401, _error("EXPIRED_AUTHENTICATION",
                               f"the OAuth token {token} used is expired"), {}
        parts = path[len(CRM):].split("/")
        if method == "POST" and parts == ["notes"]:
            if "crm.objects.contacts.write" not in scopes:
                return 403, _error("MISSING_SCOPES"), {}
            return self._create_note(json.loads(raw))
        if method == "POST" and parts == ["notes", "batch", "read"]:
            ids = [i["id"] for i in json.loads(raw)["inputs"]]
            return 200, {"status": "COMPLETE", "results": [
                self._record("notes", i) for i in ids if i in self.objects["notes"]]}, {}
        if method == "GET" and len(parts) == 2 and parts[0] in self.objects:
            if parts[1] not in self.objects[parts[0]]:
                return 404, _error("OBJECT_NOT_FOUND"), {}
            return 200, self._record(parts[0], parts[1], q.get("properties", [""])[0]), {}
        if method == "GET" and len(parts) == 4 and parts[2] == "associations":
            ids = self.assoc.get((parts[0], parts[1], parts[3]), [])
            start, limit = int(q.get("after", ["0"])[0]), int(q.get("limit", ["100"])[0])
            body: dict[str, Any] = {"results": [
                {"toObjectId": int(i), "associationTypes": [
                    {"category": "HUBSPOT_DEFINED", "typeId": 1, "label": None}]}
                for i in ids[start:start + limit]]}
            if start + limit < len(ids):
                body["paging"] = {"next": {"after": str(start + limit)}}
            if self.stuck_paging:
                body = {"results": [], "paging": {"next": {"after": str(start + 1)}}}
            return 200, body, {}
        return 404, _error("NOT_FOUND"), {}

    def _record(self, kind: str, oid: str, wanted: str = "") -> dict[str, Any]:
        props = self.objects[kind][oid]
        if wanted:
            props = {k: props.get(k) for k in wanted.split(",") if k in props}
        return {"id": oid, "properties": props, "createdAt": "2026-10-07T10:00:00.000Z",
                "updatedAt": "2026-10-07T10:00:00.000Z", "archived": False}

    def _create_note(self, body: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
        self._n += 1
        nid = str(9000 + self._n)
        self.objects["notes"][nid] = dict(body["properties"])
        for a in body.get("associations", []):
            assert a["types"][0]["associationTypeId"] == 228, "note -> ticket"
            self.associate("tickets", str(a["to"]["id"]), "notes", nid)
        self.journal.append({"id": nid, **body})
        return 201, self._record("notes", nid), {}

    def _authorize(self, q: dict[str, list[str]]) -> tuple[int, Any, dict[str, str]]:
        redirect, state = q["redirect_uri"][0], q["state"][0]
        if q["client_id"][0] != CLIENT_ID:
            return 400, {"error": "invalid_client"}, {}
        if self.deny:
            return 302, {}, {"Location": redirect + "?" + urlencode(
                {"error": "access_denied", "state": state})}
        code = self._mint("code")
        scopes = self.grant_scopes if self.grant_scopes is not None else q["scope"][0].split()
        self.codes[code] = (redirect, scopes)
        return 302, {}, {"Location": redirect + "?" + urlencode({"code": code, "state": state})}

    def _token(self, form: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        self.token_requests.append(form)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return 401, {"error": "invalid_client", "error_description": "bad client"}, {}
        bad = (400, {"error": "invalid_grant", "status": "BAD_REFRESH_TOKEN",
                     "error_description": "refresh token is invalid, expired or revoked"}, {})
        if form.get("grant_type") == "authorization_code":
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None or grant[0] != form.get("redirect_uri"):
                return bad
            scopes, refresh = grant[1], self._mint("hs-refresh")
            self.refresh_tokens[refresh] = scopes
        elif form.get("grant_type") == "refresh_token":
            refresh = form.get("refresh_token", "")
            if refresh not in self.refresh_tokens:
                return bad
            scopes = self.refresh_tokens[refresh]
        else:
            return 400, {"error": "unsupported_grant_type"}, {}
        access = self._mint("hs-access")
        self.access_tokens[access] = scopes
        return 200, {"access_token": access, "refresh_token": refresh, "expires_in": 1800,
                     "token_type": "bearer", "hub_id": self.hub_id, "scopes": scopes}, {}


def _error(category: str, message: str = "error") -> dict[str, Any]:
    return {"status": "error", "message": message, "category": category,
            "correlationId": str(uuid.uuid4())}


def _send(h: BaseHTTPRequestHandler, status: int, body: Any,
          headers: dict[str, str] | None = None) -> None:
    _send_raw(h, status, json.dumps(body).encode(), headers)


def _send_raw(h: BaseHTTPRequestHandler, status: int, data: bytes,
              headers: dict[str, str] | None = None) -> None:
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(data)


def _drop(h: BaseHTTPRequestHandler) -> None:
    """Close the connection without answering, as a lost response looks on the wire."""
    h.close_connection = True
    with contextlib.suppress(OSError):
        h.connection.shutdown(socket.SHUT_RDWR)
