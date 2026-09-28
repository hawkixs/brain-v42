"""``ha run --output-schema FILE``, parsed and read before anything runs (0.5.3 lot 1).

Parser-level only: these hold as root, where a full ``cli.main`` run is not the place to
check a flag (the root VM's baseline).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from headless_agents import cli
from headless_agents.engine import UsageError
from headless_agents.structured import MAX_SCHEMA_BYTES


def test_the_run_parser_takes_output_schema() -> None:
    # The prompt before the option: Python 3.12.3's argparse (the root VM's) refuses a
    # positional after an option taking a value here; 3.12.11 accepts both orders.
    args = cli._parser().parse_args(["run", "codex", "x", "--output-schema", "s.json"])
    assert args.output_schema == Path("s.json") and args.prompt == "x"


def test_output_schema_is_absent_by_default() -> None:
    assert cli._parser().parse_args(["run", "codex", "x"]).output_schema is None


def test_read_output_schema_reads_a_json_object_relative_to_the_cwd(tmp_path: Path) -> None:
    (tmp_path / "s.json").write_text('{"type": "object", "properties": {}}\n', encoding="utf-8")
    schema = cli._read_output_schema(Path("s.json"), cwd=tmp_path)
    assert schema == {"type": "object", "properties": {}}


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        (None, "cannot be read"),
        (b"[1]", "not a JSON object"),
        (b"not json", "not JSON"),
        (b'{"type": "object", "x": NaN}', "not JSON"),
        (b"\xff\xfe", "not JSON"),
        (b"{" * 5000 + b"}" * 5000, "not JSON"),
        (b" " * (MAX_SCHEMA_BYTES + 1), f"larger than {MAX_SCHEMA_BYTES} bytes"),
    ],
    ids=["missing", "array", "garbage", "nan", "not-utf8", "too-deep", "oversized"],
)
def test_read_output_schema_refuses(tmp_path: Path, content: bytes | None, rule: str) -> None:
    path = tmp_path / "s.json"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(UsageError, match=rf"^--output-schema {re.escape(str(path))}: {rule}"):
        cli._read_output_schema(path, cwd=tmp_path)
