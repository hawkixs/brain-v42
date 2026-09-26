"""A review's records in the state directory: its check, then its result, each written once
(spec 0.5.0 §3.8.1, §3.8.4 step 6, §3.8.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from headless_agents import reviews
from headless_agents.state import Unknown
from headless_agents.vendor import AttributedCommit, VendorCheck

RUN = "20260926T120000-cccccccc"
HEAD = "a" * 40
CHECK = VendorCheck(
    commits=(AttributedCommit(sha="1" * 40, run_id=None, made_by="hand", providers=()),),
    authors=(),
    reviewers={"reviewer-codex": ("codex",)},
)


def test_a_check_is_written_once_and_read_back(tmp_path: Path) -> None:
    reviews.write_check(tmp_path, RUN, CHECK)
    assert reviews.load_check(tmp_path, RUN) == CHECK
    with pytest.raises(FileExistsError):
        reviews.write_check(tmp_path, RUN, CHECK)


def test_a_result_is_written_once_and_read_back(tmp_path: Path) -> None:
    reviews.write_result(
        tmp_path, RUN, head=HEAD, verdict="changes", text="fix\nVERDICT: CHANGES", check=CHECK
    )
    result = reviews.load_result(tmp_path, RUN)
    assert result == reviews.ReviewResult(
        run_id=RUN, head=HEAD, verdict="changes", text="fix\nVERDICT: CHANGES", check=CHECK
    )
    with pytest.raises(FileExistsError):
        reviews.write_result(tmp_path, RUN, head=HEAD, verdict="approve", text="x", check=CHECK)


def test_a_missing_result_is_unknown_never_empty(tmp_path: Path) -> None:
    with pytest.raises(Unknown, match="missing"):
        reviews.load_result(tmp_path, RUN)


def test_a_missing_check_is_none(tmp_path: Path) -> None:
    """A review refused before its check: no check was ever written."""
    assert reviews.load_check(tmp_path, RUN) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("run_id", "20260926T120000-dddddddd"),
        ("head", "not-a-sha"),
        ("verdict", "maybe"),
        ("verdict", None),
        ("text", 3),
        ("vendor_check", {"commits": "x"}),
    ],
)
def test_a_result_ha_never_writes_is_unknown(tmp_path: Path, key: str, value: object) -> None:
    reviews.write_result(tmp_path, RUN, head=HEAD, verdict="approve", text="ok", check=CHECK)
    path = reviews.result_path(tmp_path, RUN)
    document = json.loads(path.read_text())
    document[key] = value
    path.chmod(0o600)
    path.write_text(json.dumps(document))
    with pytest.raises(Unknown):
        reviews.load_result(tmp_path, RUN)


def test_the_records_live_under_reviews(tmp_path: Path) -> None:
    assert reviews.check_path(tmp_path, RUN) == tmp_path / "reviews" / f"{RUN}.check.json"
    assert reviews.result_path(tmp_path, RUN) == tmp_path / "reviews" / f"{RUN}.json"


def test_a_malformed_run_id_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="run id"):
        reviews.result_path(tmp_path, "../x")
