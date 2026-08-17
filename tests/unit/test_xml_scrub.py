"""Unit tests for xml_scrub — strip stranded XML tool-call fragments from entity text.

Historical corruption documented in learning 4575ae14 (2026-04-21): some
brain_learn / brain_log_decision calls serialized their own Claude XML
tool-call params into the stored content field. Visible signature:
`</insight>\\n<parameter name="...">...` trailing the legitimate text
(real newline + real XML tag). Must NOT match meta-references where the
same sequence appears with escaped backslashes (e.g. `</insight>\\\\n`).
"""

from __future__ import annotations

from brain_v42.maintenance.xml_scrub import scrub_xml_tool_call_leak


class TestScrubXmlToolCallLeak:
    def test_clean_text_untouched(self) -> None:
        text = "A normal insight with no XML leak in it at all."
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == text
        assert modified is False

    def test_strips_trailing_insight_leak(self) -> None:
        # Real newline + real <parameter name= → corrupted leak
        text = 'Legit content ends here.</insight>\n<parameter name="source">leaked'
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == "Legit content ends here."
        assert modified is True

    def test_strips_multiline_trailing_leak(self) -> None:
        text = (
            "First sentence.\nSecond sentence.\nThird sentence.</insight>\n"
            '<parameter name="project_key">red-shrik\n<parameter name="tags">[x,y]'
        )
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == "First sentence.\nSecond sentence.\nThird sentence."
        assert modified is True

    def test_preserves_meta_reference_with_escaped_backslash(self) -> None:
        # Meta-discussion mentions the pattern with ESCAPED newline (literal \n,
        # 2 chars: backslash + n). Must NOT trigger the scrub.
        text = (
            "The corruption pattern is: insights end with </insight>"
            + r"\n"
            + '<parameter name="project_key">red-shrik — that signature.'
        )
        # Sanity check: the test setup produced literal backslash-n, not newline.
        assert "\n<parameter" not in text, "test setup bug — accidentally got real newline"
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == text
        assert modified is False

    def test_strips_decision_leak_via_closing_parameter(self) -> None:
        # Decision corruption pattern: reasoning body ends mid-<parameter>,
        # closes with </parameter>, then another stranded <parameter name= follows.
        text = (
            "Reasoning ends with collision-freeness.</parameter>\n"
            '<parameter name="alternatives">["opt1", "opt2"]'
        )
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == "Reasoning ends with collision-freeness."
        assert modified is True

    def test_strips_closing_topic_leak(self) -> None:
        # Defensive: any of the brain_learn / brain_log_decision field tags
        # serialized as closing then followed by a leaked parameter.
        text = 'Topic body.</topic>\n<parameter name="insight">leaked insight'
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == "Topic body."
        assert modified is True

    def test_does_not_match_tag_without_following_parameter(self) -> None:
        # A stray </insight> by itself (no following <parameter name=) must
        # NOT be stripped — could be legit content mentioning the word.
        text = "Talked about </insight> tags as a concept in the middle of body."
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == text
        assert modified is False

    def test_idempotent(self) -> None:
        text = 'Legit.</insight>\n<parameter name="source">x'
        once, _ = scrub_xml_tool_call_leak(text)
        twice, modified2 = scrub_xml_tool_call_leak(once)
        assert once == twice
        assert modified2 is False

    def test_preserves_whitespace_before_marker(self) -> None:
        # Real leak signature often has trailing punctuation before </insight>.
        # Preserve exactly, don't strip a trailing period or space.
        text = 'Final sentence with period.</insight>\n<parameter name="x">y'
        cleaned, _ = scrub_xml_tool_call_leak(text)
        assert cleaned.endswith(".")
        assert cleaned == "Final sentence with period."

    def test_strips_leak_closed_by_any_parameter_name_tag(self) -> None:
        # Empirical 2026-04-22: the closing tag can be ANY brain_learn /
        # brain_log_decision parameter name, not just `insight`. Observed in
        # decisions.consequences: `</consequences>\n<parameter name="tags">[...]`.
        # Regex must cover arbitrary lowercase parameter-name tags, not a
        # hardcoded whitelist.
        text = 'Consequence body.</consequences>\n<parameter name="tags">["x","y"]'
        cleaned, modified = scrub_xml_tool_call_leak(text)
        assert cleaned == "Consequence body."
        assert modified is True

        text2 = 'Alt body.</alternatives>\n<parameter name="project_key">foo'
        cleaned2, modified2 = scrub_xml_tool_call_leak(text2)
        assert cleaned2 == "Alt body."
        assert modified2 is True

    def test_strips_invoke_and_function_calls_close_tags(self) -> None:
        # Empirical 2026-04-22 (project_contexts.current_focus): the upstream
        # Claude Code MCP client can also leave a trailing `</invoke>` or
        # `</function_calls>` after the close of the last parameter, without
        # another <parameter> tag following. Scrub must catch these too.
        text1 = "Focus body.</current_focus>\n</invoke>"
        cleaned1, modified1 = scrub_xml_tool_call_leak(text1)
        assert cleaned1 == "Focus body."
        assert modified1 is True

        text2 = "Body.</tags>\n</function_calls>"
        cleaned2, modified2 = scrub_xml_tool_call_leak(text2)
        assert cleaned2 == "Body."
        assert modified2 is True

        text3 = "Body.</insight>\n<function_calls>"
        cleaned3, modified3 = scrub_xml_tool_call_leak(text3)
        assert cleaned3 == "Body."
        assert modified3 is True
