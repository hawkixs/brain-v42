"""State documents: atomic publication, write-once creation, unknown on doubt (spec 0.5.0 §3.8.1)."""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import pytest

from headless_agents.state import Unknown, create_once, ensure_dir, publish, read, read_optional


def test_publish_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    publish(path, {"run_id": "r", "n": 1})
    assert read(path) == {"run_id": "r", "n": 1}


def test_publish_replaces_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    publish(path, {"v": 1})
    publish(path, {"v": 2})
    assert read(path) == {"v": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doc.json"]


def test_a_published_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    publish(path, {"v": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_reader_never_sees_a_partial_document(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    publish(path, {"v": 0, "pad": "x" * 50_000})
    stop = threading.Event()
    errors: list[str] = []

    def writer() -> None:
        for index in range(200):
            publish(path, {"v": index, "pad": "x" * 50_000})
        stop.set()

    thread = threading.Thread(target=writer)
    thread.start()
    while not stop.is_set():
        try:
            json.loads(path.read_text())
        except ValueError as exc:
            errors.append(str(exc))
    thread.join()
    assert errors == []


def test_create_once_refuses_a_second_write(tmp_path: Path) -> None:
    path = tmp_path / "once.json"
    create_once(path, {"a": 1})
    with pytest.raises(FileExistsError):
        create_once(path, {"a": 2})
    assert read(path) == {"a": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", ["", "{", "[]", "3", '"x"'])
def test_an_unparsable_or_non_object_document_is_unknown(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(Unknown, match="bad.json"):
        read(path)


def test_a_document_nested_too_deep_to_parse_is_unknown(tmp_path: Path) -> None:
    """Codex review of lot 2 PR B (round 2), the same input class: json.loads raises
    RecursionError on a document nested too deep, not ValueError -- it escaped read()
    and crashed ha runs for every run instead of reading one entry unknown."""
    path = tmp_path / "deep.json"
    path.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(Unknown, match="does not parse"):
        read(path)


def test_a_missing_document_is_unknown_to_read(tmp_path: Path) -> None:
    with pytest.raises(Unknown, match="missing"):
        read(tmp_path / "absent.json")


def test_a_document_naming_another_id_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "20260925T000000-aaaaaaaa.json"
    publish(path, {"run_id": "20260925T000000-bbbbbbbb"})
    with pytest.raises(Unknown, match="bbbbbbbb"):
        read(path, expect_id=("run_id", "20260925T000000-aaaaaaaa"))


def test_read_optional_is_none_only_when_absent(tmp_path: Path) -> None:
    assert read_optional(tmp_path / "absent.json") is None
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(Unknown):
        read_optional(tmp_path / "bad.json")


def test_ensure_dir_is_private(tmp_path: Path) -> None:
    directory = ensure_dir(tmp_path / "a" / "b")
    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
