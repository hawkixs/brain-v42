"""Code de sortie discriminant de scripts/dream.sh — blocs shell EXÉCUTÉS.

Contexte mesuré le 2026-08-07 : la nuit a rendu
« 8/9 phases OK, 1 timed out (extract), 2 skipped (promote sweep) » et
`brain-v42-dream.service` était pourtant `failed (Result: exit-code)`. Le seul
écart était le timeout CONTRÔLÉ d'extract (rc=3 — échéance que le CLI s'impose
à lui-même après avoir enregistré son dream_run terminal), observé 5 nuits sur
6 entre le 2026-08-02 et le 2026-08-07. Une unité rouge en permanence ne porte
plus d'information : la prochaine VRAIE panne ne changera rien à l'écran.

Ces tests n'inspectent pas le TEXTE du script. Ils découpent les deux blocs
shell concernés dans le fichier réel et les EXÉCUTENT sous bash avec des
tableaux préfabriqués et des stubs pour `log` et `uv`. Un test qui cherche une
chaîne prouve que le texte existe ; celui-ci prouve que bash prend la bonne
décision.

Deux blocs, deux maillons de la même chaîne :
  1. la CLASSIFICATION (extract_rc → tableaux),
  2. le VERDICT (tableaux → code de sortie + alerte + résumé).
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"

# Ancres de découpe. Ce sont des morceaux de code réel, pas des commentaires :
# un remaniement qui les ferait disparaître casse le test bruyamment au lieu de
# le laisser vert sur du vide.
_VERDICT_ANCHOR = "FAIL_TOTAL=$(("
_EXTRACT_ANCHOR = "if (( extract_rc == 0 )); then"
_EXTRACT_END_ANCHOR = "# --- ROADMAP"
_PHASE_CASE_ANCHOR = 'case "$phase_rc" in'
_PHASE_CASE_END_ANCHOR = "esac"


def _source() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def _verdict_block() -> str:
    """Du calcul des compteurs jusqu'à la fin du fichier."""
    content = _source()
    start = content.index(_VERDICT_ANCHOR)
    return content[start:]


def _extract_classification_block() -> str:
    """La cascade `extract_rc` → tableaux, garde-fou externe compris."""
    content = _source()
    start = content.index(_EXTRACT_ANCHOR)
    end = content.index(_EXTRACT_END_ANCHOR, start)
    return content[start:end]


def _phase_classification_block() -> str:
    """Le `case "$phase_rc"` des SIX phases agent (scan…reorg).

    Elles partagent la porte de sortie avec extract mais n'ont jamais eu de
    témoin Python : le seul témoin était un script bash que `testpaths=["tests"]`
    ne collecte pas et qu'aucun rail de CI n'appelle.
    """
    content = _source()
    start = content.index(_PHASE_CASE_ANCHOR)
    end = content.index(_PHASE_CASE_END_ANCHOR, start) + len(_PHASE_CASE_END_ANCHOR)
    return content[start:end]


def _bash_array(values: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(v) for v in values)


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )


