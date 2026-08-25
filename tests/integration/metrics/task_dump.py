"""Nommer QUI attend, quand le banc metrics pend.

Ce module n'existe que parce que le seul filet armé sur ce dépôt ne peut pas
répondre à la question. Deux mesures, pas un raisonnement :

1. **Le filet est injoignable — mais l'arithmétique qui le disait était FAUSSE.**
   ``pyproject.toml`` arme ``faulthandler_timeout = 120`` / ``exit_on_timeout``,
   et le plugin l'arme sur le PROTOCOLE d'un item (setup + call + teardown), pas
   sur son corps. La première rédaction annonçait « 60 s de corps + 40 s de
   teardown = 100 s, sous les 120 » : elle oubliait le démarrage du serveur, et
   surtout que le corps porte DEUX attentes bornées CONSÉCUTIVES. 5 + 60 + 60 + 40
   franchit les 120 sans rien de pathologique, et le runner est mesuré 22 % plus
   lent le 2026-08-25 (148 s contre 120,9 s sur la même suite). Les budgets de
   ``test_agent_attribution.py`` sont donc redescendus : 5 + 25 + 25 + 21 = 76 s
   au pire, soit 93 s à +22 %, contre un filet à 120. La borne applicative
   regagne la course, au lieu de le prétendre. Le seul run qui a jamais atteint
   120 s (32779161805) est celui qui n'avait aucune borne dans le corps du test.
2. **Et même s'il tirait, il ne verrait rien d'utile.**
   ``faulthandler.dump_traceback_later`` dumpe les THREADS. La coroutine coupable
   n'est sur aucune pile de thread : elle est SUSPENDUE dans
   ``client.py:571 ready_event.wait()``, ses frames vivent dans l'objet ``Task``,
   et le MainThread n'affiche que ``run_forever → _run_once → epoll.poll``. Le
   seul lecteur de ces frames est ``Task.get_stack()``, que faulthandler
   n'appelle jamais.

D'où le résultat mesuré : on sait QUE ça bloque, jamais QUI.

Trois règles de conception, chacune payée par un incident de ce fichier voisin :

- **Le chien de garde tire PENDANT l'attente.** Posé dans un
  ``except TimeoutError``, il serait du code mort sur son propre chemin :
  ``asyncio.wait_for`` annule le gather, ATTEND l'annulation, PUIS lève — les
  tâches sont alors terminées et ``Task.get_stack()`` rend une liste vide.
- **Il n'annule jamais rien** et il ne fait jamais échouer le test qu'il observe.
  Un run vert avec un dump reste vert : le dump devient la mesure du dérapage.
- **Il écrit dans un FICHIER, pas seulement sur stderr.** C'est le défaut qui
  rendait ce module inutile sur exactement le chemin pour lequel il est écrit, et
  il est MESURÉ, pas déduit :
  ``pytest -o faulthandler_timeout=2 -o faulthandler_exit_on_timeout=true`` sur un
  cas armé, puis ``grep -c "DUMP TÂCHES"`` sur la sortie complète du run → **0**.
  faulthandler sort par ``os._exit`` ; pytest n'écrit jamais le rapport de l'item
  et le tampon de capture part avec le processus. Mesuré aussi, parce que c'était
  la sortie de secours évidente : ``os.write(2, ...)`` ne s'échappe pas non plus,
  la capture de pytest est au niveau du DESCRIPTEUR. Un fichier écrit puis FLUSHÉ
  survit, lui — les octets sont déjà chez le noyau quand ``os._exit`` tombe.
  stderr reste écrit, pour le chemin rouge normal où pytest rend
  ``Captured stderr call`` ; il n'est plus le seul canal. Un dump ne devient
  jamais un échec.
- **Il meurt sur le chemin heureux.** Un chien de garde est une tâche de plus sur
  la même boucle ; non annulé, il recrée la tâche immortelle que
  ``asyncio.Runner.close()`` attend ensuite sans borne. Le hang serait déplacé,
  pas fermé.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

__all__ = [
    "DUMP_FILE_ENV",
    "DUMP_FILE_NAME",
    "MAX_LINES",
    "MAX_TASKS",
    "DumpState",
    "dump_tasks_after",
    "durable_sink_paths",
    "format_probe_report",
    "format_task_dump",
    "write_durable_copy",
]

#: Bornes du dump. Sur un runner chargé, ``asyncio.all_tasks()`` d'un banc HTTP +
#: uvicorn + SQLAlchemy rend plusieurs dizaines de tâches : le budget est MAJORÉ
#: et connu, pas espéré.
MAX_TASKS = 40
STACK_FRAME_LIMIT = 8
#: MESURÉ : pour une coroutine SUSPENDUE, ``Task.get_stack()`` ne rend QU'UNE
#: frame — la plus externe. Sur ce banc elle dit ``visible_tools:292``, ce qu'on
#: savait déjà. Le maillon utile (``client.py:571 ready_event.wait()``) vit plus
#: bas, dans la chaîne ``cr_await``, que seul un parcours explicite atteint.
AWAIT_CHAIN_LIMIT = 12
MAX_LINES = 300

#: Budget de jointure du chien de garde sur le chemin heureux.
_JOIN_BUDGET_SECONDS = 5.0

_UNAVAILABLE = "indisponible"

#: Le canal DURABLE. `BRAIN_TEST_TASK_DUMP_FILE` surcharge le chemin ; par défaut
#: le dump atterrit sous `$RUNNER_TEMP` (le scratch du runner GitHub), avec repli
#: sur le temporaire local pour qu'un run de poste ait le MÊME canal — un canal
#: qu'on ne peut pas exercer chez soi n'est pas un canal.
DUMP_FILE_ENV = "BRAIN_TEST_TASK_DUMP_FILE"
DUMP_FILE_NAME = "brain-v42-task-dump.log"
_RUNNER_TEMP_ENV = "RUNNER_TEMP"
#: Second puits, et il porte la VISIBILITÉ : rien n'uploade `$RUNNER_TEMP` dans ce
#: dépôt, donc un dump qui n'existe que là est durable et invisible. GitHub rend
#: `$GITHUB_STEP_SUMMARY` sur la page du run sans qu'aucun workflow ne change,
#: et c'est un simple fichier — donc il survit à `os._exit` comme l'autre.
_STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"
#: Le résumé d'étape est du Markdown : hors clôture, un dump de piles y serait
#: reflué en un seul paragraphe. La même clôture sert de séparateur dans le
#: journal brut, qui est en APPEND et mêle les items.
_FENCE = "```"


@dataclass
class DumpState:
    """Ce que l'appelant peut mesurer d'un armement : a-t-il tiré, une seule fois."""

    fired: bool = False


# ---------------------------------------------------------------------------
# Mise en forme — pure, donc testable sans boucle ni serveur
# ---------------------------------------------------------------------------


def _short_path(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else path


def _describe_coro(task: Any) -> str:
    """Rendre le coro d'une tâche, ou DIRE qu'on n'y a pas eu accès."""
    try:
        coro = task.get_coro()
    except Exception as exc:  # noqa: BLE001 — une sonde ne lève jamais
        return f"coro {_UNAVAILABLE}: {exc!r}"
    if coro is None:
        return f"coro {_UNAVAILABLE}: get_coro() a rendu None"
    name = getattr(coro, "__qualname__", None) or getattr(coro, "__name__", None) or repr(coro)
    frame = getattr(coro, "cr_frame", None)
    if frame is None:
        return f"coro={name} (frame courante {_UNAVAILABLE})"
    code = getattr(frame, "f_code", None)
    filename = _short_path(getattr(code, "co_filename", "?"))
    return f"coro={name} ({filename}:{getattr(frame, 'f_lineno', '?')})"


