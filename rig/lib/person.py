"""A signed-in person: the dashboard's own routes and socket, on the standard
library. Everything a key may not do runs through this driver."""
from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .chat import ChatSession
from .websocket import WebSocket


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, body):
        super().__init__(f"{method} {path} answered {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


class Http:
    """One base address, one cookie jar, JSON both ways, redirects not followed."""

    def __init__(self, base: str, *, token: str = ""):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.token = token
        no_redirect = type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {
            "redirect_request": lambda self, *a, **k: None})
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), no_redirect())

    def raw(self, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None,
            timeout: float = 60.0) -> tuple[int, bytes, dict]:
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=dict(headers or {}))
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._opener.open(request, timeout=timeout) as reply:
                return reply.status, reply.read(), dict(reply.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def call(self, method: str, path: str, json_body=None, *, timeout: float = 60.0,
             headers: dict | None = None) -> tuple[int, object]:
        body = None if json_body is None else json.dumps(json_body).encode()
        sent = dict(headers or {})
        if body is not None:
            sent["Content-Type"] = "application/json"
        status, data, _ = self.raw(method, path, body=body, headers=sent, timeout=timeout)
        try:
            return status, json.loads(data) if data else None
        except ValueError:
            return status, data.decode(errors="replace")

    def ok(self, method: str, path: str, json_body=None, **kw):
        """The answer of a call that must succeed."""
        status, data = self.call(method, path, json_body, **kw)
        if status >= 300:
            raise ApiError(method, path, status, data)
        return data


class Person(Http):
    """Someone signed in with a password, as a browser is."""

    def __init__(self, base: str, email: str, password: str):
        super().__init__(base)
        self.email = email
        self.password = password
        self.me: dict = {}

    def sign_in(self) -> Person:
        self.ok("POST", "/auth/login/local", {"email": self.email, "password": self.password})
        me = self.ok("GET", "/auth/me")
        self.me = me.get("user", me)
        return self

    @property
    def sub(self) -> str:
        return self.me.get("sub", "")

    @property
    def username(self) -> str:
        return self.me["username"]

    def _cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

    def socket(self) -> WebSocket:
        parsed = urlparse(self.base)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return WebSocket(f"{scheme}://{parsed.netloc}/ws/dashboard", headers={"Cookie": self._cookie_header()})

    def chat(self, agent: str, *, engine: str = "", model: str = "", text: str = "", mode: str = "dontAsk",
             terminal: bool = False, target: str = "", chat_id: str = "") -> ChatSession:
        """Open a chat on a new socket, as the chat page does; with ``text``
        the first message rides the warmup and starts the turn."""
        session = ChatSession(self, agent=agent)
        try:
            session.open(engine=engine, model=model, text=text, mode=mode, terminal=terminal, target=target,
                         chat_id=chat_id)
        except BaseException:
            # A refused chat leaves no socket behind: a caller that tries again opens its own.
            session.close()
            raise
        return session

    def rows(self, chat_id: str) -> list[dict]:
        data = self.ok("GET", f"/v1/chats/{chat_id}")
        return (data.get("messages") if isinstance(data, dict) else data) or []

    def wait_until(self, what: str, probe, *, timeout: float = 120.0, every: float = 2.0):
        """Poll ``probe`` until it answers something truthy; raise at the bound."""
        deadline = time.monotonic() + timeout
        while True:
            got = probe()
            if got:
                return got
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{what}: not within {timeout:.0f} s")
            time.sleep(every)
