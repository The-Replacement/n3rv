"""hub_rpc must not swallow the hub's error detail (n3rv#38).

The hub answers validation failures with a plain-text 400 whose body is the
only actionable part, e.g. ``unknown or stale agent 'x'; live ids: ['n3rv']``.
Wrapping it as ``HubUnavailable`` dropped that body, so an MCP caller saw a
generic tool error and had to guess which id to use.
"""

from __future__ import annotations

import httpx
import pytest

from n3rv.mcp import shared

HUB_URL = "http://127.0.0.1:19820"


def _response(status_code: int, **kwargs) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("POST", f"{HUB_URL}/rpc"),
        **kwargs,
    )


def test_http_error_status_keeps_hub_message(monkeypatch: pytest.MonkeyPatch) -> None:
    body = "unknown or stale agent 'nope'; live ids: ['n3rv']"
    monkeypatch.setattr(shared.httpx, "post", lambda *a, **kw: _response(400, text=body))

    with pytest.raises(Exception) as excinfo:
        shared.hub_rpc(HUB_URL, "tasks/send", {"target_agent": "nope"})

    message = str(excinfo.value)
    assert body in message
    assert "HubUnavailable" not in message
    assert "400" in message


def test_http_error_without_body_names_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared.httpx, "post", lambda *a, **kw: _response(400, text=""))

    with pytest.raises(Exception) as excinfo:
        shared.hub_rpc(HUB_URL, "tasks/get", {})

    assert "HubError 400" in str(excinfo.value)


def test_transport_failure_is_hub_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(shared.httpx, "post", _boom)

    with pytest.raises(Exception) as excinfo:
        shared.hub_rpc(HUB_URL, "tasks/list", {})

    assert "HubUnavailable" in str(excinfo.value)


def test_jsonrpc_error_object_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"jsonrpc": "2.0", "id": "1", "error": {"code": -32602, "message": "bad params"}}
    monkeypatch.setattr(shared.httpx, "post", lambda *a, **kw: _response(200, json=payload))

    with pytest.raises(Exception) as excinfo:
        shared.hub_rpc(HUB_URL, "tasks/get", {})

    assert "bad params" in str(excinfo.value)


def test_non_json_success_names_the_method(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared.httpx, "post", lambda *a, **kw: _response(200, text="<html>"))

    with pytest.raises(Exception) as excinfo:
        shared.hub_rpc(HUB_URL, "agents/list", {})

    assert "non-JSON" in str(excinfo.value)
    assert "agents/list" in str(excinfo.value)


def test_success_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"jsonrpc": "2.0", "id": "1", "result": {"agents": []}}
    monkeypatch.setattr(shared.httpx, "post", lambda *a, **kw: _response(200, json=payload))

    assert shared.hub_rpc(HUB_URL, "agents/list", {}) == {"agents": []}