def _describe_stack(task: Any) -> list[str]:
    """Rendre les frames suspendues, en NOMMANT chaque trou plutôt qu'en l'omettant."""
    try:
        frames = task.get_stack(limit=STACK_FRAME_LIMIT)
    except Exception as exc:  # noqa: BLE001
        return [f"<pile illisible: {exc!r}>"]
    if not frames:
        return ["<aucune frame récupérable: tâche terminée, annulée ou non démarrée>"]
    lines: list[str] = []
    for frame in frames:
        try:
            code = frame.f_code
            lines.append(f"{_short_path(code.co_filename)}:{frame.f_lineno} in {code.co_name}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"<frame illisible: {exc!r}>")
    return lines


def _describe_await_chain(task: Any) -> list[str]:
    """Descendre la chaîne ``cr_await`` — le seul chemin vers le maillon qui attend.

    Chaque maillon est rendu, y compris ceux qu'on ne sait pas lire : une attente
    non-coroutine (Future, primitive anyio) est NOMMÉE par son type plutôt
    qu'omise, sinon la chaîne s'arrêterait en silence exactement là où elle
    devient intéressante.
    """
    try:
        node = task.get_coro()
    except Exception as exc:  # noqa: BLE001
        return [f"<chaîne d'attente illisible: {exc!r}>"]

    lines: list[str] = []
    seen: set[int] = set()
    truncated = True
    for _hop in range(AWAIT_CHAIN_LIMIT):
        if node is None:
            truncated = False
            break
        if id(node) in seen:
            lines.append("<cycle détecté dans la chaîne d'attente>")
            break
        seen.add(id(node))
        try:
            frame = getattr(node, "cr_frame", None) or getattr(node, "gi_frame", None)
            if frame is not None:
                code = frame.f_code
                lines.append(
                    f"↳ {_short_path(code.co_filename)}:{frame.f_lineno} in {code.co_name}"
                )
            else:
                lines.append(f"↳ <attente non-coroutine: {type(node).__name__} {repr(node)[:100]}>")
            node = (
                getattr(node, "cr_await", None)
                or getattr(node, "gi_yieldfrom", None)
                or getattr(node, "ag_await", None)
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"<maillon illisible: {exc!r}>")
            truncated = False
            break
    if truncated:
        # Dire qu'on s'arrête AVANT la fin est le contraire d'omettre : sans
        # cette ligne, une chaîne coupée se lit comme une chaîne complète.
        lines.append(f"<chaîne tronquée à {AWAIT_CHAIN_LIMIT} maillons>")

    if not lines:
        return ["<aucune chaîne d'attente récupérable: coro absent>"]
    return lines


def _sort_key(task: Any) -> tuple[int, str]:
    try:
        finished = 1 if task.done() else 0
    except Exception:  # noqa: BLE001
        finished = 0
    try:
        name = str(task.get_name())
    except Exception as exc:  # noqa: BLE001
        name = f"<nom {_UNAVAILABLE}: {exc!r}>"
    return (finished, name)


def format_task_dump(
    *,
    label: str,
    deadline: float,
    elapsed: float,
    tasks: Iterable[Any],
    excluded: int,
    probes: Mapping[str, str] | None = None,
    context: Mapping[str, str] | None = None,
) -> str:
    """Rendre le dump borné. Aucune E/S, aucune boucle : testable en unitaire."""
    ordered = sorted(tasks, key=_sort_key)
    shown = ordered[:MAX_TASKS]
    omitted = len(ordered) - len(shown)

    lines: list[str] = [
        f"=== DUMP TÂCHES ASYNCIO — {label} ===",
    ]
    # Le journal durable est en APPEND et partagé par tous les items du run :
    # sans horodatage ni nodeid, deux dumps se relisent comme un seul. Ces
    # lignes sont DANS la liste bornée, pas au-dessus : la borne MAX_LINES
    # resterait sinon un majorant de tout sauf de l'en-tête.
    if context:
        lines.extend(f"{name}: {value}" for name, value in context.items())
    lines += [
        f"deadline armée: {deadline:.2f} s ; écoulé depuis l'armement: {elapsed:.2f} s",
        (
            f"tâches vues: {len(ordered)} ; examinées: {len(shown)} ; "
            f"{omitted} tâche(s) omise(s) par la borne MAX_TASKS={MAX_TASKS} ; "
            f"{excluded} exclue(s) (tâche courante + chien de garde)"
        ),
        "-- sondes --",
    ]
    if probes:
        lines.extend(f"{name} = {value}" for name, value in probes.items())
    elif probes is None:
        lines.append("(différées: second enregistrement — voir SONDES ci-dessous)")
    else:
        lines.append("(aucune sonde fournie à cet armement)")

    lines.append("-- tâches (non terminées d'abord, puis par nom) --")
    for index, task in enumerate(shown, start=1):
        finished, name = _sort_key(task)
        try:
            cancelling = task.cancelling()
        except Exception as exc:  # noqa: BLE001
            cancelling = f"{_UNAVAILABLE}: {exc!r}"  # type: ignore[assignment]
        lines.append(f"[{index}] name={name!r} done={bool(finished)} cancelling={cancelling}")
        lines.append(f"    {_describe_coro(task)}")
        stack = _describe_stack(task)
        chain = _describe_await_chain(task)
        # Le premier maillon de la chaîne REDIT la frame externe rendue par
        # get_stack(). Une ligne par tâche, quarante tâches : le budget se dépense
        # à répéter ce qu'on vient de lire.
        if stack and chain and chain[0] == f"↳ {stack[-1]}":
            chain = chain[1:]
        lines.extend(f"    {frame}" for frame in stack)
        lines.extend(f"    {link}" for link in chain)

    if len(lines) > MAX_LINES:
        kept = lines[: MAX_LINES - 1]
        kept.append(f"... {len(lines) - len(kept)} ligne(s) tronquée(s) par MAX_LINES={MAX_LINES}")
        lines = kept
    return "\n".join(lines)


def format_probe_report(*, label: str, probes: Mapping[str, str]) -> str:
    """Rendre les sondes SÉPARÉMENT du corps du dump.

    Séparées parce qu'elles ne coûtent pas la même chose. Le corps du dump ne
    fait aucun import ; `collect_probes` en fait quatre, paresseux (`fastmcp`,
    `brain_v42.db.engine`). MESURÉ le 2026-08-25 : sous
    `-o faulthandler_timeout=0.5`, le chien de garde a été tué PENDANT ces
    imports — sortie 139 et dump de faulthandler tronqué en plein
    `<frozen posixpath>` — donc AVANT toute écriture. Un seul enregistrement
    faisait dépendre le noyau du diagnostic du coût de son annexe.
    """
    lines = [f"=== SONDES — {label} ==="]
    lines.extend(f"{name} = {value}" for name, value in probes.items())
    if len(lines) > MAX_LINES:
        kept = lines[: MAX_LINES - 1]
        kept.append(f"... {len(lines) - len(kept)} ligne(s) tronquée(s) par MAX_LINES={MAX_LINES}")
        lines = kept
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sondes — chacune isolée : une sonde cassée ne masque JAMAIS la vraie panne
# ---------------------------------------------------------------------------


#: Les DEUX `should_exit`, épelés pour être distinguables à l'oeil nu dans la
#: sortie. Ce sont deux variables différentes : la première est un attribut de
#: classe de `sse_starlette`, partagé par tout le processus et jamais remis à
#: False ; la seconde est un champ de l'instance `uvicorn.Server` du banc.
_SSE_LATCH_PROBE = "sse_starlette.sse.AppStatus.should_exit [GLOBAL processus]"
_UVICORN_EXIT_PROBE = "uvicorn.Server.should_exit [INSTANCE locale du banc]"


def _sse_exit_latch() -> str:
    """Lire le latch de sortie de `sse_starlette`, sans exiger qu'il soit importé."""
    from sse_starlette.sse import AppStatus

    return str(AppStatus.should_exit)


def _probe(probes: dict[str, str], name: str, read: Any) -> None:
    try:
        probes[name] = str(read())
    except Exception as exc:  # noqa: BLE001
        probes[name] = f"{_UNAVAILABLE}: {exc!r}"


def collect_probes(*, mcp: Any = None, server: Any = None) -> dict[str, str]:
    """Relever l'état du singleton partagé, du serveur local et du moteur.

    Aucune requête SQL : une attente réseau dans un chien de garde serait un
    second point de pendaison.
    """
    probes: dict[str, str] = {}

    # EN PREMIER, parce que c'est la sonde qui tranche.
    #
    # `AppStatus.should_exit` est un attribut de CLASSE : GLOBAL AU PROCESSUS, et
    # JAMAIS remis à False. Il arme `sse_starlette/sse.py:311-313
    # _listen_for_exit_signal`, seule sortie immédiate et MUETTE des quatre tâches
    # de `EventSourceResponse.__call__` — la forme même de la panne observée, où le
    # serveur envoie les en-têtes SSE puis REVIENT SANS CORPS, client encore
    # connecté (`ASGI callable returned without completing response`, h11 exigeant
    # `disconnected=False`). Le latch est posé par une veille qui sonde
    # `uvicorn_server.should_exit` toutes les 0,5 s depuis un état
    # `threading.local()` partagé par TOUTES les boucles pytest-asyncio : un module
    # antérieur peut donc l'avoir armé pour tous les suivants.
    #
    # Import TARDIF et défensif : `sse_starlette` peut ne pas être importé, et
    # `_probe` transforme ImportError comme AttributeError en ligne
    # « indisponible » — un diagnostic ne meurt jamais de son annexe.
    _probe(probes, _SSE_LATCH_PROBE, _sse_exit_latch)

    if mcp is None:
        probes["mcp"] = f"{_UNAVAILABLE}: aucun serveur FastMCP passé à l'armement"
    else:
        # Attributs PRIVÉS de fastmcp (server/mixins/lifespan.py) : susceptibles
        # de bouger en version, donc chacun sous sa propre sonde.
        #
        # LES TROIS sondes de lifespan ont été RETIRÉES d'ici — `_lifespan_ref_count`,
        # `_lifespan_result_set` et `_lifespan_result` — pour la MÊME raison, qui les
        # réfute toutes les trois d'un coup : `mcp = FastMCP("brain",
        # mask_error_details=True)` (server.py) est construit SANS lifespan, donc
        # `_lifespan_proxy` (fastmcp/server/server.py:264-266) rend `{}` et SORT avant
        # de lire quoi que ce soit. Aucune des trois ne peut influencer une requête.
        # `_lifespan_result` alterne bien `None`/`{}`, mais c'est exactement ce que
        # `_started.is_set()` dit déjà, en mieux : elle n'ajoutait qu'un doublon
        # causalement inerte. Une sonde qui ne peut rien expliquer se relit comme de
        # l'information dans un dump qu'on n'ouvre qu'en panne.
        _probe(probes, "mcp._started.is_set()", lambda: mcp._started.is_set())

    def _session_manager() -> str:
        from fastmcp.server.http import StreamableHTTPSessionManager

        return repr(StreamableHTTPSessionManager)

    _probe(probes, "fastmcp.server.http.StreamableHTTPSessionManager", _session_manager)

    if server is None:
        probes["uvicorn"] = f"{_UNAVAILABLE}: aucun serveur uvicorn passé à l'armement"
    else:
        _probe(probes, "uvicorn.server.started", lambda: server.started)
        # Nommée INSTANCE, parce qu'il y a maintenant DEUX `should_exit` dans ce
        # dump et que les confondre annulerait la mesure : celui-ci est l'objet
        # `uvicorn.Server` de CE banc, celui-là est un global de processus.
        _probe(probes, _UVICORN_EXIT_PROBE, lambda: server.should_exit)
        # Mesure DIRECTE des « requêtes en vol » que l'erreur ASGI ne fait
        # qu'inférer à l'arrêt.
        _probe(probes, "len(server.server_state.tasks)", lambda: len(server.server_state.tasks))

    def _engine_present() -> str:
        import brain_v42.db.engine as engine_module

        return repr(engine_module._engine is not None)

    def _pool_status() -> str:
        import brain_v42.db.engine as engine_module

        engine = engine_module._engine
        if engine is None:
            return f"{_UNAVAILABLE}: aucun moteur installé"
        return str(engine.pool.status())

    _probe(probes, "db.engine._engine is not None", _engine_present)
    _probe(probes, "db.engine pool.status()", _pool_status)

    return probes


# ---------------------------------------------------------------------------
# Le canal durable — la seule sortie qui existe encore après `os._exit`
# ---------------------------------------------------------------------------


def durable_sink_paths() -> list[Path]:
    """Où le dump doit exister APRÈS la sortie brutale du processus.

    Deux puits, pour deux lecteurs, et aucun des deux n'est un buffer :

    - un fichier — `BRAIN_TEST_TASK_DUMP_FILE` s'il est posé, sinon
      `$RUNNER_TEMP/brain-v42-task-dump.log`, avec repli sur le temporaire local
      pour qu'un run de poste écrive dans le même canal que la CI ;
    - `$GITHUB_STEP_SUMMARY` quand il existe, parce que le premier fichier est
      durable mais INVISIBLE : rien n'uploade `$RUNNER_TEMP` dans ce dépôt, et
      ajouter cette étape touche le workflow, hors surface de ce lot. Le résumé
      d'étape, lui, est rendu par GitHub sur la page du run tel quel.
    """
    explicit = os.environ.get(DUMP_FILE_ENV, "").strip()
    base = os.environ.get(_RUNNER_TEMP_ENV, "").strip() or tempfile.gettempdir()
    paths = [Path(explicit) if explicit else Path(base) / DUMP_FILE_NAME]
    summary = os.environ.get(_STEP_SUMMARY_ENV, "").strip()
    if summary:
        paths.append(Path(summary))
    return paths


def write_durable_copy(text: str, *, paths: Iterable[Path] | None = None) -> list[str]:
    """Écrire le dump dans chaque puits, FLUSHÉ, et rendre ce qui s'est passé.

    Le flush EST le mécanisme : il rend les octets au noyau, donc le fichier
    porte le dump même quand le processus sort ensuite par `os._exit`, qui ne
    déroule ni `atexit` ni les tampons Python. Pas de `fsync` : ce qui tue ce
    diagnostic est une sortie brutale de processus, pas une coupure de courant —
    prétendre couvrir la seconde coûterait une E/S synchrone dans un chien de
    garde.

    Ne lève jamais et ne fait jamais échouer le test observé. Un puits
    injoignable est NOMMÉ dans le dump lui-même, jamais tu.
    """
    notes: list[str] = []
    for path in durable_sink_paths() if paths is None else paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{_FENCE}\n{text}\n{_FENCE}\n")
                handle.flush()
            notes.append(f"copie durable écrite: {path}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"copie durable {_UNAVAILABLE} ({path}): {exc!r}")
    return notes


# ---------------------------------------------------------------------------
# Le chien de garde
# ---------------------------------------------------------------------------


async def _watch(
    deadline: float,
    *,
    label: str,
    state: DumpState,
    mcp: Any,
    server: Any,
    stream: TextIO | None,
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.sleep(deadline)
    except asyncio.CancelledError:
        # Chemin heureux : l'attente observée est revenue avant la deadline.
        return

    # UNE SEULE FOIS par armement : après ce bloc la tâche se termine.
    #
    # L'ORDRE de ce bloc est le fix, pas un détail de style. La course est contre
    # un `os._exit` déclenché par faulthandler : ce qui est écrit et flushé avant
    # existe, le reste n'a jamais existé. Donc le NOYAU d'abord — les tâches et
    # leurs chaînes d'attente, zéro import — écrit sur disque, et seulement
    # ensuite les sondes, qui coûtent des imports paresseux et sous lesquelles le
    # chien de garde s'est fait tuer en vol lors de la mesure du 2026-08-25.
    try:
        me = asyncio.current_task()
        watched = [t for t in asyncio.all_tasks() if t is not me]
        excluded = 1 if me is not None else 0
        core = format_task_dump(
            label=label,
            deadline=deadline,
            elapsed=loop.time() - started,
            tasks=watched,
            excluded=excluded,
            context={
                "horodatage": datetime.now(tz=UTC).isoformat(timespec="seconds"),
                "item": os.environ.get("PYTEST_CURRENT_TEST", _UNAVAILABLE),
                "pid": str(os.getpid()),
            },
        )
    except Exception as exc:  # noqa: BLE001
        core = f"=== DUMP TÂCHES ASYNCIO — {label} ===\n# dump {_UNAVAILABLE}: {exc!r}"
    notes = write_durable_copy(core)

    try:
        annex = format_probe_report(label=label, probes=collect_probes(mcp=mcp, server=server))
    except Exception as exc:  # noqa: BLE001
        annex = f"=== SONDES — {label} ===\n# sondes {_UNAVAILABLE}: {exc!r}"
    notes += write_durable_copy(annex)

    print(
        "\n".join([core, annex, *(f"# {note}" for note in notes)]),
        file=stream if stream is not None else sys.stderr,
        flush=True,
    )
    state.fired = True


@asynccontextmanager
async def dump_tasks_after(
    deadline: float,
    *,
    label: str,
    mcp: Any = None,
    server: Any = None,
    stream: TextIO | None = None,
) -> AsyncIterator[DumpState]:
    """Armer un chien de garde qui DUMPE les tâches si le corps dépasse ``deadline``.

    Il n'annule rien, ne lève rien et ne change pas le verdict du test. Sur le
    chemin heureux il est annulé dans le ``finally`` — sinon on recrée la tâche
    immortelle qui pend à la fermeture de la boucle.
    """
    state = DumpState()
    watchdog = asyncio.create_task(
        _watch(deadline, label=label, state=state, mcp=mcp, server=server, stream=stream),
        name=f"dump-tasks-after[{label}]",
    )
    try:
        yield state
    finally:
        watchdog.cancel()
        with suppress(asyncio.CancelledError, TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(watchdog), timeout=_JOIN_BUDGET_SECONDS)
