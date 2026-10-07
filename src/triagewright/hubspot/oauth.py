"""OAuth 2.0 authorization-code flow and token custody for the HubSpot connector.

Client credentials come from the environment and are never written anywhere. Tokens
live in one owner-only file outside the case directories; they never enter case
state, the trace, the case record, evaluation output or telemetry, and no error
raised here carries a response body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from triagewright.hubspot import (
    API_BASE,
    API_VERSION,
    AUTHORIZE_URL,
    DEFAULT_SCOPES,
    OAUTH_BASE,
    REVOKE_VERSION,
)

STATE_TTL = 600.0      # seconds an authorization may stay in flight
MAX_PENDING = 16
EXPIRY_SKEW = 60.0     # refresh this long before the access token expires
_ERROR_CODE = re.compile(r"^[a-z_]{1,40}$")


class AuthError(Exception):
    def __init__(self, code: str, message: str, transient: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.transient = transient


class NotConnected(AuthError):
    """No usable grant: never connected, disconnected, or the grant was revoked."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str = field(repr=False)
    store: Path
    redirect_uri: str = "http://localhost:8000/api/hubspot/callback"
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    authorize_url: str = AUTHORIZE_URL
    oauth_base: str = OAUTH_BASE
    api_base: str = API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings | None:
        """Settings from TRIAGEWRIGHT_HUBSPOT_* variables, or None if not configured."""
        e = os.environ if env is None else env
        cid, secret = e.get("TRIAGEWRIGHT_HUBSPOT_CLIENT_ID"), \
            e.get("TRIAGEWRIGHT_HUBSPOT_CLIENT_SECRET")
        if not cid or not secret:
            return None
        base = Path(e.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        kw: dict[str, Any] = {}
        for name, var in (("redirect_uri", "REDIRECT_URI"), ("authorize_url", "AUTHORIZE_URL"),
                          ("oauth_base", "OAUTH_BASE"), ("api_base", "API_BASE")):
            if e.get(f"TRIAGEWRIGHT_HUBSPOT_{var}"):
                kw[name] = e[f"TRIAGEWRIGHT_HUBSPOT_{var}"]
        if e.get("TRIAGEWRIGHT_HUBSPOT_SCOPES"):
            kw["scopes"] = tuple(e["TRIAGEWRIGHT_HUBSPOT_SCOPES"].split())
        return cls(client_id=cid, client_secret=secret,
                   store=Path(e.get("TRIAGEWRIGHT_HUBSPOT_STORE")
                              or base / "triagewright" / "hubspot.json"), **kw)


@dataclass
class _Stored:
    marker_secret: str = field(repr=False)
    hub_id: int | None = None
    scopes: list[str] = field(default_factory=list)
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float = 0.0
    needs_reauthorization: bool = False
    links: dict[str, str] = field(default_factory=dict)  # HubSpot company id -> account id


class HubSpotAuth:
    def __init__(self, settings: Settings, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._http = http or httpx.Client(timeout=10.0)
        self._clock = clock
        self._pending: dict[str, float] = {}
        self._lock = threading.RLock()

    # -- authorization ---------------------------------------------------------------

    def begin(self) -> str:
        """Start an authorization: returns the URL to send the account owner to."""
        s = self.settings
        with self._lock:
            now = self._clock()
            self._pending = {k: v for k, v in self._pending.items() if v > now}
            while len(self._pending) >= MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            state = secrets.token_urlsafe(32)
            self._pending[state] = now + STATE_TTL
        return s.authorize_url + "?" + urlencode({
            "client_id": s.client_id, "redirect_uri": s.redirect_uri,
            "scope": " ".join(s.scopes), "state": state})

    def complete(self, state: str | None, code: str | None,
                 error: str | None = None) -> dict[str, Any]:
        """Handle the redirect back. The state is checked first and is single-use."""
        if not state or not self._take(state):
            raise AuthError("INVALID_STATE", "unknown, expired or already used state")
        if error:
            raise AuthError("AUTHORIZATION_DENIED", "authorization was not granted")
        if not code:
            raise AuthError("MISSING_CODE", "no authorization code in the callback")
        body = self._token({"grant_type": "authorization_code", "code": code,
                            "redirect_uri": self.settings.redirect_uri})
        if not isinstance(body.get("refresh_token"), str):
            raise AuthError("MALFORMED_TOKEN_RESPONSE", "no refresh token in the response")
        granted = body.get("scopes")
        granted = [str(x) for x in granted] if isinstance(granted, list) else []
        missing = sorted(set(self.settings.scopes) - set(granted))
        if missing:  # nothing is stored for a grant that cannot do the job
            raise AuthError("MISSING_SCOPES", "grant lacks: " + ", ".join(missing))
        with self._lock:
            st = self._load()
            self._apply(st, body)
            st.scopes, st.needs_reauthorization = sorted(granted), False
            hub = body.get("hub_id")
            st.hub_id = hub if isinstance(hub, int) else None
            self._save(st)
        return self.status()

    def _take(self, state: str) -> bool:
        with self._lock:
            match = next((k for k in self._pending if hmac.compare_digest(k, state)), None)
            if match is None:
                return False
            return self._pending.pop(match) > self._clock()

    # -- tokens ----------------------------------------------------------------------

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            st = self._load()
            if st.refresh_token is None:
                raise NotConnected(
                    "REAUTHORIZATION_REQUIRED" if st.needs_reauthorization
                    else "NOT_CONNECTED", "no HubSpot account is connected")
            if not force_refresh and st.access_token and \
                    self._clock() < st.expires_at - EXPIRY_SKEW:
                return st.access_token
            try:
                body = self._token({"grant_type": "refresh_token",
                                    "refresh_token": st.refresh_token})
            except AuthError as e:
                if e.code != "INVALID_GRANT":
                    raise
                st.access_token = st.refresh_token = None
                st.needs_reauthorization = True
                self._save(st)
                raise NotConnected("REAUTHORIZATION_REQUIRED",
                                   "the HubSpot grant was revoked or expired") from None
            self._apply(st, body)
            self._save(st)
            assert st.access_token is not None
            return st.access_token

    def _apply(self, st: _Stored, body: Mapping[str, Any]) -> None:
        st.access_token = str(body["access_token"])
        if isinstance(body.get("refresh_token"), str):
            st.refresh_token = body["refresh_token"]
        st.expires_at = self._clock() + float(body["expires_in"])

    def _token(self, fields: dict[str, str]) -> dict[str, Any]:
        s = self.settings
        try:
            r = self._http.post(f"{s.oauth_base}/oauth/{API_VERSION}/token", data={
                **fields, "client_id": s.client_id, "client_secret": s.client_secret})
        except httpx.HTTPError as e:
            raise AuthError("TOKEN_ENDPOINT_UNREACHABLE", type(e).__name__,
                            transient=True) from None
        body = _json(r)
        if r.status_code >= 500:
            raise AuthError("TOKEN_ENDPOINT_ERROR", f"status {r.status_code}", transient=True)
        if r.status_code != 200:
            err = body.get("error")
            err = err if isinstance(err, str) and _ERROR_CODE.match(err) else "rejected"
            raise AuthError("INVALID_GRANT" if err == "invalid_grant" else "TOKEN_REJECTED",
                            f"status {r.status_code} ({err})")
        if not isinstance(body.get("access_token"), str) or \
                not isinstance(body.get("expires_in"), int | float):
            raise AuthError("MALFORMED_TOKEN_RESPONSE", "token response is not usable")
        return body

    def disconnect(self) -> dict[str, Any]:
        """Revoke the grant upstream if possible, then forget the tokens regardless."""
        s = self.settings
        with self._lock:
            st = self._load()
            revoked = False
            if st.refresh_token:
                try:
                    r = self._http.post(f"{s.oauth_base}/oauth/{REVOKE_VERSION}/token/revoke",
                                        data={"token": st.refresh_token,
                                              "token_type_hint": "refresh_token",
                                              "client_id": s.client_id,
                                              "client_secret": s.client_secret})
                    revoked = r.status_code < 300
                except httpx.HTTPError:
                    revoked = False
            st.access_token = st.refresh_token = None
            st.expires_at, st.needs_reauthorization = 0.0, False
            self._save(st)
        return {"connected": False, "revoked_upstream": revoked}

    # -- connection facts ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Connection state for operators. Contains no token material."""
        st = self._load()
        connected = st.refresh_token is not None
        return {"connected": connected, "needs_reauthorization": st.needs_reauthorization,
                "hub_id": st.hub_id, "scopes": st.scopes if connected else [],
                "access_token_valid_for": max(0, int(st.expires_at - self._clock()))
                if connected and st.access_token else 0,
                "links": dict(st.links), "api_version": API_VERSION}

    def link(self, account_id: str, company_id: str) -> None:
        """Declare which HubSpot company is which account. Operator configuration."""
        if not company_id.isdigit():
            raise ValueError("a HubSpot company id is numeric")
        with self._lock:
            st = self._load()
            st.links[company_id] = account_id
            self._save(st)

    def account_for(self, company_id: str) -> str | None:
        return self._load().links.get(company_id)

    def marker(self, idempotency_key: str) -> str:
        """Token that identifies one write upstream. Keyed, so it cannot be derived
        from anything the model sees, and the runner's key itself never leaves."""
        secret = self._load().marker_secret.encode()
        return "tw-" + hmac.new(secret, idempotency_key.encode(),
                                hashlib.sha256).hexdigest()[:32]

    # -- storage ---------------------------------------------------------------------

    def _load(self) -> _Stored:
        path = self.settings.store
        with self._lock:
            if not path.exists():
                st = _Stored(marker_secret=secrets.token_hex(32))
                self._save(st)
                return st
            try:
                return _Stored(**json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError):
                raise AuthError("STORE_UNREADABLE",
                                "the connection store is not readable") from None

    def _save(self, st: _Stored) -> None:
        path = self.settings.store
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(st), f)
        os.chmod(tmp, 0o600)
        tmp.replace(path)


def _json(r: httpx.Response) -> dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
