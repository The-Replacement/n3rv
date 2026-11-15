"""Every generated output must belong to exactly one component family.

The reconciler selects manifest entries by family, so an entry that maps to no
family would silently escape selection. There is no catch-all: this test is the
gate that forces a decision when a template is added.
"""

from __future__ import annotations

from collections import Counter

import pytest

import n3rv.init as n3rv_init
from n3rv.init import (
    ALWAYS_ON_FAMILY,
    DECLARABLE_FAMILIES,
    FAMILY_PREFIXES,
    UnknownFamilyError,
    family_for,
)


def _outputs() -> list[str]:
    return [entry[1] for entry in n3rv_init.FILE_MANIFEST]


class TestFamilyCoverage:
    def test_every_manifest_output_has_a_family(self) -> None:
        for output in _outputs():
            assert family_for(output) in self._known_families()

    def test_manifest_counts_per_family_are_pinned(self) -> None:
        """A template moving between families changes a user's installed set."""
        counts = Counter(family_for(output) for output in _outputs())
        assert counts == {
            "skills": 18,
            "agents": 13,
            "commands": 6,
            "spec-kit": 6,
            "runtime": 2,
            "hooks": 1,
            ALWAYS_ON_FAMILY: 7,
        }

    def test_every_output_maps_to_exactly_one_family(self) -> None:
        for output in _outputs():
            matches = [family for prefix, family in FAMILY_PREFIXES if output.startswith(prefix)]
            assert len(matches) <= 1, f"{output} matches several prefixes: {matches}"

    def test_unmapped_output_raises(self) -> None:
        with pytest.raises(UnknownFamilyError):
            family_for("some/new/unmapped.txt")

    def test_always_on_outputs_are_not_declarable(self) -> None:
        assert ALWAYS_ON_FAMILY not in DECLARABLE_FAMILIES
        assert DECLARABLE_FAMILIES == {"agents", "skills", "commands", "hooks"}

    def test_declarable_families_all_appear_in_the_manifest(self) -> None:
        present = {family_for(output) for output in _outputs()}
        assert DECLARABLE_FAMILIES <= present

    def test_adding_an_unmapped_entry_fails_coverage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The gate itself: a new template with nowhere to live breaks the suite."""
        bogus = ("some/new-template.j2", "brand-new-output.txt", False, False)
        monkeypatch.setattr(n3rv_init, "FILE_MANIFEST", [*n3rv_init.FILE_MANIFEST, bogus])

        with pytest.raises(UnknownFamilyError):
            for output in _outputs():
                family_for(output)

    def _known_families(self) -> set[str]:
        return {family for _prefix, family in FAMILY_PREFIXES} | {ALWAYS_ON_FAMILY}
