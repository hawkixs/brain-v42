"""Lecture des appels d'outil RÉELLEMENT émis par la phase REORG.

Le rapport de l'agent est une DÉCLARATION. Le flux d'événements est une
OBSERVATION. Les confronter est la seule façon de voir le fantôme `bccc9115` :
un id déclaré dans `updated` pour lequel aucun `brain_update` n'a jamais été
émis. Le cas symétrique — un `brain_update` émis pour un id que le rapport ne
mentionne pas — est aujourd'hui totalement invisible, et c'est le plus inquiétant
des deux : une mutation dont personne n'a la trace.

DEUX FORMATS, et l'exigence est dure. Le rail vivant est codex
(`BRAIN_DREAM_AGENT_PROVIDER=codex` par défaut) ; agy est le repli. Un parseur
mono-format rendrait « 0 écriture observée » sur toute nuit de repli — un faux
négatif qui a exactement la forme de la bonne nouvelle, et que personne ne
questionnerait. C'est pour ça que « aucun événement reconnu » doit être un fait
DISTINCT de « zéro appel », et non le même silence.

Les deux formes ci-dessous sont MESURÉES sur des flux réels du dépôt, pas
supposées : `logs/dream/2026-08-20_watchk-claude_reorg.events.jsonl` pour codex,
`logs/dream/2026-08-13_red-shrik:agent_reorg.events.jsonl` pour agy. La forme agy
en particulier ne se devine pas — les arguments de l'outil vivent sous
`tool_info.parameters.Arguments`, alors que le nom vit sous `ToolName` et que
`tool_name` vaut invariablement `call_mcp_tool`.
"""

from __future__ import annotations

import json

from scripts.dream.reorg_events import scan_events

_LID = "f9bd5e03-cbc4-43d4-92d8-c3c98c1a19b6"
_DID = "cf988d7c-ffb1-4bd4-8b64-cbaf39c9256e"


def _codex_update(entity_id: str, tool: str = "brain_update") -> str:
    """Forme mesurée sur un flux codex réel (item.completed / mcp_tool_call)."""
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_44",
                "type": "mcp_tool_call",
                "server": "brain-v42",
                "tool": tool,
                "arguments": {
                    "entity_type": "learning",
                    "entity_id": entity_id,
                    "fields": {"tags": ["a"]},
                },
                "status": "completed",
                "error": None,
            },
        }
    )


def _agy_update(entity_id: str, tool: str = "brain_update") -> str:
    """Forme mesurée sur un flux agy réel (step_update / call_mcp_tool)."""
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 12,
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "call_mcp_tool",
                "tool_info": {
                    "name": "call_mcp_tool",
                    "parameters": {
                        "Arguments": {"entity_type": "learning", "entity_id": entity_id},
                        "ServerName": "brain-v42",
                        "ToolName": tool,
                    },
                },
            },
        }
    )


def test_codex_updates_are_observed() -> None:
    scan = scan_events("\n".join([_codex_update(_LID), _codex_update(_DID)]))

    assert scan.updated_ids == {_LID, _DID}
    assert scan.codex_events == 2
    assert scan.agy_events == 0
    assert scan.recognised is True


def test_agy_updates_are_observed() -> None:
    """Le repli doit compter autant que le rail vivant.

    C'est LE test qui empêche le faux négatif : sans lui, une nuit de repli
    rendrait un flux muet, et « aucune écriture non déclarée » se lirait comme
    une nuit propre alors que rien n'aurait été regardé.
    """
    scan = scan_events("\n".join([_agy_update(_LID), _agy_update(_DID)]))

    assert scan.updated_ids == {_LID, _DID}
    assert scan.agy_events == 2
    assert scan.codex_events == 0
    assert scan.recognised is True


def test_a_mixed_stream_is_read_in_both_dialects() -> None:
    """Improbable en production, décisif comme témoin : aucun format n'en exclut l'autre."""
    scan = scan_events("\n".join([_codex_update(_LID), _agy_update(_DID)]))

    assert scan.updated_ids == {_LID, _DID}
    assert scan.codex_events == 1
    assert scan.agy_events == 1


