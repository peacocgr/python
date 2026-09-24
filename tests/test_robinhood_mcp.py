import asyncio
import json
import os
import socket
import threading
import time

import pytest

from rhtrader.robinhood_mcp import FileTokenStorage, _parse_payload

mcp = pytest.importorskip("mcp")


def test_parse_payload_handles_text_and_wrapped_structured_content():
    body = {"data": {"x": 1}, "guide": "g"}
    assert _parse_payload(json.dumps(body), None) == body
    assert _parse_payload("", {"result": json.dumps(body)}) == body
    assert _parse_payload("not json", body) == body
    assert _parse_payload("not json", None) is None


def test_token_storage_is_private_and_round_trips(tmp_path):
    from mcp.shared.auth import OAuthToken

    store = FileTokenStorage(tmp_path / "sub" / "oauth.json")
    token = OAuthToken(access_token="a", token_type="Bearer", expires_in=3600, refresh_token="r")
    asyncio.run(store.set_tokens(token))
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"
    loaded = asyncio.run(store.get_tokens())
    assert loaded.access_token == "a" and loaded.refresh_token == "r"
    assert store.expires_at() == pytest.approx(time.time() + 3600, abs=5)


@pytest.fixture
def fake_server():
    """A local MCP server over streamable HTTP with Robinhood-shaped tools."""
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.mcpserver import MCPServer

    srv = MCPServer("fake-robinhood")

    @srv.tool()
    def get_accounts() -> str:
        return json.dumps({"data": {"accounts": [{"account_number": "X1", "agentic_allowed": True}]},
                           "guide": "..."})

    @srv.tool()
    def fails() -> str:
        raise ValueError("nope")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(srv.streamable_http_app(), host="127.0.0.1",
                                          port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def test_client_calls_tools_over_http(fake_server, tmp_path):
    from rhtrader.robinhood_mcp import RobinhoodMCP, ToolError

    with RobinhoodMCP(fake_server, tmp_path / "oauth.json") as rh:
        assert rh.call("get_accounts")["accounts"][0]["account_number"] == "X1"
        with pytest.raises(ToolError):
            rh.call("fails")


@pytest.fixture
def oauth_server():
    """MCP server behind a minimal OAuth 2.1 authorization server.

    Unauthenticated MCP requests get 401; the client must discover metadata,
    register, send the user to /authorize, exchange the code at /token and
    retry with the bearer token.
    """
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.mcpserver import MCPServer
    from starlette.requests import Request
    from starlette.responses import JSONResponse, RedirectResponse, Response

    srv = MCPServer("fake-robinhood")

    @srv.tool()
    def get_accounts() -> str:
        return json.dumps({"data": {"accounts": [{"account_number": "X1"}]}})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base = f"http://127.0.0.1:{port}"
    state = {"registered": 0, "token_requests": [], "authorized": 0}
    mcp_app = srv.streamable_http_app()

    async def oauth(scope, receive, send):
        request = Request(scope, receive)
        path = scope["path"]
        if path.startswith("/.well-known/oauth-protected-resource"):
            resp = JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})
        elif path.startswith("/.well-known/oauth-authorization-server"):
            resp = JSONResponse({
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
            })
        elif path == "/register":
            body = await request.json()
            state["registered"] += 1
            resp = JSONResponse({**body, "client_id": "cid"}, status_code=201)
        elif path == "/authorize":
            q = request.query_params
            state["authorized"] += 1
            resp = RedirectResponse(f"{q['redirect_uri']}?code=thecode&state={q['state']}")
        elif path == "/token":
            form = await request.form()
            state["token_requests"].append(dict(form))
            resp = JSONResponse({"access_token": "tok", "token_type": "Bearer",
                                 "expires_in": 3600, "refresh_token": "ref"})
        else:
            resp = Response(status_code=404)
        await resp(scope, receive, send)

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await mcp_app(scope, receive, send)  # lifespan
        if scope["path"].startswith("/mcp"):
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != b"Bearer tok":
                resp = Response(status_code=401, headers={
                    "WWW-Authenticate":
                        f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'})
                return await resp(scope, receive, send)
            return await mcp_app(scope, receive, send)
        return await oauth(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"{base}/mcp", state
    server.should_exit = True
    thread.join(timeout=5)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unattended_run_without_login_fails_fast(oauth_server, tmp_path):
    from rhtrader.robinhood_mcp import LoginRequired, RobinhoodMCP

    url, _ = oauth_server
    with pytest.raises(LoginRequired, match="rhtrader login"):
        with RobinhoodMCP(url, tmp_path / "oauth.json", _free_port(), interactive=False, timeout=20):
            pass


def test_browser_login_then_silent_reuse(oauth_server, tmp_path, monkeypatch):
    import urllib.request
    import webbrowser

    from rhtrader.robinhood_mcp import RobinhoodMCP

    url, state = oauth_server
    # Play the user's browser: follow /authorize, which redirects to the callback.
    monkeypatch.setattr(
        webbrowser, "open",
        lambda u: threading.Thread(target=lambda: urllib.request.urlopen(u).read(), daemon=True).start(),
    )
    token_file = tmp_path / "oauth.json"
    port = _free_port()
    with RobinhoodMCP(url, token_file, port, interactive=True, timeout=20) as rh:
        assert rh.call("get_accounts")["accounts"][0]["account_number"] == "X1"

    exchange = state["token_requests"][0]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "thecode"
    assert exchange["code_verifier"]  # PKCE
    assert state["registered"] == 1 and state["authorized"] == 1

    # Second run: no browser, no re-registration; stored token is reused.
    monkeypatch.setattr(webbrowser, "open", lambda u: pytest.fail("browser opened again"))
    with RobinhoodMCP(url, token_file, port, interactive=False, timeout=20) as rh:
        rh.call("get_accounts")
    assert state["registered"] == 1 and len(state["token_requests"]) == 1


def test_expired_token_is_refreshed_without_browser(oauth_server, tmp_path, monkeypatch):
    import urllib.request
    import webbrowser

    from rhtrader.robinhood_mcp import RobinhoodMCP

    url, state = oauth_server
    monkeypatch.setattr(
        webbrowser, "open",
        lambda u: threading.Thread(target=lambda: urllib.request.urlopen(u).read(), daemon=True).start(),
    )
    token_file = tmp_path / "oauth.json"
    port = _free_port()
    with RobinhoodMCP(url, token_file, port, interactive=True, timeout=20):
        pass

    data = json.loads(token_file.read_text())
    data["expires_at"] = time.time() - 60
    token_file.write_text(json.dumps(data))

    monkeypatch.setattr(webbrowser, "open", lambda u: pytest.fail("browser opened again"))
    with RobinhoodMCP(url, token_file, port, interactive=False, timeout=20) as rh:
        rh.call("get_accounts")
    assert state["token_requests"][-1]["grant_type"] == "refresh_token"
    assert state["token_requests"][-1]["refresh_token"] == "ref"
