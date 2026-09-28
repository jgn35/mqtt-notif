"""Source MQTT : connexion, souscription QoS 0, reconnexion à backoff croissant.

Itération asynchrone des messages ; une ligne de journal (ISO 8601 UTC, stdout)
par connexion, déconnexion et reconnexion. Aucune erreur après le démarrage ne
peut arrêter le service : la boucle de reconnexion est inconditionnelle.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiomqtt

from . import log_line
from .config import Config
from .rules import minimal_subscription_topics

RECONNECT_BACKOFF_INITIAL = 1.0
RECONNECT_BACKOFF_MAX = 60.0


def message_payload(message: aiomqtt.Message) -> str:
    """Décode le payload d'un message MQTT en chaîne (binaire ou texte)."""
    payload = message.payload
    if isinstance(payload, bytes | bytearray):
        return bytes(payload).decode("utf-8", errors="replace")
    return str(payload)


async def iter_messages(
    config: Config, stop: asyncio.Event
) -> AsyncIterator[aiomqtt.Message]:
    """Itère les messages des topics souscrits ; reconnexion auto à backoff croissant.

    Retourne uniquement quand `stop` est déclenché (arrêt propre SIGTERM/SIGINT).
    Une erreur broker après le premier démarrage ne termine jamais l'itération :
    déconnexion journalisée, attente exponentielle bornée, puis reconnexion.
    """
    topics = minimal_subscription_topics(config.rules)
    topics_log = ",".join(topics)
    backoff = RECONNECT_BACKOFF_INITIAL
    ever_connected = False
    while not stop.is_set():
        try:
            async with aiomqtt.Client(
                hostname=config.broker_host,
                port=config.broker_port,
            ) as client:
                log_line(
                    f"mqtt {'reconnected' if ever_connected else 'connected'} "
                    f"{config.broker_host}:{config.broker_port} "
                    f"topics={topics_log} qos=0"
                )
                ever_connected = True
                backoff = RECONNECT_BACKOFF_INITIAL
                for filter_topic in topics:
                    await client.subscribe(filter_topic, qos=0)
                async for message in client.messages:
                    if stop.is_set():
                        return
                    yield message
        except aiomqtt.MqttError as exc:
            if stop.is_set():
                return
            state = "connect failed" if not ever_connected else "disconnected"
            log_line(f"mqtt {state} error={exc!r}, retry in {backoff:.1f}s")
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            return
        except TimeoutError:
            pass
        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
