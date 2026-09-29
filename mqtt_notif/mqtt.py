"""Source MQTT : connexion, souscription QoS 0 à l'ensemble disjoint des topics
des règles, resynchronisation des souscriptions au swap de configuration,
reconnexion à backoff croissant.

Les souscriptions sont pilotées par la configuration courante du
ConfigStore : au rechargement, la source abonne les nouveaux topics et
désabonne les disparus (aiomqtt subscribe/unsubscribe) sans jamais
déconnecter le broker. Itération asynchrone des messages ; une ligne de
journal (ISO 8601 UTC, stdout) par connexion, déconnexion, reconnexion
et resynchronisation. Aucune erreur après le démarrage ne peut arrêter
le service : la boucle de reconnexion est inconditionnelle.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiomqtt

from . import log_line
from .config import ConfigStore
from .rules import subscription_topics

RECONNECT_BACKOFF_INITIAL = 1.0
RECONNECT_BACKOFF_MAX = 60.0


def message_payload(message: aiomqtt.Message) -> str:
    """Décode le payload d'un message MQTT en chaîne (binaire ou texte)."""
    payload = message.payload
    if isinstance(payload, bytes | bytearray):
        return bytes(payload).decode("utf-8", errors="replace")
    return str(payload)


async def _resync_subscriptions(
    client: aiomqtt.Client, current: set[str], topics: tuple[str, ...]
) -> None:
    """Applique le delta abonnement/désabonnement vers le nouvel ensemble,
    sans déconnexion (éventuelle resynchronisation journalisée)."""
    new = set(topics)
    subscribe = sorted(new - current)
    unsubscribe = sorted(current - new)
    for filter_topic in subscribe:
        await client.subscribe(filter_topic, qos=0)
    for filter_topic in unsubscribe:
        await client.unsubscribe(filter_topic)
    if subscribe or unsubscribe:
        log_line(
            f"mqtt resync subscribed={','.join(subscribe) or '-'} "
            f"unsubscribed={','.join(unsubscribe) or '-'} qos=0"
        )


async def iter_messages(
    store: ConfigStore, stop: asyncio.Event
) -> AsyncIterator[aiomqtt.Message]:
    """Itère les messages des topics souscrits ; reconnexion auto à backoff croissant.

    Souscription initiale à l'ensemble de souscriptions deux à deux
    disjointes des topics des règles de la configuration courante — deux
    souscriptions retenues ne peuvent pas correspondre au même topic, un
    message chevauchant plusieurs règles n'est livré qu'une fois. Au swap
    de configuration (événement `changed` du store), l'attente du prochain
    message est interrompue par course (FIRST_COMPLETED) pour appliquer le
    delta abonnement/désabonnement sans déconnexion — un message sur un
    topic retiré ne déclenche plus rien. Retourne uniquement quand `stop`
    est déclenché (arrêt propre SIGTERM/SIGINT). Une erreur broker après
    le premier démarrage ne termine jamais l'itération : déconnexion
    journalisée, attente exponentielle bornée, puis reconnexion.
    """
    backoff = RECONNECT_BACKOFF_INITIAL
    ever_connected = False
    while not stop.is_set():
        config = store.get()
        topics = subscription_topics(config.rules)
        topics_log = ",".join(topics)
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
                current = set(topics)
                messages = client.messages
                pending_message: asyncio.Task | None = None
                try:
                    while True:
                        if store.changed.is_set():
                            # Swap de configuration : delta abonner/désabonner
                            # sans déconnexion, le message en attente n'est
                            # jamais annulé ni perdu.
                            store.changed.clear()
                            topics = subscription_topics(store.get().rules)
                            await _resync_subscriptions(client, current, topics)
                            current = set(topics)
                            continue
                        if pending_message is None:
                            pending_message = asyncio.ensure_future(anext(messages))
                        change_wait = asyncio.ensure_future(store.changed.wait())
                        stop_wait = asyncio.ensure_future(stop.wait())
                        try:
                            done, _pending = await asyncio.wait(
                                {pending_message, change_wait, stop_wait},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                        finally:
                            change_wait.cancel()
                            stop_wait.cancel()
                            await asyncio.gather(
                                change_wait, stop_wait, return_exceptions=True
                            )
                        if pending_message in done:
                            task = pending_message
                            pending_message = None
                            try:
                                message = task.result()
                            except StopAsyncIteration:
                                break  # fin de session : reconnexion
                            if stop.is_set():
                                return
                            yield message
                        elif stop_wait in done:
                            return
                finally:
                    if pending_message is not None and not pending_message.done():
                        pending_message.cancel()
                        await asyncio.gather(
                            pending_message, return_exceptions=True
                        )
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
