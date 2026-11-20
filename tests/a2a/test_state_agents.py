"""Agent registry store: register/list/stale/unregister/prune."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from n3rv.a2a.state import HubStateStore
from n3rv.config import RuntimePaths, RuntimeSettings


@pytest.fixture
def store(tmp_path: Path) -> HubStateStore:
    return HubStateStore(RuntimeSettings(paths=RuntimePaths.from_project_root(tmp_path)))


def test_register_then_list_live(store: HubStateStore) -> None:
    record = store.register_agent("radio-session")

    agents = store.list_agents(ttl_seconds=120)

    assert record.agent_id == "radio-session"
    assert [(r.agent_id, stale) for r, stale in agents] == [("radio-session", False)]


def test_register_is_idempotent_and_preserves_started_at(store: HubStateStore) -> None:
    first = store.register_agent("radio-session")
    second = store.register_agent("radio-session")

    assert second.started_at == first.started_at
    assert second.last_seen >= first.last_seen
    assert len(store.list_agents(ttl_seconds=120)) == 1


def test_stale_entry_hidden_unless_include_stale(store: HubStateStore) -> None:
    store.register_agent("radio-session")

    assert store.list_agents(ttl_seconds=-1) == []
    agents = store.list_agents(ttl_seconds=-1, include_stale=True)
    assert [(r.agent_id, stale) for r, stale in agents] == [("radio-session", True)]


def test_unregister_removes(store: HubStateStore) -> None:
    store.register_agent("radio-session")

    assert store.unregister_agent("radio-session") is True
    assert store.unregister_agent("radio-session") is False
    assert store.list_agents(ttl_seconds=120, include_stale=True) == []


def test_invalid_agent_id_rejected(store: HubStateStore) -> None:
    with pytest.raises(ValueError):
        store.register_agent("../escape")


def test_prune_collects_records_past_retention(store: HubStateStore) -> None:
    store.register_agent("radio-session")
    path = store.agents_dir / "radio-session.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["last_seen"] = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    path.write_text(json.dumps(record), encoding="utf-8")

    assert store.prune_agents(retention_seconds=30 * 86400) == 1
    assert store.list_agents(ttl_seconds=120, include_stale=True) == []


def test_prune_keeps_stale_but_recent_records(store: HubStateStore) -> None:
    """Idle is not gone: a stale record inside the retention window survives."""
    store.register_agent("radio-session")

    assert store.prune_agents(retention_seconds=30 * 86400) == 0
    assert store.list_agents(ttl_seconds=0, include_stale=True)[0][0].agent_id == "radio-session"


def test_prune_disabled_when_retention_is_not_positive(store: HubStateStore) -> None:
    store.register_agent("radio-session")

    assert store.prune_agents(retention_seconds=0) == 0
    assert store.list_agents(ttl_seconds=120, include_stale=True) != []
