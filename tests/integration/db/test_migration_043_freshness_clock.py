"""Migration 043 — dater le STATUT de fraîcheur, ce qu'`updated_at` ne peut pas faire.

Spec `2026-08-08-dream-v2-design.md` §4.3 et §6.2. C'est le **préalable dur de
la purge**, pas une préférence d'ordonnancement.

Le critère de suppression existe déjà dans le dépôt — `decay_tools.py`, affiché
à SCAN toutes les nuits — et il est faux sur ses deux termes :

- `access_count = 0` est le compteur TOTAL : un artefact relu par le seul dream
  sort du critère et devient indéfiniment non-purgeable ;
- `updated_at < cutoff` REDÉMARRE à chaque écriture du flusher de compteurs,
  parce que `trg_<table>_updated` est présent. Il n'existe donc aujourd'hui
  **aucune horloge honnête** pour mesurer un séjour en archive.

D'où la colonne. Sans backfill : `NULL` veut dire « jamais mesuré », jamais
« archivé depuis toujours » — la distinction décide qui serait supprimé.

MÉCANISME 041, PAS 040, et la spec explique pourquoi : `focus_updated_at` est
écrite en code applicatif parce que le focus n'a QU'UN écrivain ;
`freshness_status` en a quatre, dont le jugement REORG qui passe par le tool
générique `brain_update`, lequel ne sait rien du decay. Stamper en applicatif
obligerait à le faire dans `brain_update` lui-même, pour une colonne que 99 % de
ses appels ne touchent pas. C'est donc un trigger conditionnel.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

_DECAY_TABLES = (
    "learnings",
    "decisions",
    "snippets",
    "runbooks",
    "adrs",
    "indexed_plans",
)
_SOURCES = ("merge", "judgment", "score", "revive")


class TestColumnShape:
    @pytest.mark.parametrize("table", _DECAY_TABLES)
    async def test_the_clock_column_exists_and_is_nullable(self, table: str, db_session) -> None:
        row = (
            await db_session.execute(
                sa.text(
                    "SELECT data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'freshness_status_updated_at'"
                ),
                {"t": table},
            )
        ).one_or_none()

        assert row is not None, f"{table} n'a pas freshness_status_updated_at"
        data_type, is_nullable, default = row
        assert data_type == "timestamp with time zone"
        # NULL = « jamais mesuré ». Un backfill à now() ferait croire que tout
        # le corpus vient de changer de statut, et la purge compterait 180 jours
        # à partir d'une date inventée.
        assert is_nullable == "YES"
        assert default is None

    @pytest.mark.parametrize("table", _DECAY_TABLES)
    async def test_the_source_column_exists_and_is_constrained(
        self, table: str, db_session
    ) -> None:
        row = (
            await db_session.execute(
                sa.text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'freshness_source'"
                ),
                {"t": table},
            )
        ).one_or_none()

        assert row is not None, f"{table} n'a pas freshness_source"
        assert row[0] == "character varying"
        assert row[1] == "YES"

    @pytest.mark.parametrize("table", _DECAY_TABLES)
    async def test_no_backfill_happened(self, table: str, db_session) -> None:
        """Aucune ligne existante ne doit porter une date inventée.

        La migration n'écrit pas. Une ligne datée sans transition de statut
        depuis la bascule signalerait un backfill, donc une horloge qui ment.
        """
        stamped = (
            await db_session.execute(
                sa.text(
                    f"SELECT count(*) FROM {table} WHERE freshness_status_updated_at IS NOT NULL"
                )  # noqa: S608
            )
        ).scalar_one()
        total = (await db_session.execute(sa.text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608

        assert stamped < total or total == 0, (
            f"{table} : toutes les lignes sont datées — un backfill a eu lieu"
        )


class TestTriggerBehaviour:
    async def test_a_status_change_stamps_the_clock(self, db_session) -> None:
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 clock probe', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()

        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status = 'stale' WHERE id = :i"),
            {"i": row_id},
        )
        stamped = (
            await db_session.execute(
                sa.text("SELECT freshness_status_updated_at FROM learnings WHERE id = :i"),
                {"i": row_id},
            )
        ).scalar_one()

        assert stamped is not None, "le trigger n'a pas daté la transition de statut"

    async def test_writing_the_same_status_does_not_rejuvenate_the_clock(self, db_session) -> None:
        """Réécrire `archived` sur une entité déjà archivée ne la rajeunit pas.

        C'est toute la raison du `WHEN … IS DISTINCT FROM`. Sans lui, un
        traitement idempotent qui repose le même statut chaque nuit remettrait
        le compteur de séjour à zéro tous les jours — et rien ne serait jamais
        purgeable, silencieusement.

        L'ASSERTION NE COMPARE PAS DEUX HORODATAGES, et c'est délibéré :
        `CURRENT_TIMESTAMP` vaut l'heure de DÉBUT DE TRANSACTION en PostgreSQL,
        donc deux écritures dans la même transaction produisent la même valeur
        et un `first == second` passerait à vide, trigger armé ou non. On pose
        une sentinelle datée d'un an et on vérifie qu'elle SURVIT.
        """
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 idempotence probe', 'probe', 'brain-v42', 'archived') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        sentinel = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status_updated_at = :s WHERE id = :i"),
            {"i": row_id, "s": sentinel},
        )

        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status = 'archived' WHERE id = :i"),
            {"i": row_id},
        )
        stamped = (
            await db_session.execute(
                sa.text("SELECT freshness_status_updated_at FROM learnings WHERE id = :i"),
                {"i": row_id},
            )
        ).scalar_one()

        assert stamped.year == 2025, (
            "réécrire le même statut a rajeuni l'horloge : le prédicat "
            "`WHEN OLD.freshness_status IS DISTINCT FROM NEW.freshness_status` "
            f"ne filtre pas (valeur observée : {stamped})"
        )

    async def test_a_counter_write_does_not_touch_the_clock(self, db_session) -> None:
        """LE défaut que la colonne répare, prouvé sans comparer de dates.

        `updated_at` bouge à chaque écriture du flusher de compteurs, donc
        l'horloge de 180 jours redémarrait sur un simple accès. La preuve exacte
        que la nouvelle colonne ne fait pas ça : elle vaut NULL à l'insertion,
        et une écriture de compteur ne doit pas la faire sortir de NULL. Aucun
        horodatage n'est comparé, donc rien ne peut passer à vide.
        """
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 counter probe', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()

        await db_session.execute(
            sa.text("UPDATE learnings SET access_count = access_count + 1 WHERE id = :i"),
            {"i": row_id},
        )
        clock, updated = (
            await db_session.execute(
                sa.text(
                    "SELECT freshness_status_updated_at, updated_at FROM learnings WHERE id = :i"
                ),
                {"i": row_id},
            )
        ).one()

        assert clock is None, (
            "une écriture de compteur a daté l'horloge de statut — le trigger "
            "n'est pas restreint à `UPDATE OF freshness_status`"
        )
        # Garde du harnais : la ligne a bien été touchée, sinon l'assertion
        # ci-dessus serait verte sur une écriture qui n'a jamais eu lieu.
        assert updated is not None

    async def test_a_stale_source_never_survives_a_transition(self, db_session) -> None:
        """Un écrivain qui ne déclare pas sa source ne doit pas hériter de l'ancienne.

        Sans cette remise à NULL, `freshness_source` mentirait : elle
        décrirait la transition PRÉCÉDENTE, avec la date de la nouvelle. Une
        provenance fausse est pire qu'une provenance absente — la seconde se
        voit, la première se croit.
        """
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 source probe', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()
        await db_session.execute(
            sa.text(
                "UPDATE learnings SET freshness_status = 'archived', "
                "freshness_source = 'merge' WHERE id = :i"
            ),
            {"i": row_id},
        )
        await db_session.execute(
            sa.text("UPDATE learnings SET freshness_status = 'fresh' WHERE id = :i"),
            {"i": row_id},
        )
        source = (
            await db_session.execute(
                sa.text("SELECT freshness_source FROM learnings WHERE id = :i"), {"i": row_id}
            )
        ).scalar_one()

        assert source is None, (
            f"la source 'merge' a survécu à une transition non déclarée: {source}"
        )

    @pytest.mark.parametrize("source", _SOURCES)
    async def test_every_declared_source_is_accepted(self, source: str, db_session) -> None:
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES (:t, 'probe', 'brain-v42', 'fresh') RETURNING id"
                ),
                {"t": f"043 source {source}"},
            )
        ).scalar_one()

        await db_session.execute(
            sa.text(
                "UPDATE learnings SET freshness_status = 'stale', freshness_source = :s "
                "WHERE id = :i"
            ),
            {"i": row_id, "s": source},
        )

        stored = (
            await db_session.execute(
                sa.text("SELECT freshness_source FROM learnings WHERE id = :i"), {"i": row_id}
            )
        ).scalar_one()
        assert stored == source

    async def test_an_unknown_source_is_refused(self, db_session) -> None:
        """La contrainte est un vocabulaire, pas une suggestion."""
        row_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO learnings (topic, insight, project_key, freshness_status) "
                    "VALUES ('043 bad source', 'probe', 'brain-v42', 'fresh') RETURNING id"
                )
            )
        ).scalar_one()

        with pytest.raises(Exception, match="freshness_source|check"):
            await db_session.execute(
                sa.text(
                    "UPDATE learnings SET freshness_status = 'stale', "
                    "freshness_source = 'invented' WHERE id = :i"
                ),
                {"i": row_id},
            )
