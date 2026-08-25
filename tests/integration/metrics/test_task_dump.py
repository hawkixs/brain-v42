"""Le chien de garde de diagnostic doit TIRER, SURVIVRE, être BORNÉ, et NOMMER ses trous.

Un dump jamais déclenché est du code mort qui se lit comme une protection. Un
dump déclenché dans un canal que le processus jette en sortant l'est tout autant,
et c'est le défaut MESURÉ de la première rédaction : sous
``-o faulthandler_exit_on_timeout=true``, faulthandler sort par ``os._exit``,
pytest n'écrit jamais le rapport de l'item, et ``grep -c "DUMP TÂCHES"`` sur la
sortie complète du run rend **0**. La CI arme exactement cette configuration.

Ces tests-ci ne mesurent donc pas une mise en forme : ils mesurent que le dump
s'exécute pendant que la tâche est encore suspendue, qu'il EXISTE ENCORE après une
sortie brutale du processus, que son volume est majoré, et qu'une pile illisible
est DITE.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.metrics.task_dump import (
    DUMP_FILE_ENV,
    DUMP_FILE_NAME,
    MAX_LINES,
    MAX_TASKS,
    collect_probes,
    dump_tasks_after,
    durable_sink_paths,
    format_probe_report,
    format_task_dump,
)

pytestmark = pytest.mark.integration

#: Ce que le sous-processus de la preuve de survie doit dépenser au pire. Le
#: faulthandler y est armé à 3 s : au-delà de cette borne c'est le lanceur qui a
#: un problème, pas le cas mesuré.
_SURVIVAL_PROOF_BUDGET_SECONDS = 90.0


@pytest.fixture(autouse=True)
def _durable_sink_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Les dumps de CE module écrivent dans ``tmp_path``, pas dans les vrais puits.

    Plusieurs tests ici déclenchent un dump EXPRÈS. Sans cette redirection, ils
    appenderaient au journal partagé du runner et au résumé d'étape GitHub à
    chaque run vert : le canal qu'on vient de rendre durable deviendrait un bruit
    permanent, et un bruit permanent finit par être filtré par son lecteur.
    """
    sink = tmp_path / "dump.log"
    monkeypatch.setenv(DUMP_FILE_ENV, str(sink))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return sink


async def test_the_watchdog_fires_while_the_task_is_still_suspended() -> None:
    """Le dump tire PENDANT l'attente, et nomme la frame encore suspendue.

    C'est la différence avec un dump posé dans le ``except TimeoutError`` :
    ``asyncio.wait_for`` annule puis ATTEND l'annulation, donc là-bas
    ``Task.get_stack()`` d'une tâche terminée rend une liste vide.
    """
    stream = io.StringIO()
    never = asyncio.Event()

    async def waits_forever() -> None:
        await never.wait()

    victim = asyncio.create_task(waits_forever(), name="la-victime")
    try:
        with pytest.raises(TimeoutError):
            async with dump_tasks_after(0.05, label="témoin", stream=stream) as state:
                await asyncio.wait_for(asyncio.shield(victim), timeout=0.4)
        assert state.fired is True, "le chien de garde n'a pas tiré : diagnostic mort"
    finally:
        never.set()
        await asyncio.wait_for(victim, timeout=5)

    text = stream.getvalue()
    assert "témoin" in text
    assert "la-victime" in text
    # La frame suspendue est nommée, pas seulement la tâche.
    assert "waits_forever" in text
    assert "test_task_dump.py:" in text
    # Et la CHAÎNE, pas seulement la frame externe : `Task.get_stack()` d'une
    # coroutine suspendue ne rend QU'UNE frame. Sans le parcours `cr_await`, le
    # dump dirait « la tâche est dans waits_forever » — ce qu'on savait déjà —
    # et tairait le maillon qui attend réellement.
    assert "asyncio/locks.py:" in text, f"chaîne d'attente absente du dump:\n{text}"


async def test_the_watchdog_does_not_fire_or_survive_on_the_happy_path() -> None:
    """Chemin heureux : aucun dump, et AUCUNE tâche laissée vivante.

    Un chien de garde est une tâche de plus sur la même boucle. Non annulé, il
    recrée la tâche immortelle que ``asyncio.Runner.close()`` attend ensuite sans
    borne — le hang serait déplacé, pas fermé.
    """
    stream = io.StringIO()
    before = {t.get_name() for t in asyncio.all_tasks()}

    async with dump_tasks_after(30.0, label="jamais", stream=stream) as state:
        await asyncio.sleep(0)

    assert state.fired is False
    assert stream.getvalue() == ""
    leftover = {t.get_name() for t in asyncio.all_tasks() if not t.done()} - before
    assert leftover == set(), f"le chien de garde a survécu : {leftover}"


