"""Robinhood Agentic Trading access over MCP with OAuth.

Robinhood exposes agent trading as an MCP server
(https://agent.robinhood.com/mcp/trading) protected by OAuth 2.1 + PKCE.
No username or password is ever handled here:

  * ``rhtrader login`` opens Robinhood's sign-in page in your browser. You
    approve access there, and Robinhood redirects back to a one-shot
    listener on 127.0.0.1 with an authorization code.
  * The resulting access/refresh tokens are stored in ``token_file``
    (mode 0600). Later runs refresh the access token silently, so
    scheduled runs work without a browser until you revoke access.

Orders can only be placed in the account you enabled for agentic trading
in the Robinhood app.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_URL = "https://agent.robinhood.com/mcp/trading"


class LoginRequired(RuntimeError):
    pass


class ToolError(RuntimeError):
    pass


class FileTokenStorage:
    """Persist OAuth client registration and tokens to a private JSON file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def expires_at(self) -> float | None:
        return self._read().get("expires_at")

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        data["expires_at"] = (
            time.time() + tokens.expires_in if tokens.expires_in else None
        )
        self._write(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


def _wait_for_callback(port: int, timeout: float) -> tuple[str, str | None, str | None]:
    """Serve one request on 127.0.0.1:port and return (code, state, iss)."""
    result: dict[str, list[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            if "code" in query or "error" in query:
                result.update(query)
            ok = "code" in query
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "rhtrader is authorized. You can close this tab." if ok else "Authorization failed."
            self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise LoginRequired(f"authorization denied: {result['error'][0]}")
    if "code" not in result:
        raise LoginRequired("timed out waiting for the browser authorization")
    first = lambda k: result[k][0] if k in result else None  # noqa: E731
    return result["code"][0], first("state"), first("iss")


def _make_auth(url: str, storage: FileTokenStorage, port: int, interactive: bool):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    async def redirect_handler(auth_url: str) -> None:
        if not interactive:
            raise LoginRequired(
                "Robinhood authorization is missing or expired; run `rhtrader login`"
            )
        print(f"\nOpening Robinhood to authorize rhtrader:\n  {auth_url}\n")
        webbrowser.open(auth_url)

    async def callback_handler() -> AuthorizationCodeResult:
        code, state, iss = await asyncio.to_thread(_wait_for_callback, port, 300)
        return AuthorizationCodeResult(code=code, state=state, iss=iss)

    class Provider(OAuthClientProvider):
        async def _initialize(self) -> None:
            await super()._initialize()
            # The SDK forgets token expiry across restarts, which would force a
            # browser login instead of a silent refresh. Restore it.
            expires_at = storage.expires_at()
            if expires_at is not None:
                self.context.token_expiry_time = expires_at

    metadata = OAuthClientMetadata(
        client_name="rhtrader",
        redirect_uris=[f"http://127.0.0.1:{port}/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    return Provider(
        server_url=url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


class RobinhoodMCP:
    """Synchronous MCP client. Use as a context manager::

        with RobinhoodMCP(url, token_file) as rh:
            rh.call("get_accounts")

    The async MCP session runs on a private event loop thread so the rest
    of rhtrader can stay synchronous.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        token_file: str | Path = "~/.config/rhtrader/oauth.json",
        callback_port: int = 8765,
        interactive: bool = False,
        timeout: float = 60.0,
    ):
        self.url = url
        self.storage = FileTokenStorage(token_file)
        self.port = callback_port
        self.interactive = interactive
        self.timeout = timeout

    def __enter__(self) -> "RobinhoodMCP":
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self._main = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        try:
            # Allow time for a browser login on the first run.
            self._ready.result(timeout=330 if self.interactive else self.timeout)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    async def _run(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        self._stop = asyncio.Event()
        try:
            auth = _make_auth(self.url, self.storage, self.port, self.interactive)
            async with create_mcp_http_client(auth=auth) as http:
                async with streamable_http_client(self.url, http_client=http) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        self._ready.set_result(None)
                        await self._stop.wait()
        except BaseException as e:
            if not self._ready.done():
                self._ready.set_exception(_unwrap(e))
            else:
                raise

    def __exit__(self, *exc) -> None:
        if getattr(self, "_stop", None) is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._main.result(timeout=10)
        except BaseException:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a tool and return its ``data`` payload."""
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, arguments or {}), self._loop
        )
        result = fut.result(timeout=self.timeout)
        text = "".join(getattr(c, "text", "") for c in result.content)
        if result.is_error:
            raise ToolError(f"{tool}: {text or 'error'}")
        payload = _parse_payload(text, result.structured_content)
        if payload is None:
            raise ToolError(f"{tool}: unexpected non-JSON response: {text[:200]}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload


def _parse_payload(text: str, structured: Any) -> Any:
    """Robinhood's tools return JSON ``{"data": ..., "guide": ...}`` as text;
    some servers also (or only) send it as structured content, possibly
    wrapped as ``{"result": "<json>"}``."""
    for candidate in (text, structured):
        if isinstance(candidate, dict) and set(candidate) == {"result"}:
            candidate = candidate["result"]
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(candidate, (dict, list)):
            return candidate
    return None


def _unwrap(e: BaseException) -> BaseException:
    """Surface the real error from anyio exception groups."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    if isinstance(e, BaseExceptionGroup):
        for sub in e.exceptions:
            if isinstance(sub, LoginRequired):
                return sub
    return e