def _run_verdict(
    tmp_path: Path,
    *,
    failed: tuple[str, ...] = (),
    timed_out: tuple[str, ...] = (),
    controlled: tuple[str, ...] = (),
    skipped: tuple[str, ...] = (),
    fallbacks: tuple[str, ...] = (),
    total: int = 9,
    alert_rc: int = 0,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Exécute le bloc verdict réel; rend (process, appels `uv` capturés)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    alert_calls = tmp_path / "alert_calls.txt"
    alert_calls.write_text("", encoding="utf-8")

    harness = "\n".join(
        [
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(log_dir))}",
            "TIMESTAMP=2026-08-07",
            # Le bloc de verdict passe désormais le manifeste de la nuit à
            # l'alerte (ticket 0a9c067e). Stub de HARNAIS : ce qu'il contient
            # est vérifié par test_dream_sh_run_manifest.py, et ce qu'on en
            # fait par test_dream_sh_coverage_verdict.py.
            'MANIFEST_FILE="$LOG_DIR/${TIMESTAMP}_manifest.tsv"',
            "PROJECT_KEY=brain-v42",
            f"TOTAL_PHASES={total}",
            f"declare -a FAILED_PHASES=({_bash_array(failed)})",
            f"declare -a TIMED_OUT_PHASES=({_bash_array(timed_out)})",
            f"declare -a CONTROLLED_TIMEOUT_PHASES=({_bash_array(controlled)})",
            f"declare -a SKIPPED_PHASES=({_bash_array(skipped)})",
            f"declare -a FALLBACK_PHASES=({_bash_array(fallbacks)})",
            f"ALERT_CALLS={shlex.quote(str(alert_calls))}",
            'log() { printf "%s\\n" "$*"; }',
            # Stub de HARNAIS, pas de comportement : les blocs découpés
            # déclarent désormais chaque décision au manifeste de la nuit
            # (ticket 0a9c067e). Sans lui, `manifest_put` serait introuvable
            # et `set -e` ferait sortir bash en 127 — un faux témoin vert pour
            # les tests qui attendent 1. Ce que la déclaration écrit est
            # vérifié par tests/unit/test_dream_sh_run_manifest.py.
            "manifest_put() { :; }",
            f"ALERT_RC={alert_rc}",
            'uv() { printf "%s\\n" "$*" >> "$ALERT_CALLS"; return "$ALERT_RC"; }',
            "",
            _verdict_block(),
        ]
    )
    proc = _run(harness)
    # Garde de harnais (pas une assertion de comportement) : sous `set -u`, une
    # variable oubliée ferait sortir bash en 1 et rendrait vert un test qui
    # attend 1. On refuse ce faux témoin.
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    return proc, alert_calls.read_text(encoding="utf-8")


def _run_extract_classification(extract_rc: int) -> dict[str, list[str]]:
    """Exécute la cascade de classification d'extract pour un rc donné."""
    harness = "\n".join(
        [
            "set -euo pipefail",
            "TIMESTAMP=2026-08-07",
            f"extract_rc={extract_rc}",
            "declare -a FAILED_PHASES=()",
            "declare -a TIMED_OUT_PHASES=()",
            "declare -a CONTROLLED_TIMEOUT_PHASES=()",
            "log() { :; }",
            # Stub de HARNAIS, pas de comportement : les blocs découpés
            # déclarent désormais chaque décision au manifeste de la nuit
            # (ticket 0a9c067e). Sans lui, `manifest_put` serait introuvable
            # et `set -e` ferait sortir bash en 127 — un faux témoin vert pour
            # les tests qui attendent 1. Ce que la déclaration écrit est
            # vérifié par tests/unit/test_dream_sh_run_manifest.py.
            "manifest_put() { :; }",
            # `if true; then` équilibre le `fi` final du bloc découpé, qui
            # fermait le killswitch EXTRACT resté hors périmètre.
            "if true; then",
            _extract_classification_block(),
            'printf "FAILED=%s\\n" "${FAILED_PHASES[*]}"',
            'printf "TIMED_OUT=%s\\n" "${TIMED_OUT_PHASES[*]}"',
            'printf "CONTROLLED=%s\\n" "${CONTROLLED_TIMEOUT_PHASES[*]}"',
        ]
    )
    proc = _run(harness)
    return _parse_arrays(proc)


def _parse_arrays(proc: subprocess.CompletedProcess[str]) -> dict[str, list[str]]:
    assert proc.returncode == 0, f"harnais cassé (rc={proc.returncode}): {proc.stderr}"
    assert proc.stderr == "", f"le harnais a bruité sur stderr: {proc.stderr!r}"
    parsed: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        parsed[key] = value.split()
    return parsed


def _run_phase_classification(phase_rc: int) -> dict[str, list[str]]:
    """Exécute le `case "$phase_rc"` réel d'une phase agent pour un rc donné."""
    harness = "\n".join(
        [
            "set -euo pipefail",
            "name=synth",
            # Stub de HARNAIS, pas de comportement : les blocs découpés
            # déclarent désormais chaque décision au manifeste de la nuit
            # (ticket 0a9c067e). Sans lui, `manifest_put` serait introuvable
            # et `set -e` ferait sortir bash en 127 — un faux témoin vert pour
            # les tests qui attendent 1. Ce que la déclaration écrit est
            # vérifié par tests/unit/test_dream_sh_run_manifest.py.
            "manifest_put() { :; }",
            "PROJECT_KEY=brain-v42",
            f"phase_rc={phase_rc}",
            "declare -a FAILED_PHASES=()",
            "declare -a TIMED_OUT_PHASES=()",
            "declare -a CONTROLLED_TIMEOUT_PHASES=()",
            _phase_classification_block(),
            'printf "FAILED=%s\\n" "${FAILED_PHASES[*]}"',
            'printf "TIMED_OUT=%s\\n" "${TIMED_OUT_PHASES[*]}"',
            'printf "CONTROLLED=%s\\n" "${CONTROLLED_TIMEOUT_PHASES[*]}"',
        ]
    )
    return _parse_arrays(_run(harness))


