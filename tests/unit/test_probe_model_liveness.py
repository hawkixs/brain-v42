"""Liveness probe for the configured models — item (3) of ticket 911bb6f5.

That ticket had deliberately deferred this item, with an explicit condition: "To
be reopened if a second EOL happens." It happened. The replacement chosen on
2026-08-05 after a canary — `deepseek-ai/deepseek-v4-flash` — reached its end of
life on 2026-08-07, two days later, and the night of 2026-08-10 fell back to its
8B fallback. red-arena's ticket 2fad6cc5 reports it in the same terms.

The machinery built on the 5th WORKED: the `DÉGRADÉ` line is indeed in the report
and `dream_runs.model` is populated. The remaining defect is no longer silence, it
is LATENCY — a model's death is learned by reading the next morning's report,
after a night served degraded across ten projects.

A 410 is not a transient error: no retry will ever repair it. An out-of-run probe
makes it knowable BEFORE the night, and lets a replacement be chosen by
measurement rather than from the provider's datasheet.

This probe is wired to no run: it is an operator's tool, and the ticket noted that
the original canary lived in `/tmp`.
"""

from __future__ import annotations

import httpx
import pytest
from scripts.probe_model_liveness import (
    ProbeResult,
    Verdict,
    classify_status,
    configured_models,
    exit_code_for,
    probe_models,
)


class TestConfiguredModels:
    def test_the_inventory_comes_from_the_modules_that_use_them(self) -> None:
        """Retyping the list would replay the defect: two truths that drift apart.

        A model replaced in `roadmap_curate` and forgotten here would produce a
        green probe on a model nobody calls any more, while the real primary dies
        unseen.
        """
        from scripts.roadmap_curate import DEFAULT_ROADMAP_MODEL

        models = configured_models()

        assert DEFAULT_ROADMAP_MODEL in {entry.model for entry in models}

    def test_every_entry_names_where_it_is_used(self) -> None:
        """A verdict with no usage is not actionable: "which one do I replace?"."""
        for entry in configured_models():
            assert entry.used_by, f"{entry.model} ne dit pas qui l'utilise"

    def test_no_consumer_of_a_shared_model_is_invisible(self) -> None:
        """`extract` and `domain_backfill` share ONE constant.

        It is therefore listed once — a single value to replace — but its entry must
        name BOTH consumers. Otherwise an operator reading "domain_backfill" believes
        they are only breaking a backfill by changing the value, when they are also
        moving the night's EXTRACT phase.
        """
        entries = configured_models()
        shared = [e for e in entries if "extract" in e.used_by]

        assert shared, "le modèle d'extract a disparu de l'inventaire"
        assert any("backfill" in e.used_by for e in shared), (
            "l'entrée d'extract ne dit pas qu'elle sert aussi au backfill"
        )

    def test_the_extract_fallback_is_probed_as_its_own_site(self) -> None:
        """A DORMANT link dies without a signal: the night only probes what it exercises.

        Extract's fallback is only called when the primary falls. As long as it was
        EQUAL to the primary (the 2026-08-21 promotion), the probe covered it by
        coincidence; as soon as it diverges, it becomes invisible again — exactly the
        failure mode measured on the night of 2026-08-28, where the roadmap
        fallback's 410 was only seen mid-night. The inventory must therefore name the
        SITE `ticket_extract.DEFAULT_EXTRACT_FALLBACK_MODEL`, not hope that its value
        coincides with another entry's.
        """
        from scripts.ticket_extract import DEFAULT_EXTRACT_FALLBACK_MODEL

        sites = [e for e in configured_models() if "DEFAULT_EXTRACT_FALLBACK_MODEL" in e.used_by]

        assert sites, "le secours d'extract n'a pas d'entrée propre dans l'inventaire"
        assert [e.model for e in sites] == [DEFAULT_EXTRACT_FALLBACK_MODEL]


class TestEnvPrecedence:
    """The probe must resolve the way the SITES resolve: env included.

    Its systemd unit loads nvidia.env (EnvironmentFile) — exactly the file where
    the overrides live. A probe reading the constants alone would return a green on
    a model nobody calls any more, while the override actually served dies unseen —
    the inventory's founding defect, reintroduced through the env door.
    """

    def test_a_roadmap_fallback_override_is_probed_instead_of_the_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL", "vendor/override-fb")

        sites = [e for e in configured_models() if "DEFAULT_ROADMAP_FALLBACK_MODEL" in e.used_by]

        assert [e.model for e in sites] == ["vendor/override-fb"]
        assert "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL" in sites[0].used_by, (
            "le verdict doit dire que c'est l'ENV qu'on remplace, pas la constante"
        )

    def test_an_extract_override_is_probed_instead_of_the_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRAIN_NVIDIA_MODEL", "vendor/override-extract")

        sites = [e for e in configured_models() if "domain_backfill.DEFAULT_MODEL" in e.used_by]

        assert [e.model for e in sites] == ["vendor/override-extract"]

    def test_without_env_the_constants_hold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "BRAIN_NVIDIA_MODEL",
            "BRAIN_NVIDIA_FALLBACK_MODEL",
            "BRAIN_NVIDIA_ROADMAP_MODEL",
            "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL",
        ):
            monkeypatch.delenv(var, raising=False)
        from scripts.roadmap_curate import DEFAULT_ROADMAP_MODEL

        models = {entry.model for entry in configured_models()}

        assert DEFAULT_ROADMAP_MODEL in models


