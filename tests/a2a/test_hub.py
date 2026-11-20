from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from n3rv.a2a.hub import A2AHub


async def build_client(runtime_settings) -> TestClient:
    hub = A2AHub(runtime_settings)
    client = TestClient(TestServer(hub.create_app()))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_serves_agent_cards(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        response = await client.get("/.well-known/agent.json")
        payload = await response.json()

        assert response.status == 200
        assert payload["name"] == "n3rv-hub"
        assert payload["capabilities"] == {"streaming": True}
        assert "authentication" not in payload
        child = await client.get("/agents/opencode/agent.json")
        child_payload = await child.json()
        assert child_payload["name"] == "opencode"
        assert child_payload["capabilities"] == {"streaming": True}
        assert "authentication" not in child_payload
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tasks_send_persists_completed_task(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        response = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "tasks/send",
                "params": {
                    "requesting_agent": "claude",
                    "skill_id": "implementation",
                    "description": "Implement auth module from design",
                    "metadata": {"auto_complete": True},
                },
            },
        )
        payload = await response.json()

        assert response.status == 200
        task = payload["result"]
        assert task["status"]["state"] == "completed"
        task_path = runtime_settings.paths.hub_state_dir / "tasks" / f"{task['id']}.json"
        assert task_path.exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tasks_get_returns_persisted_task(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        sent = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "5",
                "method": "tasks/send",
                "params": {
                    "requesting_agent": "claude",
                    "skill_id": "implementation",
                    "description": "Implement auth module from design",
                    "metadata": {"auto_complete": True},
                },
            },
        )
        task_id = (await sent.json())["result"]["id"]

        fetched = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "6",
                "method": "tasks/get",
                "params": {"task_id": task_id},
            },
        )
        payload = await fetched.json()

        assert fetched.status == 200
        assert payload["result"]["id"] == task_id
        assert payload["result"]["status"]["state"] == "completed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_working_task(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        sent = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "3",
                "method": "tasks/send",
                "params": {
                    "requesting_agent": "claude",
                    "skill_id": "implementation",
                    "description": "Implement auth module from design",
                    "metadata": {"auto_complete": False},
                },
            },
        )
        task_id = (await sent.json())["result"]["id"]

        cancelled = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "4",
                "method": "tasks/cancel",
                "params": {
                    "task_id": task_id,
                    "reason": "user requested",
                    "requesting_agent": "opencode",
                },
            },
        )
        payload = await cancelled.json()

        assert cancelled.status == 200
        assert payload["result"]["status"]["state"] == "canceled"
    finally:
        await client.close()


async def _send_task(client, **overrides):
    params = {
        "requesting_agent": "claude",
        "skill_id": "implementation",
        "description": "Implement auth module from design",
        "metadata": {"auto_complete": False},
    }
    params.update(overrides)
    response = await client.post(
        "/rpc",
        json={"jsonrpc": "2.0", "id": "10", "method": "tasks/send", "params": params},
    )
    assert response.status == 200
    return (await response.json())["result"]