# --- B3 : la nuit de référence (échéance contrôlée seule) sort en 0 ----------


def test_controlled_deadline_only_night_exits_zero(tmp_path: Path) -> None:
    """Nuit du 2026-08-07 rejouée : extract en échéance CONTRÔLÉE, rien d'autre.

    L'unité systemd doit redevenir verte : la phase a fait ce qu'elle a pu dans
    le temps qu'elle s'était elle-même imposé, et a enregistré son dream_run
    terminal avant de rendre la main.
    """
    proc, _ = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        skipped=("promote", "sweep"),
        total=9,
    )
    assert proc.returncode == 0, (
        "une échéance contrôlée seule doit sortir en 0 — sinon l'unité est "
        f"rouge 5 nuits sur 6 et n'informe plus de rien. stdout={proc.stdout!r}"
    )


def test_controlled_deadline_only_night_still_alerts(tmp_path: Path) -> None:
    """B1 : rendre le code de sortie moins sensible ne doit RIEN éteindre.

    post_run_alert n'est appelé aujourd'hui que sur anomalie. Découpler le
    code de sortie sans découpler l'alerte l'aurait supprimée du même geste.
    """
    _, alert_calls = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        skipped=("promote", "sweep"),
        total=9,
    )
    assert "scripts.dream.post_run_alert" in alert_calls, (
        "l'alerte de fin de nuit doit rester envoyée sur une échéance "
        f"contrôlée. Appels uv capturés: {alert_calls!r}"
    )
    assert "--date 2026-08-07" in alert_calls
    # §11 : `--project-key` a été RETIRÉ de l'appel. Il était décoratif —
    # `fetch_failed_runs` ne le recevait même pas — et avec la rotation du
    # pool il aurait transmis une clé différente chaque nuit, sans lecteur.
    assert "--project-key" not in alert_calls


def test_controlled_deadline_summary_hides_nothing(tmp_path: Path) -> None:
    """B4 : on cesse d'en faire un échec d'unité, on ne le cache pas."""
    proc, _ = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        skipped=("promote", "sweep"),
        total=9,
    )
    assert "8/9 phases OK" in proc.stdout, proc.stdout
    assert "1 timed out (extract)" in proc.stdout, proc.stdout
    assert "2 skipped (promote sweep)" in proc.stdout, proc.stdout


# --- B2 : ce qui doit rester rouge ------------------------------------------


def test_hard_failure_exits_non_zero(tmp_path: Path) -> None:
    """Une phase réellement en échec garde un code de sortie non nul."""
    proc, _ = _run_verdict(tmp_path, failed=("roadmap",), total=9)
    assert proc.returncode != 0, (
        f"un échec dur doit rester visible dans systemd. stdout={proc.stdout!r}"
    )


def test_outer_guard_timeout_exits_non_zero(tmp_path: Path) -> None:
    """Timeout de garde-fou EXTERNE : l'état de la phase est INCONNU.

    Non classé comme échéance contrôlée -> il compte comme un échec. C'est le
    sens de la garde fail-closed : une nature de timeout ajoutée demain et
    oubliée ici reste bruyante au lieu de devenir silencieuse.
    """
    proc, _ = _run_verdict(tmp_path, timed_out=("synth",), total=9)
    assert proc.returncode != 0, (
        "un timeout de garde-fou externe laisse la phase dans un état inconnu "
        f"et doit sortir non nul. stdout={proc.stdout!r}"
    )


