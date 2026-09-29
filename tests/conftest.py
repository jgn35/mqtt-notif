"""Fixtures partagées : faux broker MQTT (aiomqtt mocké) et configuration de test."""

from __future__ import annotations

import asyncio

import aiomqtt
import pytest

from mqtt_notif.config import Config
from mqtt_notif.rules import Rule, topic_matches


class FakeMessage:
    """Message MQTT minimal (payload bytes, topic str)."""

    def __init__(self, payload: bytes, topic: str = "home/salon/capteur"):
        self.payload = payload
        self.topic = topic


class Episode:
    """Un épisode de connexion : messages livrés, puis erreur éventuelle.

    - `error` : levée à la connexion (broker injoignable).
    - `drop` : levée après livraison des messages (broker coupé en session).
    - ni l'un ni l'autre : connexion silencieuse qui reste établie.
    """

    def __init__(self, messages=(), error=None, drop=None):
        self.messages = list(messages)
        self.error = error
        self.drop = drop


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
