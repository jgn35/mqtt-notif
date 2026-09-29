"""Assemblage du pipeline : source MQTT -> sélection de règle -> debounce ->
envoi FCM, boucle asyncio unique.

La sélection lit le snapshot du ConfigStore à chaque message (règle figée
dans l'événement, jamais relue) ; le store et sa tâche de surveillance du
YAML (sondage mtime, rechargement à chaud) sont créés à l'assemblage et
le watcher est arrêté proprement au SIGTERM/SIGINT. Le debounce optionnel
par règle (fenêtre ancrée à la réception du message, jamais à l'envoi)
précède l'émetteur : le rendu et l'envoi FCM ne s'exécutent que si la
fenêtre l'autorise — un message silencié n'est jamais rendu ; le message
ignoré est journalisé avec le préfixe « debug: » (log_line n'a pas de
niveaux, le préfixe fait office), avec la fenêtre restante.

Tâche d'envoi asyncio indépendante par message (la boucle n'attend jamais la
fin d'un envoi). SIGTERM/SIGINT -> arrêt propre, exit code 0. Erreurs fatales
uniquement avant le démarrage (config invalide -> exit code non nul) ; après
le démarrage, aucune erreur n'arrête le service (relance du pipeline journalisée).
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from dataclasses import dataclass

from . import log_line
from .config import ConfigError, ConfigStore, load_store_from_env
from .debounce import Debouncer
from .mqtt import iter_messages, message_payload
from .notifier import Notifier
from .rules import PAYLOAD_FORMAT_RAW, Rule, find_rule

SHUTDOWN_TIMEOUT_S = 5.0
RESTART_DELAY_S = 1.0


@dataclass(frozen=True)
class Event:
    """Événement immuable traversant le pipeline : règle figée à la sélection,
    payload parsé à la sélection (dict pour une règle json, str pour une règle
    raw) et jamais re-parsé en traversée."""

    topic: str
    payload: dict[str, object] | str
    rule: Rule


def _parse_payload(raw: str, rule: Rule, topic: str) -> dict[str, object] | str:
    """Parse le payload selon payload_format de la règle gagnante.

    Règle json : dict attendu ; payload non parsable (RecursionError compris,
    payloads profondément imbriqués) -> dict vide + avertissement journalisé ;
    JSON parsable mais non-objet -> dict vide + avertissement distinct. Dans
    les deux cas le rendu voit tous les champs absents, le message n'est
    jamais ignoré en bloc.
    """
    if rule.payload_format == PAYLOAD_FORMAT_RAW:
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        log_line(
            f"warning: payload json non parsable topic={topic} rule={rule.name}, "
            "rendu comme si tous les champs étaient absents"
        )
        return {}
    if not isinstance(parsed, dict):
        log_line(
            f"warning: payload json non-objet topic={topic} rule={rule.name}, "
            "rendu comme si tous les champs étaient absents"
        )
        return {}
    return parsed


def build_event(rules: tuple[Rule, ...], topic: str, payload: str) -> Event | None:
    """Étape de sélection : première règle gagnante, payload parsé, événement immuable.

    Aucune correspondance -> None (message ignoré par l'appelant) ;
    plusieurs correspondances -> avertissement journalisé, première retenue.
    La règle est figée dans l'événement : jamais relue depuis la config
    en traversée (compatible avec le rechargement à chaud à venir).
    """
    rule, matches = find_rule(rules, topic)
    if rule is None:
        return None
    if matches > 1:
        log_line(
            f"warning: {matches} règles correspondent à topic={topic}, "
            f"première retenue rule={rule.name}"
        )
    return Event(topic=topic, payload=_parse_payload(payload, rule, topic), rule=rule)


async def _dispatch(
    notifier: Notifier, debouncer: Debouncer, event: Event, received_at: float
) -> None:
    """Étape émetteur : le filtre de debounce décide en premier (fenêtre
    ancrée à la réception du message, jamais à l'envoi, jamais au succès
    de l'envoi) ; le rendu puis l'envoi FCM ne s'exécutent ici, dans cette
    tâche (hors boucle d'écoute), que si la fenêtre l'autorise — un message
    silencié n'est jamais rendu. Il est perdu (QoS 0, aucun rejeu) et
    journalisé avec le préfixe « debug: » et la fenêtre restante ; aucune
    erreur de rendu ne remonte au pipeline."""
    if not debouncer.should_notify(event.rule, received_at):
        remaining_ms = debouncer.remaining_ms(event.rule, received_at)
        log_line(
            f"debug: message ignored topic={event.topic} rule={event.rule.name} "
            f"debounce_ms={event.rule.debounce_ms} fenêtre restante={remaining_ms:.0f} ms"
        )
        return
    await notifier.notify(event)


async def run_pipeline(
    store: ConfigStore,
    notifier: Notifier,
    stop: asyncio.Event,
    debouncer: Debouncer | None = None,
) -> None:
    """Consomme la source MQTT et déclenche un envoi FCM par message, sans l'attendre.

    La sélection lit `store.get()` à chaque message : la règle gagnante est
    figée dans l'événement (jamais relue en traversée), un rechargement de
    configuration prend effet à l'événement suivant. L'horodatage de
    réception est capturé à la lecture du message, avant toute étape aval —
    jamais à l'envoi : un envoi FCM lent n'étend pas la fenêtre de debounce.
    Le debouncer (état en mémoire, propriété du pipeline, horloge injectable
    pour les tests) est purgé au swap de configuration par la source (règles
    conservées). Retourne quand `stop` est observé ; laisse finir les envois
    en cours, borné dans le temps — au-delà, les envois restants sont
    annulés et journalisés.
    """
    if debouncer is None:
        debouncer = Debouncer()
    pending: set[asyncio.Task[None]] = set()
    try:
        async for message in iter_messages(store, stop, debouncer):
            received_at = debouncer.now()
            topic = str(message.topic)
            log_line(f"mqtt message topic={topic}")
            event = build_event(store.get().rules, topic, message_payload(message))
            if event is None:
                log_line(f"message ignored topic={topic} no matching rule")
                continue
            task = asyncio.create_task(_dispatch(notifier, debouncer, event, received_at))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        if pending:
            _done, stragglers = await asyncio.wait(pending, timeout=SHUTDOWN_TIMEOUT_S)
            if stragglers:
                log_line(
                    f"fcm sends abandoned: {len(stragglers)} envoi(s) encore en cours "
                    f"après {SHUTDOWN_TIMEOUT_S}s, annulés"
                )
                for task in stragglers:
                    task.cancel()
                await asyncio.gather(*stragglers, return_exceptions=True)


async def _stop_pipeline(pipeline: asyncio.Task[None]) -> None:
    """Arrête la tâche pipeline en consommant toujours son résultat ou son exception."""
    if not pipeline.done():
        pipeline.cancel()
    await asyncio.gather(pipeline, return_exceptions=True)


async def serve(store: ConfigStore, notifier: Notifier) -> None:
    """Boucle asyncio unique avec gestion des signaux (SIGTERM/SIGINT -> arrêt propre).

    Le watcher du ConfigStore (sondage mtime du YAML, rechargement à chaud)
    vit ici, indépendant des relances du pipeline ; il est arrêté proprement
    au moment de l'arrêt.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stopper = asyncio.create_task(stop.wait())
    watcher = asyncio.create_task(store.watch(stop))

    def _log_dead_watcher(task: asyncio.Task) -> None:
        """Un watcher mort en service (bug interne, pas un mauvais fichier)
        laisse une trace explicite : le rechargement à chaud est inopérant."""
        if not task.cancelled() and task.exception() is not None:
            log_line(f"config watcher error: {task.exception()!r}")

    watcher.add_done_callback(_log_dead_watcher)
    try:
        while not stop.is_set():
            pipeline = asyncio.create_task(run_pipeline(store, notifier, stop))
            done, _ = await asyncio.wait(
                {pipeline, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopper in done:
                # Arrêt demandé : on consomme toujours le résultat/exception
                # du pipeline, même s'il est déjà terminé (course FIRST_COMPLETED).
                await _stop_pipeline(pipeline)
                break
            # Pipeline terminé sans arrêt demandé : erreur interne inattendue.
            if pipeline.exception() is not None:
                log_line(f"pipeline error (restarting): {pipeline.exception()!r}")
                await asyncio.sleep(RESTART_DELAY_S)
                # Un stop pendant le sommeil ressort par la condition du while
                # puis par le même chemin d'arrêt propre ci-dessous.
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    await stopper
    log_line("mqtt-notif stopped cleanly")


def main() -> int:
    """Point d'entrée : échec rapide sur config invalide, sinon service résilient."""
    try:
        store = load_store_from_env()
    except ConfigError as exc:
        print(f"erreur de configuration : {exc}", file=sys.stderr)
        return 1
    notifier = Notifier(store)
    asyncio.run(serve(store, notifier))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
