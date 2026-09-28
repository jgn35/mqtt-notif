"""AC-1.1.1 / AC-1.1.3 / AC-1.2.2 : connexion, souscriptions QoS 0 de
l'ensemble des topics des règles (couverture minimale), logs de cycle de vie,
reconnexion automatique à backoff croissant, jamais d'arrêt spontané."""

from __future__ import annotations

import asyncio
import re
import time

import aiomqtt

import mqtt_notif.mqtt as mqtt_module
from mqtt_notif.config import Config
from mqtt_notif.mqtt import RECONNECT_BACKOFF_INITIAL, RECONNECT_BACKOFF_MAX, iter_messages, message_payload
from mqtt_notif.rules import Rule

from .conftest import Episode, FakeMessage

ISO_UTC = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00\] ")


def get_lines(capsys):
    return capsys.readouterr().out.splitlines()


async def test_connect_subscribe_and_log(fake_broker, config, capsys):
    """Broker joignable : connexion, souscription QoS 0, log de connexion ISO 8601 UTC."""
    episodes, clients = fake_broker
    episodes.append(Episode(messages=[FakeMessage(b"hello")], drop=aiomqtt.MqttError("drop test")))
    stop = asyncio.Event()

    got = []
    async for message in iter_messages(config, stop):
        got.append(message_payload(message))
        stop.set()

    assert got == ["hello"]
    assert clients[0].subscribed == [(config.rules[0].topic, 0)]
    out = get_lines(capsys)
    assert ISO_UTC.match(out[0])
    assert f"mqtt connected {config.broker_host}:{config.broker_port}" in out[0]
    assert f"topics={config.rules[0].topic} qos=0" in out[0]


async def test_subscribe_all_rule_topics(fake_broker, capsys):
    """AC-1.2.2 : souscription QoS 0 à l'ensemble des topics des règles."""
    episodes, clients = fake_broker
    config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(name="a", topic="home/salon/capteur"),
            Rule(name="b", topic="home/+/bouton"),
            Rule(name="c", topic="garage/porte"),
        ),
    )
    episodes.append(Episode(messages=[FakeMessage(b"x")], drop=aiomqtt.MqttError("drop")))
    stop = asyncio.Event()

    async for _message in iter_messages(config, stop):
        stop.set()

    assert clients[0].subscribed == [
        ("home/salon/capteur", 0),
        ("home/+/bouton", 0),
        ("garage/porte", 0),
    ]


async def test_overlapping_rules_subscribe_minimal_coverage(fake_broker):
    """AC-1.2.2 : filtres chevauchants -> couverture minimale, une seule livraison par message."""
    episodes, clients = fake_broker
    config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(name="exact", topic="home/salon/capteur"),
            Rule(name="motif", topic="home/+/capteur"),
            Rule(name="tout", topic="home/#"),
        ),
    )
    episodes.append(Episode(messages=[FakeMessage(b"x")], drop=aiomqtt.MqttError("drop")))
    stop = asyncio.Event()

    async for _message in iter_messages(config, stop):
        stop.set()

    assert clients[0].subscribed == [("home/#", 0)]


async def test_reconnect_after_drop(fake_broker, config, capsys):
    """Broker coupé puis revenu : reconnexion, message suivant livré, log de reconnexion."""
    episodes, clients = fake_broker
    drop = aiomqtt.MqttError("broker coupé")
    episodes.append(Episode(messages=[FakeMessage(b"1")], drop=drop))
    episodes.append(Episode(messages=[FakeMessage(b"2")], drop=drop))
    stop = asyncio.Event()

    got = []
    async for message in iter_messages(config, stop):
        got.append(message_payload(message))
        if len(got) == 2:
            stop.set()

    assert got == ["1", "2"]
    out = get_lines(capsys)
    assert any("mqtt connected" in line for line in out)
    # Déconnexion de l'épisode 1 journalisée (le stop arrive pendant l'épisode 2).
    assert any("mqtt disconnected" in line for line in out)
    assert any("mqtt reconnected" in line for line in out)


async def test_backoff_grows_and_never_stops(fake_broker, config, monkeypatch, capsys):
    """Broker définitivement injoignable : backoff croissant, boucle sans fin volontaire.

    On patche asyncio.wait_for pour observer les délais sans dormir réellement ;
    le garde-fou arrête le test après quelques tentatives (le service, lui,
    ne s'arrêterait jamais de lui-même).
    """
    episodes, _clients = fake_broker  # aucun épisode -> connexion refusée à chaque fois
    stop = asyncio.Event()
    recorded: list[float] = []

    async def fake_wait_for(aw, timeout):
        recorded.append(timeout)
        if len(recorded) >= 4:
            stop.set()
        aw.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    async for _message in iter_messages(config, stop):
        pass

    assert recorded == [
        RECONNECT_BACKOFF_INITIAL,
        RECONNECT_BACKOFF_INITIAL * 2,
        RECONNECT_BACKOFF_INITIAL * 4,
        RECONNECT_BACKOFF_INITIAL * 8,
    ]
    out = get_lines(capsys)
    assert len([line for line in out if "mqtt connect failed" in line]) >= 4


async def test_backoff_capped(fake_broker, config, monkeypatch):
    """Le backoff est borné par RECONNECT_BACKOFF_MAX."""
    episodes, _clients = fake_broker
    stop = asyncio.Event()
    recorded: list[float] = []

    async def fake_wait_for(aw, timeout):
        recorded.append(timeout)
        if len(recorded) >= 12:
            stop.set()
        aw.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    async for _message in iter_messages(config, stop):
        pass

    assert max(recorded) == RECONNECT_BACKOFF_MAX


async def test_stop_during_backoff_returns(fake_broker, config, monkeypatch, capsys):
    """Stop pendant l'attente de reconnexion : retour propre bien avant l'expiration."""
    episodes, _clients = fake_broker  # épuisé : chaque connexion est refusée
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 10.0)
    stop = asyncio.Event()

    async def consume():
        async for _message in iter_messages(config, stop):
            pass

    task = asyncio.create_task(consume())
    # Attendre que la boucle entre réellement dans l'attente de backoff.
    seen = ""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        seen += capsys.readouterr().out
        if "retry in" in seen:
            break
    assert "retry in 10.0s" in seen

    stop.set()
    start = time.monotonic()
    await asyncio.wait_for(task, timeout=2)
    # Retour immédiat : bien avant les 10s du backoff en cours.
    assert time.monotonic() - start < 5


def test_message_payload_decodes_bytes_and_text():
    assert message_payload(FakeMessage(b"octets")) == "octets"
    msg = FakeMessage(b"x")
    msg.payload = "texte"
    assert message_payload(msg) == "texte"