async def test_the_watchdog_never_fails_the_test_it_observes() -> None:
    """Une sonde cassée doit rendre ``# indisponible``, jamais masquer la panne."""
    stream = io.StringIO()

    class Explodes:
        def __getattr__(self, name: str) -> object:
            raise AttributeError(f"attribut privé disparu: {name}")

    async with dump_tasks_after(0.05, label="sonde-cassée", stream=stream, mcp=Explodes()):
        await asyncio.sleep(0.2)

    text = stream.getvalue()
    assert "indisponible" in text
    assert "attribut privé disparu" in text


def test_the_dump_is_capped_and_says_how_much_it_omitted() -> None:
    """Un log noyé est aussi inexploitable qu'un log absent : le budget est majoré."""

    class FakeTask:
        def __init__(self, index: int) -> None:
            self._index = index

        def get_name(self) -> str:
            return f"Task-{self._index:04d}"

        def done(self) -> bool:
            return False

        def cancelling(self) -> int:
            return 0

        def get_coro(self) -> object:
            return None

        def get_stack(self, limit: int | None = None) -> list[object]:
            raise RuntimeError("pile illisible pour ce faux")

    tasks = [FakeTask(i) for i in range(MAX_TASKS * 4)]
    text = format_task_dump(
        label="borné",
        deadline=25.0,
        elapsed=25.1,
        tasks=tasks,
        excluded=0,
        probes={},
    )
    lines = text.splitlines()
    assert len(lines) <= MAX_LINES, f"{len(lines)} lignes — le dump noie le log"
    assert f"{len(tasks) - MAX_TASKS} tâche(s) omise(s)" in text
    # Trou NOMMÉ, pas omis en silence.
    assert "pile illisible" in text


def test_a_task_without_a_recoverable_stack_is_named_not_dropped() -> None:
    """Une tâche sans pile récupérable DOIT apparaître, avec son trou déclaré."""

    class Finished:
        def get_name(self) -> str:
            return "déjà-terminée"

        def done(self) -> bool:
            return True

        def cancelling(self) -> int:
            return 0

        def get_coro(self) -> object:
            return None

        def get_stack(self, limit: int | None = None) -> list[object]:
            return []

    text = format_task_dump(
        label="trou",
        deadline=1.0,
        elapsed=1.0,
        tasks=[Finished()],
        excluded=2,
        probes={"uvicorn.server.started": "indisponible: pas de serveur"},
    )
    assert "déjà-terminée" in text
    assert "aucune frame récupérable" in text
    assert "uvicorn.server.started = indisponible: pas de serveur" in text


async def test_a_chain_that_ends_is_not_reported_as_truncated() -> None:
    """« tronquée » doit vouloir dire tronquée.

    La première rédaction posait la note dans le ``else`` du ``for`` : une chaîne
    qui se terminait pile au dernier maillon était annoncée coupée. Une borne qui
    ment sur elle-même se relit comme un maillon manquant qui n'existe pas.
    """
    stream = io.StringIO()
    async with dump_tasks_after(0.02, label="chaîne-courte", stream=stream):
        await asyncio.sleep(0.15)

    text = stream.getvalue()
    assert "tronquée" not in text.split("-- tâches")[0]
    # La tâche courante dort : sa chaîne tient en trois maillons et se termine.
    assert "asyncio/tasks.py" in text


def test_the_dump_survives_a_faulthandler_hard_exit(tmp_path: Path) -> None:
    """LA preuve : le dump existe encore APRÈS ``os._exit``.

    Rejoué du chemin de CI, pas d'un équivalent : un vrai pytest, avec
    ``faulthandler_timeout`` + ``exit_on_timeout``, sur un cas armé qui ne revient
    jamais. Sur la rédaction stderr-seule, ce même run rendait
    ``grep -c "DUMP TÂCHES"`` = **0** sur toute sa sortie — le diagnostic était
    muet précisément là où il sert. On n'assert PAS l'absence côté console : ce
    serait épingler le comportement de capture de pytest, qui n'est pas le contrat
    de ce module. Ce qu'on épingle est la présence côté fichier.
    """
    repo_root = Path(__file__).resolve().parents[3]
    sink = tmp_path / "durable" / "dump.log"
    case = tmp_path / "test_never_returns.py"
    case.write_text(
        "import asyncio\n"
        "from tests.integration.metrics.task_dump import dump_tasks_after\n"
        "\n"
        "\n"
        "async def _body() -> None:\n"
        "    never = asyncio.Event()\n"
        "    async with dump_tasks_after(0.2, label='preuve-os-exit'):\n"
        "        await never.wait()\n"
        "\n"
        "\n"
        "def test_never_returns() -> None:\n"
        "    asyncio.run(_body())\n",
        encoding="utf-8",
    )

    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(repo_root),
        "PYTHONIOENCODING": "utf-8",
        DUMP_FILE_ENV: str(sink),
    }
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            str(case),
            "-o",
            "faulthandler_timeout=3",
            "-o",
            "faulthandler_exit_on_timeout=true",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=_SURVIVAL_PROOF_BUDGET_SECONDS,
        check=False,
    )

    console = completed.stdout + completed.stderr
    # Le processus est bien sorti PAR le filet, sinon la preuve porte sur un
    # autre chemin que celui de la CI.
    assert "Timeout (0:00:03)!" in console, f"faulthandler n'a pas tiré:\n{console[-2000:]}"
    assert completed.returncode != 0

    assert sink.exists(), (
        "aucune copie durable : le dump n'existe que dans un tampon que "
        f"`os._exit` a jeté\n{console[-2000:]}"
    )
    durable = sink.read_text(encoding="utf-8")
    assert "DUMP TÂCHES ASYNCIO — preuve-os-exit" in durable
    # Et il porte le maillon utile, pas seulement le titre : le parcours
    # `cr_await` est le coeur du livrable, la survie ne vaut rien sans lui.
    assert "asyncio/locks.py:" in durable, f"chaîne d'attente absente:\n{durable}"
    assert "item:" in durable and "horodatage:" in durable


