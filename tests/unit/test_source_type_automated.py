"""Test that SourceType includes 'automated' for dream agent reports."""

from brain_v42.models.learning import LearningCreate


def test_source_type_automated_is_valid():
    """Dream agent reports use source_type='automated'."""
    lr = LearningCreate(
        topic="Dream Scan — 2026-04-05",
        insight="test",
        source_type="automated",
        tags=["dream:scan"],
    )
    assert lr.source_type == "automated"