def test_mixed_night_exits_non_zero(tmp_path: Path) -> None:
    """B5 : échec dur ET échéance contrôlée la même nuit -> sortie non nulle.

    Le cas mixte est celui que les implémentations ratent : soustraire les
    échéances contrôlées sans borner le compteur à zéro, ou tester
    `timed_out == controlled` au lieu du total, rend cette nuit verte.
    """
    proc, _ = _run_verdict(
        tmp_path,
        failed=("roadmap",),
        timed_out=("extract",),
        controlled=("extract",),
        total=9,
    )
    assert proc.returncode != 0, (
        "un échec dur ne doit jamais être masqué par une échéance contrôlée "
        f"concomitante. stdout={proc.stdout!r}"
    )


def test_mixed_night_still_alerts(tmp_path: Path) -> None:
    """Le cas mixte doit aussi alerter (aucune régression de sensibilité)."""
    _, alert_calls = _run_verdict(
        tmp_path,
        failed=("roadmap",),
        timed_out=("extract",),
        controlled=("extract",),
        total=9,
    )
    assert "scripts.dream.post_run_alert" in alert_calls, alert_calls


def test_a_hard_failure_marked_controlled_by_mistake_still_exits_non_zero(
    tmp_path: Path,
) -> None:
    """La garde doit être STRUCTURELLE, pas arithmétique.

    La forme d'origine soustrayait `#CONTROLLED` du total : elle n'était
    fail-closed que tant que CONTROLLED ⊆ TIMED_OUT, invariant qu'aucune garde
    n'imposait. Une phase inscrite par erreur dans FAILED **et** CONTROLLED —
    un motif recopié dans la mauvaise branche, ce qui est le mode d'erreur
    normal d'un `case` shell — effaçait son propre échec : le script sortait en
    0 en imprimant lui-même « 1 failed (synth) ».

    Ici FAILED_PHASES est interrogé pour lui-même. Aucune écriture dans
    CONTROLLED_TIMEOUT_PHASES ne peut plus le masquer.
    """
    proc, _ = _run_verdict(
        tmp_path,
        failed=("synth",),
        timed_out=(),
        controlled=("synth",),
        total=9,
    )
    assert "1 failed (synth)" in proc.stdout, (
        f"le scénario n'a pas été atteint (résumé attendu). stdout={proc.stdout!r}"
    )
    assert proc.returncode != 0, (
        "un échec dur ne doit jamais pouvoir être effacé par une inscription "
        f"erronée dans les échéances contrôlées. stdout={proc.stdout!r}"
    )


def test_failed_alert_on_a_controlled_night_still_exits_non_zero(tmp_path: Path) -> None:
    """B1 pour la LIVRAISON, pas seulement pour l'invocation.

    Depuis que la nuit à échéance contrôlée sort en 0 — soit le cas
    majoritaire —, le log est le seul témoin restant. Si le rapporteur
    lui-même échoue (base indisponible, `TooManyConnectionsError` mesurée le
    2026-06-10), plus rien n'est délivré ET l'unité reste verte : la nuit
    devient totalement muette. Un échec du rapporteur est précisément le genre
    de panne que ce chantier veut rendre de nouveau visible.
    """
    proc, alert_calls = _run_verdict(
        tmp_path,
        timed_out=("extract",),
        controlled=("extract",),
        alert_rc=1,
        total=9,
    )
    assert "scripts.dream.post_run_alert" in alert_calls, (
        f"le scénario n'a pas été atteint (alerte non tentée): {alert_calls!r}"
    )
    assert proc.returncode != 0, (
        "une alerte non délivrée doit rougir l'unité : sinon la nuit ne laisse "
        f"aucune trace nulle part. stdout={proc.stdout!r}"
    )


def test_clean_night_exits_zero_and_reports_all_phases(tmp_path: Path) -> None:
    """Nuit sans anomalie : inchangée."""
    proc, _ = _run_verdict(tmp_path, total=9)
    assert proc.returncode == 0
    assert "9/9 phases OK" in proc.stdout, proc.stdout