@pytest.mark.asyncio
async def test_tasks_send_leaves_task_submitted_without_started_at(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        task = await _send_task(client)

        assert task["status"]["state"] == "submitted"
        assert task["metadata"]["assigned_agent"] not in ("", "pending")
        assert "started_at" not in task["metadata"]
        assert "routed_at" in task["metadata"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_claim_moves_task_to_working_with_started_at(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        task = await _send_task(client)
        agent = task["metadata"]["assigned_agent"]

        response = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "11",
                "method": "tasks/claim",
                "params": {"task_id": task["id"], "agent_id": agent},
            },
        )
        claimed = (await response.json())["result"]

        assert response.status == 200
        assert claimed["status"]["state"] == "working"
        assert "started_at" in claimed["metadata"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_claim_by_other_agent_is_forbidden_and_names_assignee(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        task = await _send_task(client)
        agent = task["metadata"]["assigned_agent"]

        response = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "12",
                "method": "tasks/claim",
                "params": {"task_id": task["id"], "agent_id": "someone-else"},
            },
        )

        assert response.status == 403
        assert agent in await response.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_complete_by_other_agent_is_forbidden_and_names_assignee(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        task = await _send_task(client)
        agent = task["metadata"]["assigned_agent"]

        response = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "13",
                "method": "tasks/complete",
                "params": {
                    "task_id": task["id"],
                    "result": "done",
                    "completing_agent": "someone-else",
                },
            },
        )

        assert response.status == 403
        assert agent in await response.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_with_target_agent_assigns_that_agent_and_stays_submitted(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        await _register(client, "radio-session")
        task = await _send_task(client, target_agent="radio-session")

        assert task["status"]["state"] == "submitted"
        assert task["metadata"]["assigned_agent"] == "radio-session"
    finally:
        await client.close()


async def _register(client, agent_id):
    response = await client.post(
        "/rpc",
        json={
            "jsonrpc": "2.0",
            "id": "20",
            "method": "agents/register",
            "params": {"agent_id": agent_id},
        },
    )
    assert response.status == 200
    return (await response.json())["result"]


@pytest.mark.asyncio
async def test_agents_register_then_list(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        await _register(client, "radio-session")
        response = await client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": "21", "method": "agents/list", "params": {}},
        )
        agents = (await response.json())["result"]["agents"]

        assert [a["agent_id"] for a in agents] == ["radio-session"]
        assert agents[0]["stale"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delegate_to_registered_target_assigns_it(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        await _register(client, "radio-session")
        task = await _send_task(client, target_agent="radio-session")

        assert task["status"]["state"] == "submitted"
        assert task["metadata"]["assigned_agent"] == "radio-session"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delegate_to_unknown_target_is_rejected_and_lists_live_ids(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        await _register(client, "radio-session")
        response = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "22",
                "method": "tasks/send",
                "params": {
                    "requesting_agent": "claude",
                    "skill_id": "implementation",
                    "description": "x",
                    "target_agent": "typo-session",
                    "metadata": {"auto_complete": False},
                },
            },
        )

        assert response.status == 400
        body = await response.text()
        assert "typo-session" in body
        assert "radio-session" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delegate_allow_offline_queues_unknown_target(runtime_settings) -> None:
    client = await build_client(runtime_settings)
    try:
        task = await _send_task(client, target_agent="ghost", allow_offline=True)

        assert task["status"]["state"] == "submitted"
        assert task["metadata"]["assigned_agent"] == "ghost"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delegate_to_idle_known_target_is_accepted(runtime_settings, monkeypatch) -> None:
    """A registered id stays addressable after it goes stale (n3rv#38).

    TTL=0 makes every record stale immediately, so the registry says the target
    is idle while the routing decision must still accept the handoff: an
    address that expires is not an address. The staleness is reported on the
    task instead of being turned into a rejection.
    """
    client = await build_client(runtime_settings)
    try:
        await _register(client, "radio-session")
        monkeypatch.setenv("N3RV_AGENT_TTL_SECONDS", "0")

        listed = await client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": "23a", "method": "agents/list", "params": {}},
        )
        assert (await listed.json())["result"]["agents"] == []

        task = await _send_task(client, target_agent="radio-session")

        assert task["status"]["state"] == "submitted"
        assert task["metadata"]["assigned_agent"] == "radio-session"
        assert task["metadata"]["target_stale"] is True
        assert task["metadata"]["target_last_seen"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delegate_to_retention_pruned_target_is_rejected(runtime_settings) -> None:
    """The address disappears only once the record is collected, not when idle."""
    client = await build_client(runtime_settings)
    try:
        await _register(client, "radio-session")
        record_path = runtime_settings.paths.hub_state_dir / "agents" / "radio-session.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["last_seen"] = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        record_path.write_text(json.dumps(record), encoding="utf-8")

        listed = await client.post(
            "/rpc",
            json={"jsonrpc": "2.0", "id": "23b", "method": "agents/list", "params": {"include_stale": True}},
        )
        assert (await listed.json())["result"]["agents"] == []

        response = await client.post(
            "/rpc",
            json={
                "jsonrpc": "2.0",
                "id": "23c",
                "method": "tasks/send",
                "params": {
                    "requesting_agent": "claude",
                    "skill_id": "implementation",
                    "description": "x",
                    "target_agent": "radio-session",
                    "metadata": {"auto_complete": False},
                },
            },
        )

        assert response.status == 400
        assert "radio-session" in await response.text()
    finally:
        await client.close()