def test_only_brain_update_counts_as_a_mutation() -> None:
    """`brain_list` et `brain_get` sont l'essentiel du flux — les compter noierait le signal.

    REORG pagine son corpus entier avant de muter quoi que ce soit : sur le flux
    du 2026-08-20, 22 `brain_list` et 6 `brain_get` pour 4 `brain_update`.
    """
    scan = scan_events(
        "\n".join(
            [
                _codex_update(_LID, tool="brain_get"),
                _agy_update(_DID, tool="brain_list"),
                _codex_update(_DID),
            ]
        )
    )

    assert scan.updated_ids == {_DID}


def test_another_mcp_server_is_not_ours() -> None:
    """Un `brain_update` d'un autre serveur MCP ne prouve rien sur notre corpus."""
    foreign = json.loads(_codex_update(_LID))
    foreign["item"]["server"] = "some-other-server"
    scan = scan_events(json.dumps(foreign))

    assert scan.updated_ids == set()


def test_blank_and_malformed_lines_are_skipped_without_crashing() -> None:
    """Un flux tronqué par un timeout est le cas NORMAL, pas l'exception."""
    scan = scan_events("\n".join(["", "   ", "{ceci n'est pas du JSON", _codex_update(_LID)]))

    assert scan.updated_ids == {_LID}
    assert scan.recognised is True


def test_an_unreadable_stream_is_not_the_same_fact_as_zero_calls() -> None:
    """LE piège du lot : « rien reconnu » et « rien appelé » doivent se distinguer.

    Les deux donnent un ensemble vide. Les confondre transforme une panne de
    lecture — nouveau format d'agent, fichier vide, flux d'une autre commande —
    en bonne nouvelle silencieuse.
    """
    scan = scan_events('{"type":"thread.started","thread_id":"x"}\n{"unknown":"shape"}')

    assert scan.updated_ids == set()
    assert scan.recognised is False


def test_an_update_without_an_entity_id_is_counted_but_not_attributed() -> None:
    """Un appel sans id est observé sans être attribuable — il ne doit rien inventer.

    Le compter comme un événement reconnu garde `recognised` honnête ; lui
    fabriquer un id ferait dire au contrôle de symétrie une chose qu'il n'a pas vue.
    """
    payload = json.loads(_codex_update(_LID))
    del payload["item"]["arguments"]["entity_id"]
    scan = scan_events(json.dumps(payload))

    assert scan.updated_ids == set()
    assert scan.recognised is True


# ────────── Symétrie rapport ↔ appels observés ───────────────────────────────


def _report(updated: list[str], archived: list[str] | None = None) -> dict:
    return {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": updated,
        "archived_ids": archived or [],
    }


def test_a_declared_id_that_was_never_called_is_named() -> None:
    """Le fantôme `bccc9115` : déclaré dans `updated`, jamais écrit.

    C'est la seule direction qu'on savait déjà chercher — et encore, à la main.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events(_codex_update(_LID))
    warnings = symmetry_warnings(_report([_LID, _DID]), scan)

    assert len(warnings) == 1
    assert _DID in warnings[0]
    assert _LID not in warnings[0], "l'id réellement écrit ne doit pas être dénoncé"


def test_a_call_that_no_report_declared_is_named() -> None:
    """La direction aujourd'hui INVISIBLE, et la plus inquiétante des deux.

    Une mutation dont le rapport ne parle pas ne laisse aucune trace lisible :
    ni le validateur, ni l'alerte, ni le briefing ne la mentionneraient. Elle
    n'existerait que dans le flux d'événements, que personne ne relit.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events("\n".join([_codex_update(_LID), _codex_update(_DID)]))
    warnings = symmetry_warnings(_report([_LID]), scan)

    assert len(warnings) == 1
    assert _DID in warnings[0]


