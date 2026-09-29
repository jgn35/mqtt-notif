"""Fixtures partagées : faux broker MQTT (aiomqtt mocké), configuration de test
et écriture de YAML de test (mtime bombé pour le sondage du store)."""

from __future__ import annotations

import asyncio
import itertools
import os
import threading
from pathlib import Path

import aiomqtt
import firebase_admin
import pytest
import yaml
from firebase_admin import messaging

from mqtt_notif.config import Config
from mqtt_notif.notifier import CREDENTIALS_ENV
from mqtt_notif.rules import Rule, topic_matches

_MTIME_NS = itertools.count(1)


def bump_mtime(path) -> None:
    """mtimes strictement croissants et déterministes (indépendants du FS)."""
    ns = next(_MTIME_NS) * 1_000_000
    os.utime(path, ns=(ns, ns))


def write_config_yaml(
    path, rules, *, host: str = "localhost", port: int = 1883, token: str = "token-a"
) -> Path:
    """Écrit un YAML valide ; `rules` = liste d'entrées de règle (dicts),
    échappement garanti par safe_dump ; bombe le mtime : le sondage du store
    verra le changement. Copie unique de la mécanique d'écriture."""
    data = {"broker": {"host": host, "port": port}, "device_token": token, "rules": rules}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    bump_mtime(path)
    return path


class FakeMessage:
    """Message MQTT minimal (payload bytes, topic str)."""

    def __init__(self, payload: bytes, topic: str = "home/salon/capteur"):
        self.payload = payload
        self.topic = topic


class Episode:
    """Un épisode de connexion : messages livrés, puis erreur éventuelle.

    - `error` : levée à la connexion (broker injoignable).
    - `drop` : levée après livraison des messages (broker coupé en session).
    - `queue` : optionnelle — après épuisement de `messages`, les messages
      suivants arrivent par cette file (livraison pilotable par le test,
      même connexion établie) au lieu de l'attente silencieuse.
    - ni l'un ni l'autre : connexion silencieuse qui reste établie.
    """

    def __init__(self, messages=(), error=None, drop=None, queue=None):
        self.messages = list(messages)
        self.error = error
        self.drop = drop
        self.queue = queue


class FakeClient:
    """Remplace aiomqtt.Client : consomme une liste partagée d'épisodes.

    Un broker MQTT peut livrer un message une fois par souscription
    correspondante (filtres '+'/'#' évalués par topic_matches) — MQTT
    3.1.1 § 3.3.5 l'autorise aussi en copie unique ; le faux broker
    choisit la livraison multiple, pire cas exposant tout doublon.
    Épisodes épuisés -> erreur de connexion (broker définitivement
    injoignable).
    """

    def __init__(self, episodes, hostname="", port=0):
        self._episodes = episodes
        self._episode = None
        self.hostname = hostname
        self.port = port
        self.subscribed = []
        self.unsubscribed = []
        self.connected_count = 0

    async def __aenter__(self):
        if not self._episodes:
            raise aiomqtt.MqttError("connection refused (fake broker épuisé)")
        self._episode = self._episodes.pop(0)
        if self._episode.error is not None:
            raise self._episode.error
        self.connected_count += 1
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def subscribe(self, topic, qos=0):
        self.subscribed.append((str(topic), qos))

    async def unsubscribe(self, topic, **kwargs):
        """Retire la souscription : un topic désabonné n'est plus livré."""
        topic = str(topic)
        self.subscribed = [entry for entry in self.subscribed if entry[0] != topic]
        self.unsubscribed.append(topic)

    @property
    def messages(self):
        return self._iter_messages()

    async def _iter_messages(self):
        for message in self._episode.messages:
            # Livraison possible une fois par souscription correspondante,
            # dans l'ordre des souscriptions prises sur ce client.
            topic = str(message.topic)
            for filter_topic, _qos in self.subscribed:
                if topic_matches(filter_topic, topic):
                    yield message
        if self._episode.queue is not None:
            # Suite pilotable : chaque message de la file est livré selon les
            # souscriptions en vigueur au moment de sa livraison (un topic
            # désabonné entre-temps ne déclenche plus rien).
            while True:
                message = await self._episode.queue.get()
                topic = str(message.topic)
                for filter_topic, _qos in self.subscribed:
                    if topic_matches(filter_topic, topic):
                        yield message
        if self._episode.drop is not None:
            raise self._episode.drop
        # Connexion silencieuse : reste établie sans rien livrer.
        while True:
            await asyncio.sleep(3600)


@pytest.fixture
def fake_broker(monkeypatch):
    """Patche aiomqtt.Client ; retourne (episodes, clients) pilotables par le test."""
    episodes: list[Episode] = []
    clients: list[FakeClient] = []

    def client_factory(*, hostname=None, port=None, **kwargs):
        client = FakeClient(episodes, hostname=hostname or "", port=port or 0)
        clients.append(client)
        return client

    import mqtt_notif.mqtt as mqtt_module

    monkeypatch.setattr(mqtt_module.aiomqtt, "Client", client_factory)
    return episodes, clients


@pytest.fixture
def config() -> Config:
    return Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="token-test-1234",
        rules=(
            Rule(
                name="capteur-salon",
                topic="home/salon/capteur",
                title="Capteur salon",
                short_message="Valeur: {valeur}",
            ),
        ),
    )


@pytest.fixture
def credentials_env(monkeypatch, tmp_path):
    path = tmp_path / "service-account.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(CREDENTIALS_ENV, str(path))
    return str(path)


@pytest.fixture
def fcm_mock(monkeypatch, credentials_env):
    """Mocke la frontière firebase-admin ; capture app, messages et thread
    d'exécution. Forme unique partagée : (appels, threads)."""
    calls = []
    sent_from_threads = []

    def fake_certificate(path):
        return f"credential:{path}"

    def fake_initialize_app(credential=None, **kwargs):
        return "fake-app"

    def fake_send(message, **kwargs):
        calls.append(message)
        sent_from_threads.append(threading.get_ident())
        if kwargs.get("app") != "fake-app":
            raise ValueError("app inattendu")

    monkeypatch.setattr(firebase_admin, "initialize_app", fake_initialize_app)
    monkeypatch.setattr("firebase_admin.credentials.Certificate", fake_certificate)
    monkeypatch.setattr(messaging, "send", fake_send)
    return calls, sent_from_threads
