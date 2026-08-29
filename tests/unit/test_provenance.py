"""Tests de la couche de provenance — classification et contexte d'acteur."""

from __future__ import annotations

import asyncio
import os

from brain_v42.provenance import (
    UNKNOWN_ACTOR,
    enter_call,
    exit_call,
    get_current_actor,
    get_current_session,
    is_human_actor,
    is_outermost_call,
    normalize_agent,
    normalize_session,
    normalize_transport,
    set_current_actor,
    set_current_session,
)


class TestNormalizeTransport:
    """``Mcp-Session-Id`` frappé par le serveur, jamais par le client.

    Forme mesurée le 2026-08-10 sur mcp 1.28.1 :
    ``streamable_http_manager.py`` fait ``uuid4().hex`` — 32 hexadécimaux
    MINUSCULES, **sans tirets**. C'est précisément ce qui interdit de la faire
    passer par ``normalize_session`` : cette dernière n'accepte que la forme
    canonique tiretée, et la re-tirer injecterait dans l'espace des sessions
    d'agent une valeur qui n'en est pas une.
    """

    def test_minted_form_is_accepted(self) -> None:
        assert normalize_transport("0f9d2c1b3a4e5f60718293a4b5c6d7e8") == (
            "0f9d2c1b3a4e5f60718293a4b5c6d7e8"
        )

    def test_canonical_uuid_is_rejected(self) -> None:
        # La forme tirée est celle d'une SESSION D'AGENT. L'accepter ici
        # confondrait deux espaces de clés qui ne se joignent pas.
        assert normalize_transport("3d7a88d7-791b-45da-b8b9-75727e3c9eec") is None

    def test_uppercase_is_rejected(self) -> None:
        # Deux graphies de la même valeur produiraient deux lignes de panneau.
        assert normalize_transport("0F9D2C1B3A4E5F60718293A4B5C6D7E8") is None

    def test_wrong_length_is_rejected(self) -> None:
        assert normalize_transport("0f9d2c1b3a4e5f60718293a4b5c6d7e") is None
        assert normalize_transport("0f9d2c1b3a4e5f60718293a4b5c6d7e89") is None

    def test_non_hex_is_rejected(self) -> None:
        assert normalize_transport("g" * 32) is None
        assert normalize_transport("0f9d2c1b3a4e5f60718293a4b5c6d7g8") is None

    def test_blank_and_missing_are_none(self) -> None:
        assert normalize_transport("   ") is None
        assert normalize_transport("") is None
        assert normalize_transport(None) is None

    def test_oversized_input_is_rejected_without_scanning(self) -> None:
        # L'en-tête est une entrée non maîtrisée : la borne doit tenir même
        # quand l'appelant envoie un mégaoctet.
        assert normalize_transport("a" * 1_000_000) is None

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert normalize_transport("  0f9d2c1b3a4e5f60718293a4b5c6d7e8  ") == (
            "0f9d2c1b3a4e5f60718293a4b5c6d7e8"
        )


class TestNormalizeAgent:
    def test_absolute_path_reduces_to_basename(self) -> None:
        assert normalize_agent("/home/hawixs/git/red-lab") == "red-lab"

    def test_trailing_slash_is_stripped(self) -> None:
        assert normalize_agent("/home/hawixs/git/red-lab/") == "red-lab"

    def test_static_label_passes_through(self) -> None:
        assert normalize_agent("dream-codex-synth") == "dream-codex-synth"

    def test_blank_becomes_unknown(self) -> None:
        assert normalize_agent("   ") == UNKNOWN_ACTOR
        assert normalize_agent(None) == UNKNOWN_ACTOR

    def test_unexpanded_template_collapses(self) -> None:
        assert normalize_agent("${PWD}") == "_unexpanded"

    def test_bare_root_becomes_unknown(self) -> None:
        assert normalize_agent("/") == UNKNOWN_ACTOR

    def test_value_at_column_width_is_unchanged(self) -> None:
        value = "a" * 64
        assert normalize_agent(value) == value
        assert len(normalize_agent(value)) == 64

    def test_value_over_column_width_is_truncated(self) -> None:
        assert len(normalize_agent("a" * 65)) == 64
        assert normalize_agent("a" * 65) == "a" * 64

    def test_much_longer_value_is_truncated_to_column_width(self) -> None:
        value = "/home/hawixs/git/" + ("very-long-project-name-" * 10)
        result = normalize_agent(value)
        assert len(result) == 64
        assert result == os.path.basename(value.rstrip("/"))[:64]