def test_an_archived_id_is_declared_too() -> None:
    """Partie 2 passe aussi par `brain_update` — l'ignorer inventerait des fantômes.

    `phase_reorg.md` §Partie 2 d : archiver, c'est écrire
    `fields={"freshness_status": "archived"}` avec le même outil. Comparer au seul
    `updated` dénoncerait chaque archivage comme une mutation non déclarée, et le
    contrôle crierait toutes les nuits sur son propre fonctionnement nominal.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events(_codex_update(_DID))

    assert symmetry_warnings(_report([], archived=[_DID]), scan) == []


def test_a_matching_pair_is_silent() -> None:
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events(_codex_update(_LID))

    assert symmetry_warnings(_report([_LID]), scan) == []


def test_an_unreadable_stream_says_so_instead_of_denouncing_everything() -> None:
    """LE faux négatif inversé, et son jumeau : ni « tout est faux », ni « tout va bien ».

    Sur un flux illisible, `updated_ids` est vide. Le comparer naïvement
    dénoncerait CHAQUE id déclaré comme un fantôme — une alerte massive, fausse,
    qu'on apprendrait vite à ignorer. Et taire le problème rendrait « rien
    d'anormal » indiscernable de « rien n'a été lu ». Le seul avertissement
    honnête nomme l'incapacité à vérifier.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events('{"type":"thread.started","thread_id":"x"}')
    warnings = symmetry_warnings(_report([_LID, _DID]), scan)

    assert len(warnings) == 1
    assert "UNVERIFIED" in warnings[0]
    assert _LID not in warnings[0] and _DID not in warnings[0]


# ────────── Le CLI : un AVERTISSEMENT, jamais un échec ───────────────────────


def _validator_argv(tmp_path, *, trailer: str, events: str | None) -> list[str]:
    report_log = tmp_path / "reorg.log"
    report_log.write_text(trailer, encoding="utf-8")
    tags_before = tmp_path / "tags_before.json"
    tags_before.write_text("{}", encoding="utf-8")
    events_path = tmp_path / "reorg.events.jsonl"
    if events is not None:
        events_path.write_text(events, encoding="utf-8")
    return [
        "--report-log",
        str(report_log),
        "--project-key",
        "rv-cli-unused",
        "--tags-before-json",
        str(tags_before),
        "--events-jsonl",
        str(events_path),
    ]


def _empty_trailer() -> str:
    return '=== REORG REPORT ===\n{"dry_run": false, "updated": [], "archived": []}\n=== END ===\n'


def test_the_cli_warns_without_failing_the_phase(tmp_path, monkeypatch, capsys) -> None:
    """Une écriture non déclarée doit se VOIR, et ne doit pas encore faire rougir.

    L'escalade en échec attend une semaine d'observation propre. Une garde qui
    commence par faire échouer des nuits qu'elle n'a jamais mesurées apprend aux
    opérateurs à la désactiver — et c'est la seule panne dont on ne revient pas.
    """
    from unittest.mock import MagicMock

    from scripts.dream import reorg_validate

    monkeypatch.setattr(
        reorg_validate,
        "Settings",
        lambda: MagicMock(postgres_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(reorg_validate, "_build_factory", lambda _url: MagicMock())

    argv = _validator_argv(tmp_path, trailer=_empty_trailer(), events=_codex_update(_LID))
    rc = reorg_validate.main(argv)

    err = capsys.readouterr().err
    assert rc == 0, "le contrôle de symétrie ne doit pas changer le code de retour"
    assert "REORG SYMMETRY WARN" in err
    assert _LID in err
    assert "REORG VALIDATE: OK" in err, "le verdict du validateur doit rester lisible"


def test_an_absent_event_file_is_announced_once(tmp_path, monkeypatch, capsys) -> None:
    """Un flux absent (phase morte avant d'écrire) s'annonce, sans crasher ni doubler.

    Deux avertissements pour un seul fait apprennent à survoler les
    avertissements ; c'est ainsi qu'une alerte cesse d'être lue.
    """
    from unittest.mock import MagicMock

    from scripts.dream import reorg_validate

    monkeypatch.setattr(
        reorg_validate,
        "Settings",
        lambda: MagicMock(postgres_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(reorg_validate, "_build_factory", lambda _url: MagicMock())

    argv = _validator_argv(tmp_path, trailer=_empty_trailer(), events=None)
    rc = reorg_validate.main(argv)

    err = capsys.readouterr().err
    assert rc == 0
    assert err.count("REORG SYMMETRY WARN") == 1, f"avertissements doublés :\n{err}"
    assert "unreadable" in err
