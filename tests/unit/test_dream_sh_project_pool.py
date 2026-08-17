"""Le pool de projets — la boucle EXÉCUTÉE, pas seulement le texte du script.

Spec `2026-08-08-dream-project-pool-design.md` §3, §6, §9, §10.

Ces tests lancent une copie réelle de `dream.sh` avec des stubs pour `uv`,
`claude` et `codex`. Aucun appel réseau, aucune écriture en base : on observe
le journal et l'arborescence de `logs/dream/`, qui suffisent à prouver ce qui
compte ici — combien de projets ont été servis, et si leurs journaux ont
survécu les uns aux autres.

Le mode de panne visé est **vert et silencieux** dans les deux sens :
- un pool qui rétrécit à un projet sans erreur (transport systemd, §6) ;
- des journaux qui se tronquent l'un l'autre, ne laissant que le dernier
  projet au matin (§3.2).
Aucun des deux ne produit de code de sortie non nul. Seule une mesure les voit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Copie exécutable de dream.sh, hors production à tous les égards.

    `LOG_DIR` vaut `$SCRIPT_DIR/../logs/dream`, donc la copie journalise sous
    `tmp_path`. `XDG_RUNTIME_DIR` privé, sinon le script sortirait 0 en trouvant
    le flock de production pris — un vert pour rien.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dream_copy = scripts_dir / "dream.sh"
    dream_copy.write_text(DREAM_SH.read_text(encoding="utf-8"), encoding="utf-8")
    dream_copy.chmod(0o755)
    subprocess.run(
        ["cp", "-r", str(REPO_ROOT / "scripts" / "dream"), str(scripts_dir / "dream")],
        check=True,
    )

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()

    # `date` chirurgical : n'intercepte QUE `+%j`, délègue tout le reste au vrai
    # binaire (horodatages, noms de fichiers de log). Sans lui, la rotation
    # quotidienne du pool — `_rotation=$(( 10#$(date +%j) % taille ))` — rend
    # l'ORDRE du pool dépendant du jour où la suite tourne.
    date_stub = mock_bin / "date"
    date_stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == "+%j" && -n "${BRAIN_DREAM_FAKE_DOY:-}" ]]; then\n'
        '  printf "%s\\n" "$BRAIN_DREAM_FAKE_DOY"\n'
        "  exit 0\n"
        "fi\n"
        'exec /bin/date "$@"\n'
    )
    date_stub.chmod(0o755)

    for name in ("claude", "codex"):
        stub = mock_bin / name
        stub.write_text("#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n")
        stub.chmod(0o755)

    # `uv` échoue SÉLECTIVEMENT sur otel_split. Ce n'est pas un artifice : un
    # otel_split réussi supprime le brut (`rm -f "$raw_log"`) et laisse le vrai
    # binaire écrire report/otel, que le stub ne sait pas faire. Le faire
    # échouer emprunte la branche WARN — du code réel — qui recopie le brut vers
    # le rapport et touche l'otel. Les trois chemins projetés existent alors sur
    # disque, construits par le script et non par le test.
    # Le rail claude passe par scripts.dream.claude_runner depuis le
    # 2026-08-11. Le brut n'est donc plus créé par une redirection de dream.sh
    # mais par le runner lui-même : le stub doit reproduire ce contrat
    # observable, sinon la branche WARN d'otel_split n'a rien à recopier et le
    # test échoue sur une absence que la production ne produit pas.
    uv_stub = mock_bin / "uv"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null 2>&1 || true\n"
        'case "$*" in\n'
        "  *otel_split*) exit 1 ;;\n"
        "  *claude_runner*)\n"
        '    _raw=""\n'
        "    while (($#)); do\n"
        "      if [[ $1 == --raw-log ]]; then _raw=$2; shift 2; else shift; fi\n"
        "    done\n"
        '    [[ -n "$_raw" ]] && printf "mock claude phase output\\n" >> "$_raw"\n'
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)

    env = {
        "HOME": str(tmp_path),
        "PATH": f"{mock_bin}:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "BRAIN_DREAM_AGENT_PROVIDER": "claude",
        # Le rail claude a reçu son préflight le 2026-08-11, symétrique de
        # celui de codex. Une nuit sans jeton MCP ne doit pas démarrer : c'est
        # l'incident du 2026-07-03, six phases aveugles et « 6/6 OK ».
        "MCP_HTTP_TOKEN": "test-only-token",
    }
    return dream_copy, env


def _run(
    tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    dream_copy, env = _sandbox(tmp_path)
    env.update(extra_env or {})
    return subprocess.run(
        [str(dream_copy), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )


def _main_log(tmp_path: Path) -> str:
    """Le récit unique de la nuit — `$TIMESTAMP.log`, sans composante de phase."""
    logs = sorted((tmp_path / "logs" / "dream").glob("*.log"))
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in logs if "_" not in path.name
    )


# --- Le pool se forme, et il dit d'où il vient -----------------------------


def test_a_single_positional_still_serves_exactly_one_project(tmp_path: Path) -> None:
    """Régression de la propriété qui rend ce chantier sûr.

    Étapes 1 à 5 de §12 sont livrables « sans qu'une seule nuit change de
    comportement ». Un positionnel nu doit donc produire exactement la nuit
    d'avant : un projet, servi une fois.
    """
    _run(tmp_path, "brain-v42")
    log = _main_log(tmp_path)

    assert "Pool (1) from positional argument: brain-v42" in log
    assert log.count("--- Projet ") == 1


def test_a_comma_separated_pool_serves_every_project_once(tmp_path: Path) -> None:
    proc = _run(tmp_path, "alpha,beta,gamma")
    log = _main_log(tmp_path)

    assert log.count("--- Projet ") == 3, f"log={log!r} stderr={proc.stderr!r}"
    for project in ("alpha", "beta", "gamma"):
        assert f"--- Projet {project} ---" in log


def test_the_drop_in_variable_beats_the_positional(tmp_path: Path) -> None:
    """Le sens de priorité est une garde, pas une préférence.

    `ExecStart=` vit dans le template versionné que install.sh régénère ; le
    drop-in survit. Si le positionnel gagnait, élargir le pool dans le drop-in
    ne changerait rien et la nuit resterait à un projet — verte et muette.
    """
    # Jour pair -> rotation nulle sur un pool de 2, donc ordre d'écriture
    # préservé. L'ordre est ce que ce test épingle ; la rotation a son témoin.
    _run(
        tmp_path,
        "brain-v42",
        extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha,beta", "BRAIN_DREAM_FAKE_DOY": "222"},
    )
    log = _main_log(tmp_path)

    assert "Pool (2) from BRAIN_DREAM_PROJECT_POOL: alpha beta" in log
    assert "--- Projet brain-v42 ---" not in log


def test_the_pool_source_is_named_in_the_log(tmp_path: Path) -> None:
    """Une nuit à un projet ne doit pas être ambiguë au matin.

    Sans la source, « pool de 1 » ne distingue pas « le drop-in dit un projet »
    de « systemd a mangé la variable et on est retombé sur le positionnel ».
    """
    _run(tmp_path, "brain-v42", extra_env={"BRAIN_DREAM_PROJECT_POOL": "solo"})

    assert "from BRAIN_DREAM_PROJECT_POOL" in _main_log(tmp_path)


# --- Les pièges de transport font sortir en 2, jamais rétrécir en silence ---


def test_a_space_separated_pool_is_a_hard_failure(tmp_path: Path) -> None:
    """§6, le piège de transport nommé.

    `Environment=BRAIN_DREAM_PROJECT_POOL=a b` pose la variable à `a` et jette
    `b`. Mais une valeur protégée (`Environment="…=a b"`) arrive ENTIÈRE, avec
    son blanc. La traiter comme une seule clé fabriquerait un `project_key` que
    canonicalize_project_key rejette au fond d'une fonction best-effort qui
    avale son exception : la colonne resterait NULL sans un bruit.
    """
    proc = _run(tmp_path, "alpha", extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha beta"})

    assert proc.returncode == 2, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "comma-separated" in proc.stderr


def test_a_duplicate_key_is_a_hard_failure(tmp_path: Path) -> None:
    """Servir deux fois le même projet est une faute de frappe, pas un choix."""
    proc = _run(tmp_path, "alpha,beta,alpha")

    assert proc.returncode == 2
    assert "Duplicate" in proc.stderr


def test_an_empty_entry_is_a_hard_failure(tmp_path: Path) -> None:
    """`a,,b` est une virgule de trop, pas un projet anonyme."""
    proc = _run(tmp_path, "alpha,,beta")

    assert proc.returncode == 2
    assert "Empty project key" in proc.stderr


def test_a_slash_in_a_key_is_a_hard_failure(tmp_path: Path) -> None:
    """La clé entre dans un nom de fichier de journal (§3.2)."""
    proc = _run(tmp_path, "alpha/beta")

    assert proc.returncode == 2
    assert "Slash" in proc.stderr


def test_surrounding_whitespace_is_trimmed_not_rejected(tmp_path: Path) -> None:
    """`a, b` est une écriture humaine naturelle et sans ambiguïté."""
    _run(tmp_path, "alpha, beta", extra_env={"BRAIN_DREAM_FAKE_DOY": "222"})
    log = _main_log(tmp_path)

    assert "Pool (2) from positional argument: alpha beta" in log


# --- §3.2 : les journaux d'un projet ne survivent pas au suivant ------------


def test_each_project_keeps_its_own_phase_logs(tmp_path: Path) -> None:
    """codex_runner tronque : sans projection, seul le dernier projet survit.

    Le rapport de phase n'est pas qu'un journal — `PHASE_DEPS` le RELIT pour
    injecter le contexte de la phase précédente (§3.3). Un rapport écrasé fait
    lire à CONNECT de `beta` le rapport CLEAN d'`alpha`.
    """
    _run(tmp_path, "alpha,beta")
    names = {path.name for path in (tmp_path / "logs" / "dream").glob("*")}

    # La preuve n'est pas « chaque projet a des fichiers » : c'est que la MÊME
    # phase coexiste pour les deux. Un gabarit non projeté produirait un seul
    # `…_scan.log`, et le test passerait quand même si on se contentait de
    # compter des fichiers par projet.
    for phase in ("scan", "clean", "connect"):
        alpha = {n for n in names if n.endswith(f"_alpha_{phase}.log")}
        beta = {n for n in names if n.endswith(f"_beta_{phase}.log")}
        assert alpha, f"pas de rapport {phase} pour alpha: {sorted(names)}"
        assert beta, f"pas de rapport {phase} pour beta: {sorted(names)}"

    # Et aucun rapport de phase ne doit rester SANS projet : ce serait le
    # gabarit d'avant, que le second projet écraserait.
    for phase in ("scan", "clean", "connect"):
        assert not [
            n for n in names if n.endswith(f"_{phase}.log") and "alpha" not in n and "beta" not in n
        ], f"gabarit non projeté survivant: {sorted(names)}"


def test_the_night_narrative_stays_a_single_file(tmp_path: Path) -> None:
    """§3.2 : le journal principal n'est PAS projeté.

    Il est ouvert en `tee -a`, c'est le récit unique de la nuit et la cible de
    l'alerte agrégée de §11. Le fragmenter par projet contredirait Q6.
    """
    _run(tmp_path, "alpha,beta")
    unprojected = [
        path.name for path in (tmp_path / "logs" / "dream").glob("*.log") if "_" not in path.name
    ]

    assert len(unprojected) == 1, f"le récit de la nuit s'est fragmenté: {unprojected}"


# --- §10 : l'allocation de retries est une ressource de nuit ---------------


def test_the_retry_budget_is_a_night_allocation_not_a_per_phase_one() -> None:
    """La forme, parce que l'effet demande une phase qui échoue vraiment.

    +43 min éligibles PAR PROJET, c'est +344 min de plafond à huit — la
    différence entre 7,7 h et 13,4 h de pire cas configuré.
    """
    source = DREAM_SH.read_text(encoding="utf-8")

    assert 'BRAIN_DREAM_RETRY_BUDGET="${BRAIN_DREAM_RETRY_BUDGET:-2}"' in source
    assert "RETRY_BUDGET_LEFT=$(( RETRY_BUDGET_LEFT - 1 ))" in source
    assert "(( RETRY_BUDGET_LEFT > 0 ))" in source
    # Le budget épuisé ne doit pas éteindre le signal : la phase garde son rc.
    assert "NO-RETRY" in source


def test_the_pool_order_rotates_so_the_same_project_is_not_always_last() -> None:
    """§10 : sans rotation, c'est toujours le même projet qui est sacrifié.

    Même idiome que roadmap_curate.rotate_keys, en service depuis 2026-07-04.
    """
    source = DREAM_SH.read_text(encoding="utf-8")

    assert "10#$(date +%j)" in source, (
        "la rotation doit forcer la base 10 : `date +%j` rend 001-366 et bash "
        "lirait 008 comme un octal invalide — une nuit qui casse 2 jours sur 366"
    )


# --- §3.4 : les exports ne survivent pas à l'itération ---------------------


def test_the_promote_exports_are_reset_per_project() -> None:
    """Un projet qui saute PROMOTE ne doit pas hériter du pool d'un autre.

    La remise à zéro est en TÊTE d'itération : les cinq `continue` du corps
    sauteraient un nettoyage placé en queue.
    """
    source = DREAM_SH.read_text(encoding="utf-8")
    body_start = source.index("run_project_phases() {")
    phase_loop = source.index('for phase_spec in "${PHASES[@]}"', body_start)
    preamble = source[body_start:phase_loop]

    assert "export PROMOTE_CANDIDATE_POOL_JSON='[]'" in preamble
    assert "export PROMOTE_RECENT_PROMOTIONS_JSON='[]'" in preamble


def test_the_pool_rotates_by_one_notch_per_day(tmp_path: Path) -> None:
    """Le témoin que la rotation n'avait pas, et dont l'absence coûtait cher.

    `dream.sh` fait tourner le pool d'un cran par jour — sans quoi le projet en
    queue est toujours celui qu'on sacrifie quand la nuit dépasse son plafond.
    Ce comportement n'était prouvé par AUCUN test dédié : sa seule trace était
    l'ordre asserté par deux tests voisins, qui le SUBISSAIENT au lieu de le
    vérifier. D'où leur rouge un jour sur deux.
    """
    _run(
        tmp_path,
        "brain-v42",
        extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha,beta,gamma", "BRAIN_DREAM_FAKE_DOY": "223"},
    )

    # 223 % 3 == 1 : le pool démarre sur son deuxième élément.
    assert "Pool (3) from BRAIN_DREAM_PROJECT_POOL: beta gamma alpha" in _main_log(tmp_path)


def test_the_rotation_serves_every_project_whatever_the_day(tmp_path: Path) -> None:
    """La rotation change l'ORDRE, jamais l'ENSEMBLE.

    Une rotation mal écrite — un décalage qui tronque au lieu de faire tourner —
    perdrait un projet par nuit sans que le compte annoncé le dise.
    """
    _run(
        tmp_path,
        "brain-v42",
        extra_env={"BRAIN_DREAM_PROJECT_POOL": "alpha,beta,gamma", "BRAIN_DREAM_FAKE_DOY": "223"},
    )
    log = _main_log(tmp_path)

    for project in ("alpha", "beta", "gamma"):
        assert f"--- Projet {project} ---" in log, f"{project} n'a pas été servi"
    assert log.count("--- Projet ") == 3


def test_no_test_asserts_a_multi_project_pool_order_without_pinning_the_day() -> None:
    """La garde ANTI-RÉCIDIVE, et c'est elle qui vaut le lot.

    Le défaut n'était pas dans `dream.sh` : la rotation quotidienne est correcte
    et voulue. Il était dans deux tests qui asseraient un ORDRE de pool sans
    fixer le jour, donc verts ou rouges selon la parité de `date +%j`. Mesuré le
    2026-08-11 (jour 223, rotation 1 sur un pool de 2) : rouges. La veille
    (jour 222, rotation 0) : verts. Un test vert un jour sur deux ne garde rien,
    et sa couleur ne dit rien du code.
    """
    source = Path(__file__).read_text(encoding="utf-8")

    offenders: list[str] = []
    for block in re.split(r"\ndef ", source):
        name = block.split("(", 1)[0].strip()
        if not name.startswith("test_"):
            continue
        # « Pool (1) » est insensible à la rotation : n % 1 vaut toujours 0.
        if re.search(r'"Pool \((?!1\))\d+\) from [^"]*: \w+ \w+', block) and (
            "BRAIN_DREAM_FAKE_DOY" not in block
        ):
            offenders.append(name)

    assert offenders == [], (
        "ces tests asserent l'ordre d'un pool de plusieurs projets sans fixer le "
        f"jour : ils seront verts ou rouges selon la date d'exécution — {offenders}"
    )
