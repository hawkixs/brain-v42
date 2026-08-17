"""Unit tests for format_graph_path formatter.

TDD: Written BEFORE implementation — all tests must fail RED first.
"""

from __future__ import annotations

from brain_v42.mcp.tools.formatters import format_graph_path


class TestFormatGraphPath:
    def test_empty_path_returns_placeholder(self) -> None:
        """format_graph_path([]) returns '(empty path)'."""
        assert format_graph_path([]) == "(empty path)"

    def test_single_node_path(self) -> None:
        """Single-node path renders '[Type] Label' with no arrow."""
        result = format_graph_path([{"type": "Decision", "label": "X"}])
        assert result == "[Decision] X"

    def test_multi_node_path_interleaves_rels(self) -> None:
        """3-node path with 2 rel_to_next values renders correctly."""
        path = [
            {"type": "Decision", "label": "A", "rel_to_next": "RELATED_TO"},
            {"type": "Learning", "label": "B", "rel_to_next": "USES"},
            {"type": "Snippet", "label": "C"},
        ]
        result = format_graph_path(path)
        assert result == "[Decision] A --RELATED_TO--> [Learning] B --USES--> [Snippet] C"

    def test_missing_label_falls_back_to_untitled(self) -> None:
        """Node without 'label' key renders as 'untitled'."""
        result = format_graph_path([{"type": "Decision"}])
        assert result == "[Decision] untitled"