def test_the_default_sink_lands_under_runner_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le puits par défaut est le scratch du runner, et l'explicite le remplace."""
    monkeypatch.delenv(DUMP_FILE_ENV, raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    assert durable_sink_paths() == [tmp_path / "runner" / DUMP_FILE_NAME]

    monkeypatch.setenv(DUMP_FILE_ENV, str(tmp_path / "explicite.log"))
    assert durable_sink_paths() == [tmp_path / "explicite.log"]

    # Le résumé d'étape s'AJOUTE : il porte la visibilité, pas la durabilité,
    # et il n'existe que sous GitHub Actions.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    assert durable_sink_paths() == [tmp_path / "explicite.log", tmp_path / "summary.md"]


async def test_an_unreachable_sink_is_named_and_never_fails_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un puits injoignable est DIT dans le dump, jamais tu, jamais levé.

    Le canal durable est du code qui tourne pendant une panne. S'il levait, il
    remplacerait le diagnostic par le sien.
    """
    blocker = tmp_path / "pas-un-dossier"
    blocker.write_text("je suis un fichier", encoding="utf-8")
    monkeypatch.setenv(DUMP_FILE_ENV, str(blocker / "dump.log"))

    stream = io.StringIO()
    async with dump_tasks_after(0.05, label="puits-mort", stream=stream) as state:
        await asyncio.sleep(0.2)

    assert state.fired is True
    text = stream.getvalue()
    assert "DUMP TÂCHES" in text
    assert f"copie durable indisponible ({blocker / 'dump.log'})" in text


class _FakeMcp:
    """Un singleton FastMCP crédible : toutes les sondes MCP y réussissent.

    Le contraire du ``Explodes`` ci-dessus. Ici on ne mesure pas la robustesse
    d'une sonde cassée, on mesure QUELLES sondes sont relevées — un faux qui lève
    rendrait « indisponible » et masquerait une sonde qu'on croit retirée.
    """

    _lifespan_ref_count = 0
    _lifespan_result_set = False
    _lifespan_result = None
    _lifespan_ref_count_should_not_be_read = True

    class _Started:
        @staticmethod
        def is_set() -> bool:
            return False

    _started = _Started()


def test_the_refuted_lifespan_probes_are_no_longer_collected() -> None:
    """Les TROIS sondes lifespan sont RÉFUTÉES PAR LE CODE, donc retirées.

    ``mcp = FastMCP("brain", mask_error_details=True)`` (server.py) est construit
    SANS lifespan : ``_lifespan_proxy`` (fastmcp/server/server.py:264-266) rend
    ``{}`` et SORT avant de lire ``_lifespan_ref_count``, ``_lifespan_result_set``
    ou ``_lifespan_result``. Aucune des trois ne peut influencer une requête, donc
    aucune ne peut expliquer une panne. C'est du bruit qui se lit comme de
    l'information dans un dump qu'on ne relit qu'en panne.
    """
    probes = collect_probes(mcp=_FakeMcp())

    assert "mcp._lifespan_ref_count" not in probes
    assert "mcp._lifespan_result_set" not in probes
    # `_lifespan_result` tombe avec les deux autres, et pour la même raison : le
    # proxy sort avant de la lire. Elle varie (`None`/`{}`), mais c'est exactement
    # ce que dit `_started.is_set()` — un doublon causalement inerte n'est pas une
    # mesure. `_FakeMcp` la PORTE, donc ce test échouerait si on la remettait.
    assert "mcp._lifespan_result is None" not in probes
    assert "mcp._started.is_set()" in probes


def test_the_sse_exit_latch_is_probed_and_named_apart_from_uvicorns() -> None:
    """DEUX ``should_exit`` distincts, et la sortie ne doit pas les confondre.

    ``sse_starlette.sse.AppStatus.should_exit`` est un attribut de CLASSE, donc
    GLOBAL AU PROCESSUS et jamais remis à ``False``; il arme la seule sortie
    immédiate et muette de ``_listen_for_exit_signal``. ``server.should_exit`` est
    l'instance uvicorn de CE banc. Les lire pour la même variable annulerait la
    mesure, donc leurs clés doivent être lisibles séparément.
    """

    class _FakeUvicorn:
        started = True
        should_exit = False

        class _State:
            tasks: set[object] = set()

        server_state = _State()

    probes = collect_probes(mcp=_FakeMcp(), server=_FakeUvicorn())

    latch = [name for name in probes if "AppStatus.should_exit" in name]
    instance = [
        name for name in probes if "should_exit" in name and "AppStatus.should_exit" not in name
    ]
    assert len(latch) == 1, f"sonde du latch SSE absente ou dupliquée: {sorted(probes)}"
    assert len(instance) == 1, f"sonde uvicorn absente ou dupliquée: {sorted(probes)}"
    assert latch != instance

    # Rendues, les deux lignes doivent rester distinguables à l'oeil nu.
    text = format_probe_report(label="deux-latches", probes=probes)
    assert "GLOBAL processus" in text
    assert "INSTANCE locale du banc" in text
    # Et la valeur réellement lue, pas un placeholder.
    assert probes[latch[0]].endswith(("True", "False")), probes[latch[0]]


def test_the_sse_latch_probe_is_named_unavailable_when_the_module_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sse_starlette`` peut ne pas être importable : la sonde DIT, ne lève pas.

    L'import est tardif et défensif. Une ``ImportError`` doit rendre une ligne
    « indisponible », jamais faire échouer le dump — un diagnostic qui meurt de
    son annexe remplace la panne observée par la sienne.
    """
    monkeypatch.setitem(sys.modules, "sse_starlette.sse", None)

    probes = collect_probes()

    (latch,) = [name for name in probes if "AppStatus.should_exit" in name]
    assert "indisponible" in probes[latch]
    # La FAMILLE, pas la classe exacte : un module absent lève
    # `ModuleNotFoundError`, sous-classe d'`ImportError`. Épingler le nom littéral
    # « ImportError » ferait échouer ce test sur le cas le plus banal de son propre
    # scénario.
    assert "Error" in probes[latch] and "sse_starlette" in probes[latch]


def test_the_sse_latch_probe_is_named_unavailable_when_the_attribute_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AppStatus.should_exit`` est un attribut PRIVÉ d'amont : il peut disparaître.

    Même contrat que pour l'import manquant, autre exception : une
    ``AttributeError`` doit rendre une ligne « indisponible », jamais tuer le dump.
    """
    from sse_starlette.sse import AppStatus

    monkeypatch.delattr(AppStatus, "should_exit", raising=True)

    probes = collect_probes()

    (latch,) = [name for name in probes if "AppStatus.should_exit" in name]
    assert "indisponible" in probes[latch]
    assert "AttributeError" in probes[latch]


def test_the_entry_witness_reads_the_latch_before_the_bench_touches_anything() -> None:
    """Le relevé d'entrée est la mesure qui tranche : il doit être PREMIER.

    ``True`` à l'entrée du banc metrics = un module antérieur a laissé le latch
    armé (``tests/integration/mcp/**`` est collecté avant : « mcp » < « metrics »),
    et la cause est établie. ``False`` tue l'hypothèse proprement. Le relevé ne
    vaut que s'il est pris AVANT que ce banc ne construise quoi que ce soit :
    déplacé sous ``build_services()``, il mesurerait l'état que le banc vient
    lui-même de produire et ne pourrait plus attribuer le latch à personne.

    Épinglé sur le TEXTE parce que l'ordre est la seule chose qui compte ici et
    qu'aucune exécution ne peut le prouver sans monter le banc entier.
    """
    source = (Path(__file__).parent / "test_agent_attribution.py").read_text(encoding="utf-8")

    witness = source.index("collect_probes(mcp=mcp)")
    build = source.index("services = build_services()")
    assert witness < build, "le témoin d'entrée est passé APRÈS le montage du banc"
    # Et il reste un RELEVÉ : aucune assertion sur le latch, tant qu'on ne sait
    # pas ce que la valeur vaut en pratique. Asserter ici convertirait un
    # coin-flip en rouge franc sans qu'on sache pourquoi.
    between = source[witness:build]
    asserting = [line for line in between.splitlines() if line.strip().startswith("assert ")]
    assert asserting == [], f"le témoin d'entrée assert au lieu de relever: {asserting}"