class TestIsHumanActor:
    def test_interactive_session_is_human(self) -> None:
        """Le témoin HUMAIN exigé par 6878077f : fermer le trou sans casser le
        cas légitime — un basename de projet arbitraire reste humain."""
        assert is_human_actor("red-lab") is True
        assert is_human_actor("brain_v42") is True

    def test_measured_machine_names_are_not_human(self) -> None:
        """Recensement PAR SITE D'APPEL du 2026-08-29 (6878077f) — des rails
        machine VIVANTS hors famille `dream-` comptaient humains :

        - `red-shrik` : bot actif (`systemctl is-active` → active), fait du
          `brain_search` en se déclarant par `mcp_client.py:83` ;
        - `antigravity` : le même client, déploiement agy
          (`deploy/agy/settings.mcp.example.json`) ;
        - `red-lab-factory` : l'acteur que red-lab DOIT poser (a3fa6696) —
          pré-classé pour que le correctif cross-repo n'ouvre pas le trou
          qu'il ferme ;
        - `pc-dev-red` : client scripté du PC dev, mesuré sur
          `brain_ticket_list` (fil a3fa6696).

        Des noms EXACTS, jamais un préfixe `red-` : il avalerait les basenames
        humains (`red-games` lancé interactivement). Coût assumé, sens
        conservateur : une session interactive lancée DEPUIS le répertoire
        red-shrik déclare le même basename et comptera machine — l'erreur
        coûte en couverture humaine, jamais en fausse écriture.
        """
        for name in ("red-shrik", "antigravity", "red-lab-factory", "pc-dev-red"):
            assert is_human_actor(name) is False, name

    def test_the_sql_mirror_shares_the_same_constants(self) -> None:
        """`session_derived_capture` porte un prédicat SQL « miroir de
        provenance.is_human_actor » : deux sources de vérité qui ne divergent
        qu'à la lecture sont le mode de panne maison — le miroir doit IMPORTER
        les constantes, jamais les redéclarer."""
        from brain_v42.db import session_derived_capture as mirror
        from brain_v42.provenance import (
            SYSTEM_ACTOR_NAMES,
            SYSTEM_ACTOR_PREFIXES,
        )

        assert mirror._SYSTEM_ACTOR_PREFIXES is SYSTEM_ACTOR_PREFIXES
        assert mirror._SYSTEM_ACTOR_NAMES is SYSTEM_ACTOR_NAMES

    def test_dream_phase_is_not_human(self) -> None:
        assert is_human_actor("dream-codex-synth") is False
        assert is_human_actor("dream-codex-reorg") is False

    def test_unknown_is_not_human(self) -> None:
        """Fail-closed : un appelant non identifié ne débloque aucune promotion."""
        assert is_human_actor(UNKNOWN_ACTOR) is False
        assert is_human_actor(None) is False
        assert is_human_actor("") is False

    def test_unexpanded_is_not_human(self) -> None:
        assert is_human_actor("_unexpanded") is False


class TestCurrentActor:
    def test_default_is_unknown(self) -> None:
        """Contexte neuf : un ContextVar non posé rend sa valeur par défaut.

        `Context()` est vide — ne PAS utiliser `get_current_actor()` nu ici, un
        test voisin ayant déjà posé une valeur dans le contexte courant.
        """
        from contextvars import Context

        assert Context().run(get_current_actor) == UNKNOWN_ACTOR

    def test_set_then_get(self) -> None:
        set_current_actor("red-lab")
        assert get_current_actor() == "red-lab"

    def test_blank_set_falls_back_to_unknown(self) -> None:
        set_current_actor("")
        assert get_current_actor() == UNKNOWN_ACTOR

    async def test_value_does_not_leak_across_tasks(self) -> None:
        """Chaque requête doit voir son propre acteur, pas celui d'une voisine."""
        seen: list[str] = []

        async def worker(actor: str) -> None:
            set_current_actor(actor)
            await asyncio.sleep(0)
            seen.append(get_current_actor())

        await asyncio.gather(worker("red-lab"), worker("dream-codex-scan"))
        assert sorted(seen) == ["dream-codex-scan", "red-lab"]


class TestNormalizeSession:
    def test_canonical_uuid_passes_through(self) -> None:
        assert normalize_session("3d7a88d7-791b-45da-b8b9-75727e3c9eec") == (
            "3d7a88d7-791b-45da-b8b9-75727e3c9eec"
        )

    def test_unexpanded_template_is_rejected(self) -> None:
        assert normalize_session("${CLAUDE_CODE_SESSION_ID}") is None

    def test_non_uuid_is_rejected(self) -> None:
        assert normalize_session("brain-v42") is None

    def test_uppercase_uuid_is_rejected(self) -> None:
        # Une seule forme canonique, sinon deux clients écrivant la même
        # session sous deux casses produiraient deux lignes distinctes.
        assert normalize_session("3D7A88D7-791B-45DA-B8B9-75727E3C9EEC") is None

    def test_blank_and_none_are_rejected(self) -> None:
        assert normalize_session("   ") is None
        assert normalize_session(None) is None

    def test_overlong_value_is_rejected(self) -> None:
        assert normalize_session("x" * 4096) is None


class TestCurrentSession:
    def test_default_is_none(self) -> None:
        """Contexte neuf : un ContextVar non posé rend sa valeur par défaut.

        `Context()` est vide — ne PAS utiliser `get_current_session()` nu ici, un
        test voisin ayant déjà posé une valeur dans le contexte courant.
        """
        from contextvars import Context

        assert Context().run(get_current_session) is None

    def test_set_then_get(self) -> None:
        set_current_session("3d7a88d7-791b-45da-b8b9-75727e3c9eec")
        assert get_current_session() == "3d7a88d7-791b-45da-b8b9-75727e3c9eec"
        set_current_session(None)

    def test_isolated_between_tasks(self) -> None:
        async def scenario() -> tuple[str | None, str | None]:
            async def inner() -> str | None:
                set_current_session("11111111-1111-4111-8111-111111111111")
                return get_current_session()

            inside = await asyncio.create_task(inner())
            return inside, get_current_session()

        inside, outside = asyncio.run(scenario())
        assert inside == "11111111-1111-4111-8111-111111111111"
        assert outside is None


class TestCallDepth:
    def test_outermost_call_is_reported_once(self) -> None:
        outer = enter_call()
        assert is_outermost_call() is True
        inner = enter_call()
        assert is_outermost_call() is False
        exit_call(inner)
        assert is_outermost_call() is True
        exit_call(outer)

    def test_depth_resets_after_exit(self) -> None:
        token = enter_call()
        exit_call(token)
        again = enter_call()
        assert is_outermost_call() is True
        exit_call(again)

    def test_outside_any_call_is_not_outermost(self) -> None:
        assert is_outermost_call() is False
