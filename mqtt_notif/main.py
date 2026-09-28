"""Assemblage du pipeline : source MQTT -> envoi FCM, boucle asyncio unique.

Tâche d'envoi asyncio indépendante par message (la boucle n'attend jamais la
fin d'un envoi). SIGTERM/SIGINT -> arrêt propre, exit code 0. Erreurs fatales
uniquement avant le démarrage (config invalide -> exit code non nul) ; après
le démarrage, aucune erreur n'arrête le service (relance du pipeline journalisée).
"""

from __future__ import annotations

import asyncio
import signal
import sys

from . import log_line
from .config import Config, ConfigError, load_config_from_env
from .mqtt import iter_messages
from .notifier import Notifier

SHUTDOWN_TIMEOUT_S = 5.0
RESTART_DELAY_S = 1.0


async def run_pipeline(config: Config, notifier: Notifier, stop: asyncio.Event) -> None:
    """Consomme la source MQTT et déclenche un envoi FCM par message, sans l'attendre.

    Retourne quand `stop` est observé ; laisse finir les envois en cours, borné
    dans le temps — au-delà, les envois restants sont annulés et journalisés.
    """
    pending: set[asyncio.Task[None]] = set()
    try:
        async for message in iter_messages(config, stop):
            log_line(f"mqtt message topic={message.topic}")
            task = asyncio.create_task(notifier.notify())
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
