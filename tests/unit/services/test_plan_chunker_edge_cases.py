"""Edge case tests for plan_chunker.chunk_markdown()."""

from brain_v42.services.plan_chunker import chunk_markdown


def test_ignores_h2_inside_fenced_code_block():
    content = """# Doc

```python
## This is a comment, not a header
x = 1
```

## Real Section

Content.
"""
    parent, chunks = chunk_markdown(content)
    assert len(chunks) == 1
    assert chunks[0].section_title == "Real Section"


def test_extracts_frontmatter_title_status_summary_tags():
    content = """---
title: From Frontmatter
status: draft
summary: A short abstract.
tags: [alpha, beta]
---

# Ignored H1 Title

## Section

Body.
"""
    parent, chunks = chunk_markdown(content)
    assert parent.title == "From Frontmatter"
    assert parent.status == "draft"
    assert parent.summary == "A short abstract."
    assert parent.tags == ["alpha", "beta"]
    # Stored content has frontmatter stripped
    assert "---" not in parent.content
    assert "title: From Frontmatter" not in parent.content


def test_completed_frontmatter_status_is_canonicalized_to_archived():
    content = """---
title: Finished Plan
status: completed
---

# Finished Plan
"""

    parent, _chunks = chunk_markdown(content)

    assert parent.status == "archived"


def test_planned_frontmatter_status_is_canonicalized_to_active():
    content = """---
title: Future Work
status: planned
---

# Future Work
"""

    parent, _chunks = chunk_markdown(content)

    assert parent.status == "active"


def test_no_h2_produces_empty_chunks_with_full_preamble():
    content = """# Only a title

Just a paragraph, no H2.
"""
    parent, chunks = chunk_markdown(content)
    assert chunks == []
    assert "Just a paragraph" in parent.preamble


def test_tiny_chunk_merged_with_next():
    content = """# Doc

## Tiny

word

## Normal

""" + ("word " * 60)
    parent, chunks = chunk_markdown(content)
    # 4 words in "Tiny" < 50 threshold -> merged into next
    assert len(chunks) == 1
    assert chunks[0].section_title == "Normal"
    assert "Tiny" in chunks[0].content  # merged header line kept
    assert "word " * 60 in chunks[0].content


def test_oversized_chunk_stored_with_warning(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="brain_v42.services.plan_chunker")
    big_body = "word " * 1600
    content = f"""# Doc

## Huge

{big_body}
"""
    parent, chunks = chunk_markdown(content)
    assert len(chunks) == 1
    assert chunks[0].word_count > 1500
    assert any("oversized" in rec.message.lower() for rec in caplog.records)


def test_h3_contributes_to_section_path():
    content = """# Doc

## Parent

### Child One

Body of child one.

### Child Two

Body of child two.
"""
    parent, chunks = chunk_markdown(content)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.section_title == "Parent"
    assert chunk.section_path == "Parent > Child One > Child Two"
    assert "Child Two" in chunk.content


def test_empty_content_yields_empty_parent():
    parent, chunks = chunk_markdown("")
    assert parent.title == ""
    assert chunks == []


def test_crlf_line_endings_handled():
    content = "# Doc\r\n\r\n## Section\r\n\r\nBody.\r\n"
    parent, chunks = chunk_markdown(content)
    assert parent.title == "Doc"
    assert len(chunks) == 1
    assert chunks[0].section_title == "Section"


def test_fenced_code_before_h2_preserves_section_content():
    """Regression: scrubbing must preserve byte offsets so H2 after a fence
    cuts the correct slice from the original content."""
    content = (
        """# Doc

```python
def foo():
    ## not a header
    return 1
```

## Real Section

Real section body with enough words to pass the threshold. """
        + ("word " * 60)
        + """
"""
    )
    parent, chunks = chunk_markdown(content)
    assert len(chunks) == 1
    assert chunks[0].section_title == "Real Section"
    assert "Real section body" in chunks[0].content
    # The code block content must NOT leak into the chunk
    assert "def foo():" not in chunks[0].content
