"""Contracts for deterministic test file sharding."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import ci_shard
from tests.ci_shard import partition


def test_partition_assigns_every_file_exactly_once() -> None:
    files = [f"tests/unit/test_{index}.py" for index in range(17)]
    for weights in ({}, {files[i]: float(i + 1) for i in range(10)}):
        for shards in (1, 2, 4, 7):
            result = partition(files, weights, shards)
            assert set(result) == set(files)
            assert all(1 <= shard <= shards for shard in result.values())
            groups = [
                {path for path in files if result[path] == shard} for shard in range(1, shards + 1)
            ]
            assert set.union(*groups) == set(files)
            assert all(
                not groups[i] & groups[j] for i in range(shards) for j in range(i + 1, shards)
            )


def test_partition_is_deterministic_and_balances_lpt_case() -> None:
    files = [f"f{i}" for i in range(6)]
    weights = dict(zip(files, [8.0, 7.0, 6.0, 5.0, 4.0, 3.0], strict=True))
    first = partition(files, weights, 3)
    assert partition(files, weights, 3) == first
    loads = [sum(weights[path] for path in files if first[path] == shard) for shard in (1, 2, 3)]
    assert max(loads) - min(loads) <= 2


def test_unplanned_files_use_plan_median() -> None:
    result = partition(["known-a", "known-b", "new"], {"known-a": 10, "known-b": 20}, 2)
    assert result["new"] == 2


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[object] = []

    def pytest_deselected(self, items: list[object]) -> None:
        self.deselected.extend(items)


def _config(spec: str | None, plan: str | None = None) -> SimpleNamespace:
    options = {"--shard": spec, "--shard-plan": plan}
    return SimpleNamespace(getoption=lambda name: options[name], rootpath=Path.cwd(), hook=_Hook())


class _Item:
    """Hashable like a real pytest item, which the plugin uses as a dict key."""

    def __init__(self, path: Path, nodeid: str) -> None:
        self.path = path
        self.nodeid = nodeid


def _items(files: list[str]) -> list[_Item]:
    """Two fake test items per file, in collection order."""
    return [_Item(Path.cwd() / f, f"{f}::t{n}") for f in files for n in (1, 2)]


def test_the_hook_partitions_the_real_unit_collection_by_whole_files() -> None:
    """Run the real hook over every test file of tests/unit: shards tile the suite exactly."""
    files = sorted(p.as_posix() for p in Path("tests/unit").rglob("test_*.py"))
    assert files, "no unit test file found: the check would be hollow"
    for total in (1, 2, 4, 7):
        kept_nodeids: list[str] = []
        files_per_shard: list[set[str]] = []
        for current in range(1, total + 1):
            items = _items(files)
            config = _config(f"{current}/{total}")
            ci_shard.pytest_collection_modifyitems(config, items)
            assert len(items) + len(config.hook.deselected) == 2 * len(files)
            kept_nodeids.extend(item.nodeid for item in items)
            files_per_shard.append({item.nodeid.split("::")[0] for item in items})
        assert sorted(kept_nodeids) == sorted(item.nodeid for item in _items(files))
        assert sum(len(group) for group in files_per_shard) == len(files)
        for item_files in files_per_shard:
            assert all(sum(f in group for group in files_per_shard) == 1 for f in item_files)


def test_without_the_option_the_hook_changes_nothing() -> None:
    items = _items(["tests/unit/a.py", "tests/unit/b.py"])
    config = _config(None)
    ci_shard.pytest_collection_modifyitems(config, items)
    assert len(items) == 4
    assert config.hook.deselected == []


@pytest.mark.parametrize("spec", ["0/4", "5/4", "1/0", "a/b", "3", "1/2/3", "-1/4"])
def test_a_malformed_shard_option_is_a_usage_error(spec: str) -> None:
    with pytest.raises(pytest.UsageError):
        ci_shard.pytest_collection_modifyitems(_config(spec), _items(["tests/unit/a.py"]))
