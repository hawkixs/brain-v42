"""Le signal de mutation ignore le bruit de compteur et la production du dream."""

from __future__ import annotations

from scripts.dream.dream_preflight import _mutation_sql


class TestMutationSignal:
    def test_uses_content_updated_at_only(self) -> None:
        """Aucune occurrence de `updated_at` qui ne soit `content_updated_at`."""
        sql = _mutation_sql()
        assert sql.count("content_updated_at") == 5
        assert sql.count("updated_at") == sql.count("content_updated_at")

    def test_excludes_dream_generated_entities(self) -> None:
        """Sinon SYNTH garantit que la nuit suivante synthétise sur sa propre sortie."""
        sql = _mutation_sql()
        assert sql.count("dream:generated") == 5

    def test_covers_the_five_knowledge_tables(self) -> None:
        for table in ("decisions", "learnings", "snippets", "runbooks", "adrs"):
            assert f"FROM {table}" in _mutation_sql()
