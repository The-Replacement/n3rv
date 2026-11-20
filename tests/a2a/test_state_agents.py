"""Agent registry store: register/list/stale/unregister/prune."""

from __future__ import annotations

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


def test_prune_only_when_over_cap(store: HubStateStore) -> None:
    store.register_agent("radio-session")

    assert store.prune_agents(ttl_seconds=-1, max_records=256) == 0
    assert store.prune_agents(ttl_seconds=-1, max_records=0) == 1
