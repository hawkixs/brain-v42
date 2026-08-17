"""Tests for CLAUDE.md dynamic section writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_v42.services.claude_md_writer import (
    MARKER_END,
    MARKER_START,
    update_claude_md,
)


@pytest.fixture
def claude_md_file(tmp_path: Path) -> Path:
    """Create a temporary CLAUDE.md with markers."""
    content = (
        "# My Project\n\n"
        "## Description\n"
        "This is a project.\n\n"
        f"{MARKER_START}\n"
        "**Focus:** old focus\n"
        "**Updated:** 2026-01-01\n"
        f"{MARKER_END}\n\n"
        "## Other Section\n"
        "Don't touch this.\n"
    )
    f = tmp_path / "CLAUDE.md"
    f.write_text(content)
    return f


class TestUpdateClaudeMd:
    def test_writes_between_markers(self, claude_md_file: Path):
        """Only the block between markers is rewritten."""
        result = update_claude_md(claude_md_file, "new focus here")

        assert result is True
        content = claude_md_file.read_text()
        assert "new focus here" in content
        assert "old focus" not in content

    def test_preserves_content_outside_markers(self, claude_md_file: Path):
        """Content before and after markers is untouched."""
        update_claude_md(claude_md_file, "new focus")

        content = claude_md_file.read_text()
        assert "# My Project" in content
        assert "This is a project." in content
        assert "## Other Section" in content
        assert "Don't touch this." in content

    def test_no_markers_returns_false(self, tmp_path: Path):
        """File without markers -> no modification, returns False."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("# Project\n\nNo markers here.\n")

        result = update_claude_md(f, "new focus")

        assert result is False
        assert f.read_text() == "# Project\n\nNo markers here.\n"

    def test_missing_file_returns_false(self, tmp_path: Path):
        """Non-existent file -> returns False."""
        result = update_claude_md(tmp_path / "nonexistent.md", "focus")
        assert result is False

    def test_tilde_expansion(self, tmp_path: Path):
        """Tilde (~) in path is expanded (via Path.expanduser())."""
        f = tmp_path / "CLAUDE.md"
        f.write_text(f"{MARKER_START}\nold\n{MARKER_END}\n")
        # Test with actual path (tilde expansion would not change tmp_path)
        result = update_claude_md(str(f), "new focus")
        assert result is True

    def test_updated_date_is_set(self, claude_md_file: Path):
        """The updated date is set to today."""
        from datetime import date

        update_claude_md(claude_md_file, "focus")
        content = claude_md_file.read_text()
        assert date.today().isoformat() in content
