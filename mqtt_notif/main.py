"""Assemblage du pipeline : source MQTT -> sélection de règle -> envoi FCM,
boucle asyncio unique.

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
from .config import Config, ConfigError, load_config_from_env
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


async def _dispatch(notifier: Notifier, event: Event) -> None:
    """Étape émetteur : le rendu depuis l'événement puis l'envoi FCM s'exécutent
    ici, dans cette tâche (hors boucle d'écoute) — aucune erreur de rendu ne
    remonte au pipeline."""
    await notifier.notify(event)


async def run_pipeline(config: Config, notifier: Notifier, stop: asyncio.Event) -> None:
    """Consomme la source MQTT et déclenche un envoi FCM par message, sans l'attendre.

    Retourne quand `stop` est observé ; laisse finir les envois en cours, borné
    dans le temps — au-delà, les envois restants sont annulés et journalisés.
    """
    pending: set[asyncio.Task[None]] = set()
    try:
        async for message in iter_messages(config, stop):
            topic = str(message.topic)
            log_line(f"mqtt message topic={topic}")
            event = build_event(config.rules, topic, message_payload(message))
            if event is None:
                log_line(f"message ignored topic={topic} no matching rule")
                continue
            task = asyncio.create_task(_dispatch(notifier, event))
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


async def serve(config: Config, notifier: Notifier) -> None:
    """Boucle asyncio unique avec gestion des signaux (SIGTERM/SIGINT -> arrêt propre)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stopper = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set():
            pipeline = asyncio.create_task(run_pipeline(config, notifier, stop))
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
    await stopper
    log_line("mqtt-notif stopped cleanly")


def main() -> int:
    """Point d'entrée : échec rapide sur config invalide, sinon service résilient."""
    try:
        config = load_config_from_env()
    except ConfigError as exc:
        print(f"erreur de configuration : {exc}", file=sys.stderr)
        return 1
    notifier = Notifier(config.device_token)
    asyncio.run(serve(config, notifier))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