def test_a_night_carried_by_the_fallback_is_never_reported_as_clean(
    tmp_path: Path,
) -> None:
    """Un rail primaire mort ne doit pas ressembler à une nuit parfaite.

    Mesuré du 2026-08-11 au 2026-08-17 : le code-mode host éteint faisait
    échouer les 60 phases codex de chaque nuit, le repli agy les rattrapait
    toutes, et la nuit signait « 63/63 phases OK ». Six nuits, aucun voyant.
    `FAIL_TOTAL` compte des PHASES — une phase repliée est une phase réussie —
    quand `dream_runs` enregistre des TENTATIVES : 60 lignes `fail` que personne
    ne lisait, le rapporteur étant court-circuité par le `exit 0` du cas propre.

    Le repli reste un succès : la nuit sort bien en 0, on ne rougit pas une
    unité pour un secours qui a fait son travail. Mais le résumé le NOMME, et le
    rapporteur parle sur toutes les nuits, pas seulement celles déjà rouges.
    """
    proc, alert_calls = _run_verdict(
        tmp_path,
        total=63,
        fallbacks=("brain-v42/scan", "brain-v42/clean"),
    )

    assert proc.returncode == 0, proc.stdout
    assert "63/63 phases OK" in proc.stdout, proc.stdout
    assert "2 repliées" in proc.stdout, (
        f"une nuit portée par le secours se lit comme une nuit parfaite: {proc.stdout!r}"
    )
    assert "brain-v42/scan" in proc.stdout, proc.stdout
    assert "scripts.dream.post_run_alert" in alert_calls, (
        "le rapporteur reste court-circuité quand aucune PHASE n'a échoué, "
        f"donc les tentatives échouées ne sont lues par personne: {alert_calls!r}"
    )


# --- Classification amont : extract_rc → tableaux ---------------------------


def test_extract_controlled_deadline_is_classified_controlled() -> None:
    """rc=3 : échéance bornée, dream_run terminal enregistré."""
    arrays = _run_extract_classification(3)
    assert arrays["CONTROLLED"] == ["*/extract"], arrays
    assert arrays["TIMED_OUT"] == ["*/extract"], arrays
    assert arrays["FAILED"] == [], arrays


def test_extract_outer_guard_is_not_classified_controlled() -> None:
    """rc=124 : `timeout 10m` a tué le process, état inconnu.

    Il reste dans TIMED_OUT (alerte + résumé) mais ne doit JAMAIS entrer dans
    les échéances contrôlées, sinon le garde-fou externe devient silencieux.
    """
    arrays = _run_extract_classification(124)
    assert arrays["CONTROLLED"] == [], arrays
    assert arrays["TIMED_OUT"] == ["*/extract"], arrays


def test_extract_hard_failure_is_classified_failed() -> None:
    arrays = _run_extract_classification(1)
    assert arrays["FAILED"] == ["*/extract"], arrays
    assert arrays["CONTROLLED"] == [], arrays


