"""Tests for the project board adapter."""

from __future__ import annotations

import pytest

from n3rv import board


def _item(
    item_id: str,
    node_id: str,
    number: int,
    repo: str = "The-Replacement/n3rv",
    *,
    status: str | None = None,
    phase: str | None = None,
) -> dict:
    values = []
    if status:
        values.append(
            {
                "name": "In Progress" if status == board.STATUS_OPTIONS["in progress"] else "Done",
                "optionId": status,
                "field": {"id": board.STATUS_FIELD_ID, "name": "Status"},
            }
        )
    if phase:
        values.append(
            {
                "name": "x",
                "optionId": phase,
                "field": {"id": board.PHASE_FIELD_ID, "name": "SDD Phase"},
            }
        )
    return {
        "id": item_id,
        "content": {
            "__typename": "Issue",
            "id": node_id,
            "number": number,
            "title": f"issue {number}",
            "repository": {"nameWithOwner": repo},
        },
        "fieldValues": {"nodes": values},
    }


class FakeTransport:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, variables: dict) -> dict:
        self.calls.append((query, variables))
        if "mutation" in query:
            return {}
        index = 0 if variables.get("cursor") is None else int(variables["cursor"])
        return self.pages[index]

    @property
    def mutations(self) -> list[dict]:
        return [variables for query, variables in self.calls if "mutation" in query]


def _page(items: list[dict], *, next_cursor: str | None = None) -> dict:
    return {
        "organization": {
            "projectV2": {
                "id": board.BOARD_PROJECT_ID,
                "items": {
                    "pageInfo": {
                        "hasNextPage": next_cursor is not None,
                        "endCursor": next_cursor,
                    },
                    "nodes": items,
                },
            }
        }
    }


def test_sync_pages_through_every_item():
    """A match on page 2 is found: first:100 was the bug, not the fix."""
    transport = FakeTransport(
        [
            _page([_item("I_1", "N_1", 1)], next_cursor="1"),
            _page([_item("I_5", "N_5", 5)]),
        ]
    )

    result = board.sync_issue(phase="spec", repo="The-Replacement/n3rv", issue=5, transport=transport)

    assert result["item_id"] == "I_5"
    assert len([1 for query, _ in transport.calls if "mutation" not in query]) == 2


def test_sync_updates_status_and_phase_together():
    transport = FakeTransport([_page([_item("I_1", "N_1", 1)])])

    result = board.sync_issue(phase="archive", repo="The-Replacement/n3rv", issue=1, transport=transport)

    assert result["changed"] == ["status", "phase"]
    assert result["applied"] is True
    assert len(transport.mutations) == 1
    variables = transport.mutations[0]
    assert variables["statusOption"] == board.STATUS_OPTIONS["done"]
    assert variables["phaseOption"] == board.PHASE_OPTIONS["archive"]


def test_sync_is_idempotent():
    """Both fields already at target: no mutation, reported as unchanged."""
    item = _item(
        "I_1",
        "N_1",
        1,
        status=board.STATUS_OPTIONS["in progress"],
        phase=board.PHASE_OPTIONS["spec"],
    )
    transport = FakeTransport([_page([item])])

    result = board.sync_issue(phase="spec", repo="The-Replacement/n3rv", issue=1, transport=transport)

    assert result["applied"] is False
    assert result["changed"] == []
    assert transport.mutations == []


def test_sync_dry_run_resolves_without_writing():
    transport = FakeTransport([_page([_item("I_1", "N_1", 1)])])

    result = board.sync_issue(phase="apply", repo="The-Replacement/n3rv", issue=1, dry_run=True, transport=transport)

    assert result["dry_run"] is True
    assert result["applied"] is False
    assert result["changed"] == ["status", "phase"]
    assert transport.mutations == []


def test_sync_missing_item_fails_loudly():
    transport = FakeTransport([_page([_item("I_1", "N_1", 1)])])

    with pytest.raises(board.BoardError) as excinfo:
        board.sync_issue(phase="spec", repo="The-Replacement/n3rv", issue=99, transport=transport)

    assert excinfo.value.code == 3
    assert "not on project" in str(excinfo.value)


def test_sync_ambiguous_item_fails_loudly():
    transport = FakeTransport([_page([_item("I_1", "N_1", 7), _item("I_2", "N_2", 7)])])

    with pytest.raises(board.BoardError) as excinfo:
        board.sync_issue(phase="spec", repo="The-Replacement/n3rv", issue=7, transport=transport)

    assert excinfo.value.code == 2
    assert "ambiguous" in str(excinfo.value)


def test_sync_matches_repository_not_just_number():
    """Issue #5 in another repository must not match."""
    transport = FakeTransport(
        [
            _page(
                [
                    _item("I_1", "N_1", 5, repo="The-Replacement/hub"),
                    _item("I_2", "N_2", 5, repo="The-Replacement/n3rv"),
                ]
            )
        ]
    )

    result = board.sync_issue(phase="spec", repo="The-Replacement/n3rv", issue=5, transport=transport)

    assert result["item_id"] == "I_2"


def test_sync_accepts_issue_node_id():
    transport = FakeTransport([_page([_item("I_1", "N_9", 9)])])

    result = board.sync_issue(phase="todo", issue_node_id="N_9", transport=transport)

    assert result["item_id"] == "I_1"


def test_sync_rejects_unknown_phase():
    with pytest.raises(board.BoardError) as excinfo:
        board.sync_issue(phase="deploy", repo="The-Replacement/n3rv", issue=1, transport=FakeTransport([]))

    assert excinfo.value.code == 4


def test_sync_requires_issue_identifier():
    with pytest.raises(board.BoardError) as excinfo:
        board.sync_issue(phase="spec", transport=FakeTransport([]))

    assert excinfo.value.code == 4


def test_parse_repo_rejects_malformed_values():
    with pytest.raises(board.BoardError) as excinfo:
        board.parse_repo("n3rv")

    assert excinfo.value.code == 4


def test_project_missing_is_distinct_error():
    transport = FakeTransport([{"organization": {"projectV2": None}}])

    with pytest.raises(board.BoardError) as excinfo:
        board.sync_issue(phase="spec", repo="The-Replacement/n3rv", issue=1, transport=transport)

    assert excinfo.value.code == 4


def test_canonical_ids_cover_the_rendered_template_values():
    """The board skill renders from render_context(); both must agree."""
    context = board.render_context()
    assert context["board_number"] == board.BOARD_NUMBER
    assert context["board_project_id"] in board.CANONICAL_IDS
    for option_id in context["board_status_options"].values():
        assert option_id in board.CANONICAL_IDS
    for option_id in context["board_phase_options"].values():
        assert option_id in board.CANONICAL_IDS
