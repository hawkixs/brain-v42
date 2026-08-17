"""Spans OpenTelemetry pour les appels de tool — sur un provider PRIVÉ.

POURQUOI JAMAIS ``trace.set_tracer_provider()``. Vérifié dans le venv le
2026-08-12 : FastMCP porte sa propre télémétrie. ``fastmcp/server/telemetry.py``
(``server_span``, ligne 57) fait, sur échec ::

    span.record_exception(e)                           # args + stacktrace
    span.set_status(Status(StatusCode.ERROR, str(e)))  # message brut

et pose ``enduser.id`` — le principal du bearer de capacité Dream — via
``get_auth_span_attributes()`` (ligne 85). Son tracer vient de
``fastmcp/telemetry.py::get_tracer`` (ligne 38), qui appelle
``otel_get_tracer(INSTRUMENTATION_NAME)`` : il lit le provider **GLOBAL**.

Installer un provider global armerait donc ce bloc. Or ``business_errors.py:101``
fait ``raise ToolError(str(exc)) from exc`` : ``str(e)`` EST le message métier
brut, et ``record_exception`` sérialise une stacktrace qui suit ``__cause__``.
Le secret que ``test_decorator_does_not_log_authorization_failure_context``
existe pour retenir ressortirait par ce canal. D'où un provider privé, épinglé
par un test qui lit cette source.

POURQUOI LE SDK N'EST IMPORTÉ QU'À L'INTÉRIEUR DES FONCTIONS. C'est une
dépendance OPTIONNELLE (extra ``tracing``). Un import au niveau module ferait
échouer le démarrage du serveur partout où l'extra n'est pas installé —
c'est-à-dire aujourd'hui en CI et en production.
"""

from __future__ import annotations

import logging
from typing import Any

from brain_v42.provenance import UNKNOWN_ACTOR

logger = logging.getLogger(__name__)

#: Plafond de cardinalité sur ``brain.actor``. Même valeur et même raison que
#: ``MetricsCollector._MAX_AGENTS`` (collector.py:130) : ``X-Brain-Agent`` est
#: déclaré par le client donc falsifiable, et un attribut de span n'a AUCUN
#: plafond natif. Un sampler ne remplace pas cette borne — il borne le VOLUME,
#: pas le nombre de valeurs DISTINCTES. Plafond volontairement indépendant de
#: celui du collector : réutiliser son état privé coûterait un couplage pire
#: que la divergence, puisque les deux sont bornés.
MAX_TRACED_ACTORS = 32
OVERFLOW_ACTOR = "_overflow"

_SPAN_OPERATION = "execute_tool"

_tracer: Any | None = None
#: Le provider privé, gardé pour pouvoir le vider À L'ARRÊT. Sans cette
#: référence, `shutdown_on_exit=False` ferait perdre en silence tout ce qui
#: reste dans la file du BatchSpanProcessor — on aurait échangé un arrêt qui
#: traîne contre des spans qui disparaissent.
_provider: Any | None = None
_known_actors: set[str] = set()


def set_tracer(tracer: Any | None) -> None:
    """Poser le tracer courant — point d'injection des tests.

    Séparé de ``reset_actor_cardinality`` à dessein : un helper qui ferait les
    deux créerait un couplage caché entre « je change de tracer » et « j'oublie
    les acteurs vus », deux gestes sans rapport.
    """
    global _tracer
    _tracer = tracer


def get_tracer() -> Any | None:
    """Le tracer courant, ou ``None`` quand le tracing est fermé.

    Rendre ``None`` et non un tracer no-op est le point qui fait que le
    killswitch COUPE : l'API OTel rend volontiers un no-op, mais on paierait
    quand même la construction des attributs sur un chemin qui s'exécute à
    chaque appel de tool.
    """
    return _tracer


def reset_actor_cardinality() -> None:
    """Vider le registre des acteurs déjà vus."""
    _known_actors.clear()


def bounded_actor(actor: str | None) -> str:
    """Ramener un acteur dans un nombre borné de seaux."""
    name = (actor or UNKNOWN_ACTOR).strip() or UNKNOWN_ACTOR
    if name in _known_actors:
        return name
    if len(_known_actors) >= MAX_TRACED_ACTORS:
        return OVERFLOW_ACTOR
    _known_actors.add(name)
    return name


def _error_type(exc: BaseException, unwrap: bool) -> str:
    """Le nom de classe à publier, en déballant le masquage métier.

    ``business_errors._wrap`` relaie toute erreur métier en ``ToolError``.
    Publier ce nom-là ferait dégénérer ``error.type`` en « ToolError » pour
    toute panne — précisément le défaut qui avait fait écarter un middleware
    comme point de mesure.
    """
    if unwrap and exc.__cause__ is not None:
        return type(exc.__cause__).__name__
    return type(exc).__name__