class TestExitCode:
    """OTHER is not a green: "I do not know" must be visible.

    Measured on 2026-08-29: gpt-oss-120b — the dormant WET link, the very one the
    weekly probe exists to watch — answers in 75 s from a cold queue, beyond the
    probe timeout. An OTHER exiting 0 would make the unit green every Monday on the
    one site it structurally cannot measure. GONE keeps its code (1) and dominates
    it: a DEAD model is more urgent than an unreadable one.
    """

    @staticmethod
    def _result(verdict: Verdict) -> ProbeResult:
        entries = configured_models()
        return ProbeResult(entries[0], None if verdict is Verdict.OTHER else 200, verdict)

    def test_all_alive_is_zero(self) -> None:
        assert exit_code_for([self._result(Verdict.ALIVE)]) == 0

    def test_busy_is_transient_and_stays_zero(self) -> None:
        assert exit_code_for([self._result(Verdict.BUSY)]) == 0

    def test_gone_is_one(self) -> None:
        assert exit_code_for([self._result(Verdict.GONE)]) == 1

    def test_other_is_a_distinct_failure(self) -> None:
        assert exit_code_for([self._result(Verdict.OTHER)]) == 3

    def test_gone_dominates_other(self) -> None:
        results = [self._result(Verdict.OTHER), self._result(Verdict.GONE)]
        assert exit_code_for(results) == 1


class TestClassify:
    def test_410_is_gone_and_never_transient(self) -> None:
        """This is the distinction the probe is worth: no retry repairs an EOL."""
        assert classify_status(410) is Verdict.GONE

    def test_200_is_alive(self) -> None:
        assert classify_status(200) is Verdict.ALIVE

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
    def test_overload_is_busy_not_gone(self, status: int) -> None:
        """529 included: it was missing from RETRYABLE_STATUS and sent a whole night
        onto the fallback (commit 0eda7e18). Confusing it with an EOL would have a
        perfectly alive model replaced."""
        assert classify_status(status) is Verdict.BUSY

    def test_an_unknown_status_is_never_silently_alive(self) -> None:
        """Fail-closed: a misread 401 would lead to concluding "every model is dead"."""
        assert classify_status(401) is Verdict.OTHER


class TestProbe:
    @staticmethod
    def _client(statuses: dict[str, int]) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            model = json.loads(request.content)["model"]
            return httpx.Response(statuses[model], json={})

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_a_dead_model_is_named_with_its_use_site(self) -> None:
        entries = configured_models()
        statuses = {entry.model: 200 for entry in entries}
        dead = entries[0].model
        statuses[dead] = 410

        results = probe_models(entries, client=self._client(statuses), api_key="k")

        gone = [r for r in results if r.verdict is Verdict.GONE]
        # A name shared by several constants returns ONE line PER SITE: it is the
        # site that says which constant to replace, not the name (since 2026-08-29,
        # mistral-nemotron is both the roadmap DRY primary and extract's fallback).
        assert [r.entry.model for r in gone] == [e.model for e in entries if e.model == dead]
        assert all(r.entry.used_by for r in gone)

    def test_the_probe_never_writes_anything(self) -> None:
        """Read-only: the original canary persisted nothing, nor does this one.

        A minimal `max_tokens` and no persistence — a probe writing to the database
        would make the verification a side effect.
        """
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        probe_models(configured_models(), client=client, api_key="k")

        assert seen, "aucune requête émise"
        for body in seen:
            assert body["max_tokens"] <= 8, "la sonde consomme plus que nécessaire"

    def test_the_api_key_never_reaches_the_result(self) -> None:
        """A printed or logged verdict must not carry the secret."""
        secret = "nvapi-SENTINEL-DO-NOT-LEAK"
        statuses = {entry.model: 200 for entry in configured_models()}

        results = probe_models(configured_models(), client=self._client(statuses), api_key=secret)

        assert secret not in repr(results)
