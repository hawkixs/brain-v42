"""Unit tests for plan_chunker.chunk_markdown()."""

from brain_v42.services.plan_chunker import chunk_markdown


def test_chunk_markdown_splits_on_h2():
    content = """# Main Title

Intro paragraph.

## Section A

Content of A.

## Section B

Content of B.
"""
    parent, chunks = chunk_markdown(content)

    assert parent.title == "Main Title"
    assert parent.preamble.strip() == "Intro paragraph."
    assert parent.content == content
    assert parent.word_count > 0

    assert len(chunks) == 2
    assert chunks[0].section_title == "Section A"
    assert chunks[0].section_order == 0
    assert "Content of A." in chunks[0].content
    assert chunks[1].section_title == "Section B"
    assert chunks[1].section_order == 1
    assert "Content of B." in chunks[1].content