def test_extract_deferral_is_not_an_anomaly() -> None:
    """rc=4 : rien n'a été coupé court, aucune anomalie à compter."""
    arrays = _run_extract_classification(4)
    assert arrays["FAILED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays
    assert arrays["CONTROLLED"] == [], arrays


def test_extract_success_is_not_an_anomaly() -> None:
    arrays = _run_extract_classification(0)
    assert arrays["FAILED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays
    assert arrays["CONTROLLED"] == [], arrays


# --- Classification amont : phase_rc des SIX phases agent -------------------
#
# Ces phases (scan, clean, connect, synth, promote, reorg) partagent la porte
# de sortie avec extract mais n'avaient aucun témoin Python. Le seul témoin,
# tests/integration/test_dream_sh_fail_propagation.sh, n'est collecté par aucun
# rail : `testpaths=["tests"]` ne ramasse pas les .sh et .github/workflows/ ne
# l'appelle pas.


def test_agent_phase_timeout_is_never_a_controlled_deadline() -> None:
    """phase_rc=2 : `timeout Nm` (ou codex_runner) a TUÉ l'agent en vol.

    Rien n'a été enregistré, l'état de travail est inconnu — c'est le
    postmortem du 2026-04-09. Exempter ces timeouts rendrait l'unité verte sur
    exactement la panne qu'elle doit signaler. Ils restent dans TIMED_OUT
    (alerte + résumé) et n'entrent JAMAIS dans les échéances contrôlées.
    """
    arrays = _run_phase_classification(2)
    assert arrays["CONTROLLED"] == [], arrays
    assert arrays["TIMED_OUT"] == ["brain-v42/synth"], arrays
    assert arrays["FAILED"] == [], arrays


def test_agent_phase_hard_failure_is_classified_failed() -> None:
    arrays = _run_phase_classification(1)
    assert arrays["FAILED"] == ["brain-v42/synth"], arrays
    assert arrays["CONTROLLED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays


def test_agent_phase_success_is_not_an_anomaly() -> None:
    arrays = _run_phase_classification(0)
    assert arrays["FAILED"] == [], arrays
    assert arrays["TIMED_OUT"] == [], arrays
    assert arrays["CONTROLLED"] == [], arrays


def test_dream_shell_remains_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(DREAM_SH)], check=True)


# --- Bout en bout : le script RÉEL, du rc d'extract au code de sortie -------
#
# Les deux harnais ci-dessus prouvent chacun un maillon. Celui-ci prouve qu'ils
# sont CÂBLÉS ensemble : dream.sh entier, avec `claude` et `uv` mockés, dans un
# arbre temporaire (LOG_DIR = $SCRIPT_DIR/../logs/dream, donc aucun log de prod
# touché) et son propre XDG_RUNTIME_DIR pour ne pas partager le flock de
# production. Même patron que tests/integration/test_dream_sh_fail_propagation.sh.


def _run_full_dream(
    tmp_path: Path, *, extract_rc: int, claude_rc: int = 0
) -> tuple[subprocess.CompletedProcess[str], str]:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dream_copy = scripts_dir / "dream.sh"
    dream_copy.write_text(_source(), encoding="utf-8")
    dream_copy.chmod(0o755)
    subprocess.run(
        ["cp", "-r", str(REPO_ROOT / "scripts" / "dream"), str(scripts_dir / "dream")],
        check=True,
    )

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    claude_mock = mock_bin / "claude"
    claude_mock.write_text(
        "#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\n"
        'echo "[mock claude] phase output"\nexit "${CLAUDE_MOCK_EXIT:-0}"\n',
        encoding="utf-8",
    )
    claude_mock.chmod(0o755)
    # `uv` sensible à ses arguments : seul ticket_extract rend le rc injecté,
    # tout le reste (preflight, parsers, alerte) est un no-op silencieux.
    uv_mock = mock_bin / "uv"
    uv_mock.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *scripts.ticket_extract* ]]; then\n'
        '  exit "${EXTRACT_MOCK_EXIT:-0}"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    uv_mock.chmod(0o755)

    proc = subprocess.run(
        [str(dream_copy), "test-project"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env={
            "HOME": str(tmp_path),
            "PATH": f"{mock_bin}:/usr/bin:/bin",
            # Verrou privé : le script sortirait 0 en trouvant le flock de
            # production pris, ce qui rendrait ce test vert pour rien.
            "XDG_RUNTIME_DIR": str(tmp_path),
            "BRAIN_DREAM_AGENT_PROVIDER": "claude",
            # Préflight claude livré le 2026-08-11 : pas de jeton, pas de nuit.
            "MCP_HTTP_TOKEN": "test-only-token",
            "BRAIN_DREAM_EXTRACT_ENABLED": "true",
            "CLAUDE_MOCK_EXIT": str(claude_rc),
            "EXTRACT_MOCK_EXIT": str(extract_rc),
        },
    )
    log = (tmp_path / "logs" / "dream").glob("*.log")
    main_log = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in sorted(log) if "_" not in p.name
    )
    return proc, main_log


def test_full_run_with_controlled_extract_deadline_exits_zero(tmp_path: Path) -> None:
    """La nuit du 2026-08-07, rejouée sur le script entier : 0."""
    proc, main_log = _run_full_dream(tmp_path, extract_rc=3)
    assert "TIMEOUT extract (controlled deadline" in main_log, (
        f"le scénario n'a pas été atteint. log={main_log!r} stdout={proc.stdout!r}"
    )
    assert proc.returncode == 0, (
        f"nuit à échéance contrôlée : l'unité doit être verte. log={main_log!r}"
    )


def test_full_run_with_outer_guard_extract_timeout_exits_non_zero(
    tmp_path: Path,
) -> None:
    """Même script, garde-fou externe (`timeout 10m`) : l'unité rougit."""
    proc, main_log = _run_full_dream(tmp_path, extract_rc=124)
    assert "TIMEOUT extract (outer guard" in main_log, (
        f"le scénario n'a pas été atteint. log={main_log!r} stdout={proc.stdout!r}"
    )
    assert proc.returncode != 0, f"état de phase inconnu : l'unité doit rougir. log={main_log!r}"