def start_tool_span(tool_name: str) -> Any | None:
    """Ouvrir un span RACINE pour un appel de tool, ou ``None``.

    Le contexte vide est passé EXPLICITEMENT. ``start_span(name)`` sans
    contexte résout le parent depuis le contexte courant : un client qui
    propage un ``traceparent`` nous adopterait comme enfant et déciderait de
    notre échantillonnage. Ce serveur mesure ses propres appels, il n'est pas
    un maillon de la trace d'un tiers.
    """
    tracer = _tracer
    if tracer is None:
        return None
    try:
        from opentelemetry.context import Context  # noqa: PLC0415

        span = tracer.start_span(f"{_SPAN_OPERATION} {tool_name}", context=Context())
        span.set_attribute("gen_ai.operation.name", _SPAN_OPERATION)
        span.set_attribute("gen_ai.tool.name", tool_name)
        return span
    except Exception:
        # Une sonde ne peut pas faire tomber ce qu'elle observe. Ce chemin
        # s'exécute à chaque appel de tool dans un process partagé.
        logger.debug("tracing.start_span_failed", exc_info=True)
        return None


def finish_tool_span(
    span: Any | None,
    *,
    actor: str | None,
    error: bool,
    latency_ms: float,
    exception: BaseException | None = None,
    unwrap: bool = False,
) -> None:
    """Clore le span avec le MÊME verdict que le compteur.

    ``error`` et ``latency_ms`` sont ceux passés à ``record_tool_call`` : un
    span qui contredirait le compteur donnerait deux vérités et rendrait les
    deux inutilisables.

    Ce qui n'entre JAMAIS ici : les arguments et le résultat du tool,
    ``str(exc)``, ``exc.args``, une stacktrace, la clé de projet, l'identifiant
    de session ou de transport.
    """
    if span is None:
        return
    try:
        span.set_attribute("brain.actor", bounded_actor(actor))
        span.set_attribute("brain.tool.error", error)
        span.set_attribute("brain.tool.latency_ms", round(latency_ms, 1))
        if exception is not None:
            span.set_attribute("error.type", _error_type(exception, unwrap))
    except Exception:
        logger.debug("tracing.set_attribute_failed", exc_info=True)
    try:
        span.end()
    except Exception:
        logger.debug("tracing.end_span_failed", exc_info=True)


def init_tracing(endpoint: str, *, service_name: str = "brain-v42-mcp") -> bool:
    """Armer un provider PRIVÉ exportant en OTLP. Rend False si impossible.

    ``shutdown_on_exit=False`` est explicite : ``TracerProvider.__init__``
    enregistre sinon ``atexit.register(self.shutdown)`` (vérifié dans le SDK
    1.44.0, lignes 1316 et 1347), et un exporter injoignable ferait alors
    traîner l'arrêt du serveur jusqu'à son propre délai. L'arrêt est piloté
    par l'appelant, borné, pas par ``atexit``.

    Toutes les bornes sont passées, y compris ``export_timeout_millis`` —
    vérifié actif dans ``BatchSpanProcessor.__init__`` (ligne 169), contre
    l'hypothèse répandue qu'il serait inerte.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    except ImportError:
        # Extra `tracing` non installé : c'est un état NORMAL, pas une panne.
        logger.info("tracing.sdk_absent endpoint=%s", endpoint)
        return False
    try:
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}),
            shutdown_on_exit=False,
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, timeout=2),
                max_queue_size=2048,
                max_export_batch_size=256,
                schedule_delay_millis=5000,
                export_timeout_millis=2000,
            )
        )
        # `provider.get_tracer`, JAMAIS `trace.set_tracer_provider` : voir
        # l'en-tête du module.
        global _provider
        _provider = provider
        set_tracer(provider.get_tracer(__name__))
        return True
    except Exception:
        logger.warning("tracing.init_failed endpoint=%s", endpoint, exc_info=True)
        return False


def shutdown_tracing(timeout_ms: int = 3000) -> None:
    """Vider la file puis fermer le provider, dans un délai BORNÉ.

    Contrepartie obligatoire de ``shutdown_on_exit=False`` : l'``atexit`` du SDK
    ayant été désactivé pour qu'un exporter injoignable ne fasse pas traîner
    l'arrêt, c'est à l'appelant de vider — sinon les spans encore en file
    disparaissent sans un mot. Découvert par l'e2e, pas par relecture.

    Ne lève jamais : un arrêt ne doit pas échouer à cause de sa télémétrie.
    """
    global _provider
    provider = _provider
    _provider = None
    set_tracer(None)
    if provider is None:
        return
    try:
        provider.force_flush(timeout_ms)
    except Exception:
        logger.debug("tracing.flush_failed", exc_info=True)
    try:
        provider.shutdown()
    except Exception:
        logger.debug("tracing.shutdown_failed", exc_info=True)
