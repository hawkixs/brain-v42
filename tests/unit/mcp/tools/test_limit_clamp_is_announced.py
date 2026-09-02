"""A `limit` cap applied silently makes the result lie.

Ticket af3b58dd, item 4. `brain_search`, `brain_list` and `brain_list_adrs` clamp
`limit` into [1, 100] through `max(1, min(limit, 100))` and return the page saying
nothing. A caller asking for 500 receives 100 and cannot tell "there were only 100"
from "there were 500 and you are shown 100".

It is the repository contradicting itself: in the SAME file, `_format_plan_detail`
documents "no content is silently dropped — the notice names the number of omitted
chunks". The rule exists, these three paths do not follow it.

The CLAMP contract is not called into question in favour of `brain_session_list`'s
hard rejection: for an LLM caller, a refusal costs a round trip where an announced
cap costs a sentence. What is fixed is the SILENCE, not the cap.
"""

from __future__ import annotations

import pytest

from brain_v42.mcp.tools.formatters import LIST_LIMIT_MAX, clamp_list_limit


class TestClampListLimit:
    def test_a_request_within_the_cap_is_returned_untouched_and_silent(self) -> None:
        """The nominal case must produce NO noise.

        Without this assertion, one could "fix" it by announcing the cap on every
        call — the reader would stop reading the notice, exactly the drift this
        repository documents elsewhere for the alarm that rings every night.
        """
        value, notice = clamp_list_limit(20)

        assert value == 20
        assert notice == ""

    def test_a_request_above_the_cap_says_so(self) -> None:
        value, notice = clamp_list_limit(500)

        assert value == LIST_LIMIT_MAX
        assert str(LIST_LIMIT_MAX) in notice
        assert "500" in notice, "la notice doit rappeler ce qui a été DEMANDÉ"

    @pytest.mark.parametrize("asked", [0, -1, -100])
    def test_a_non_positive_request_says_so_too(self, asked: int) -> None:
        """Zero returned an empty page silently — indistinguishable from an empty corpus."""
        value, notice = clamp_list_limit(asked)

        assert value == 1
        assert notice, f"limit={asked} a été corrigé sans le dire"

    def test_the_cap_is_the_one_the_tools_actually_apply(self) -> None:
        """Positive control: a cap decoupled from the tools would guard nothing."""
        assert LIST_LIMIT_MAX == 100


class TestToolsAnnounceTheClamp:
    """The behavioural witness, on the three tools the ticket names."""

    @staticmethod
    def _sources() -> list[str]:
        from pathlib import Path

        root = Path(__file__).resolve().parents[4] / "src" / "brain_v42" / "mcp" / "tools"
        return [
            (root / name).read_text(encoding="utf-8")
            for name in ("brain_tools.py", "crud_tools.py")
        ]

    def test_no_list_path_still_clamps_silently(self) -> None:
        """The NEGATIVE probe: it fires again if anyone hardcodes the clamp.

        RED before the fix: `max(1, min(limit, 100))` appears three times.
        """
        offenders = [
            f"{index}: {line.strip()}"
            for index, source in enumerate(self._sources())
            for line in source.splitlines()
            if "min(limit, 100)" in line
        ]

        assert offenders == [], (
            "un plafond de limit est appliqué en dur, donc en silence ; utiliser "
            "clamp_list_limit qui rend aussi la notice :\n" + "\n".join(offenders)
        )
