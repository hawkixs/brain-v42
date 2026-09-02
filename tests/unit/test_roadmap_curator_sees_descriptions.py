"""The curator must see a feature's description, not only its title.

Ticket `e9b2faf4`, defect 3 — the root cause of the 14/14 rejection of
2026-09-02. `FeatureCard` carried `id, name, status, pinned, artifacts`, so the
model was shown a title and asked to curate. On a corpus where most features are
auto-created with `description == name`, the only move left to it is rewriting
the title, which is exactly what the review rejected fourteen times.

WHAT THIS FIXES, MEASURED, AND WHAT IT DOES NOT. Read-only against production on
2026-09-02: **422 live features, 359 with `description == name`, 63 carrying a
description that adds something** (brain-v42: 47 live, 31 identical). So this
change gives the curator real material on 15 % of the corpus and changes nothing
for the other 85 % — it removes a cause, it is not by itself the cure for "the
phase fabricates stock to reject". Said here so that a green suite is not read as
a solved problem.

THE DESCRIPTION IS RENDERED ONLY WHEN IT ADDS SOMETHING. Emitting it on the 359
features where it equals the name would spend prompt budget repeating a line the
model already has — on a path with a documented truncation history (the brain-v42
batch cut at 4096 tokens on the first wet run, char 12160). Skipping the
duplicate is both cheaper and more honest: the absence of a description line now
MEANS "this feature says nothing beyond its title", which is itself the signal a
curator needs.
"""

from __future__ import annotations

from uuid import uuid4

from scripts.roadmap_curate import FeatureCard, ProjectBatch, build_messages, render_batch


def _card(name: str, description: str | None, **kw) -> FeatureCard:
    return FeatureCard(
        id=uuid4(),
        name=name,
        status="research",
        pinned=False,
        description=description,
        **kw,
    )


def _batch(*cards: FeatureCard) -> ProjectBatch:
    return ProjectBatch(project_key="integ-desc", features=list(cards))


class TestTheDescriptionReachesThePrompt:
    def test_a_description_that_differs_from_the_name_is_rendered(self) -> None:
        """THE test of this defect: the material the curator was missing.

        Without it the model sees `nom: Hybrid search` and nothing else, and the
        only op it can justify is `rename`.
        """
        card = _card("Hybrid search", "RRF over FTS and pgvector, k=60, no reranker yet.")

        text = render_batch(_batch(card))

        assert "RRF over FTS and pgvector" in text

    def test_a_description_equal_to_the_name_is_not_repeated(self) -> None:
        """359 of 422 live features are in this case: repeating the title costs budget.

        The absence of the line is the signal — this feature says nothing beyond
        its title — and it keeps the batch inside the completion cap that has
        already truncated a brain-v42 run once.
        """
        text = render_batch(_batch(_card("Hybrid search", "Hybrid search")))

        assert text.count("Hybrid search") == 1

    def test_a_missing_description_renders_nothing(self) -> None:
        """`None` must not print `None` into the prompt as if it were content."""
        text = render_batch(_batch(_card("Hybrid search", None)))

        assert "None" not in text

    def test_the_description_travels_all_the_way_into_the_user_message(self) -> None:
        """Negative witness against a render that nobody sends.

        `render_batch` feeding a message the model never receives would pass every
        assertion above while leaving the curator exactly as blind.
        """
        card = _card("Hybrid search", "RRF over FTS and pgvector, k=60.")

        messages = build_messages(_batch(card))

        assert "RRF over FTS and pgvector" in messages[1]["content"]


class TestTheCardStaysCheapToBuild:
    def test_description_is_optional_so_existing_callers_keep_working(self) -> None:
        """The shrink rebuilds cards; a required field would break it at the worst moment."""
        card = FeatureCard(id=uuid4(), name="n", status="research", pinned=False)

        assert card.description is None

    def test_compacting_a_batch_preserves_the_description(self) -> None:
        """The shrink is what runs under a tight NVIDIA window — it must not drop it.

        `_compact_batch` rebuilds every card field by field: a field forgotten
        there is silently lost on exactly the retries where the curator is already
        working with less.
        """
        from scripts.roadmap_curate import _compact_batch

        card = _card("Hybrid search", "RRF over FTS and pgvector, k=60.")

        compacted = _compact_batch(_batch(card), feature_cap=10, artifact_cap=10)

        assert compacted.features[0].description == card.description


class TestTheDescriptionIsBounded:
    """A 30-card brain-v42 batch would carry 29 469 bytes of descriptions.

    Measured read-only on 2026-09-02 against 2 733 bytes of names — an 11× feature
    section, roughly 7 400 tokens of input, with a single description reaching
    3 543 bytes across all projects. Sending them whole would break the path this
    change exists to improve: that same batch was truncated once already, at 4 096
    tokens on the first wet run of 2026-07-04.
    """

    def test_a_long_description_is_clipped(self) -> None:
        from scripts.roadmap_curate import MAX_DESCRIPTION_CHARS

        text = render_batch(_batch(_card("F", "x" * 3000)))

        assert "x" * MAX_DESCRIPTION_CHARS in text
        assert "x" * (MAX_DESCRIPTION_CHARS + 1) not in text

    def test_the_clip_announces_itself(self) -> None:
        """A silently cut description reads as a description that stops there.

        Same rule as every other bound in this module: the drop is logged, never
        silent.
        """
        text = render_batch(_batch(_card("F", "x" * 3000)))

        assert "tronquée" in text

    def test_a_short_description_is_untouched_and_unannounced(self) -> None:
        """Negative witness: without it, clipping everything would pass the two above."""
        text = render_batch(_batch(_card("F", "Short but real.")))

        assert "Short but real." in text
        assert "tronquée" not in text

    def test_multiline_prose_is_flattened(self) -> None:
        """These descriptions are multi-paragraph; raw newlines would break the card layout.

        `render_batch` builds one line per field, so an embedded newline would make
        a description look like the start of the next feature.
        """
        text = render_batch(_batch(_card("F", "First line.\n\nSecond paragraph.")))

        assert "First line. Second paragraph." in text
