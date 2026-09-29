"""AC-1.1.1 / AC-1.1.3 / AC-1.2.2 : connexion, souscription QoS 0 à l'ensemble
des topics des règles, logs de cycle de vie, reconnexion automatique à backoff
croissant, jamais d'arrêt spontané."""

from __future__ import annotations

import asyncio
import re
import time

import aiomqtt
import pytest

import mqtt_notif.mqtt as mqtt_module
from mqtt_notif.config import Config, ConfigStore
from mqtt_notif.mqtt import RECONNECT_BACKOFF_INITIAL, RECONNECT_BACKOFF_MAX, iter_messages, message_payload
from mqtt_notif.rules import Rule

from .conftest import Episode, FakeClient, FakeMessage

ISO_UTC = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00\] ")


def get_lines(capsys):
    return capsys.readouterr().out.splitlines()


async def test_connect_subscribe_and_log(fake_broker, config, capsys):
    """Broker joignable : connexion, souscription QoS 0, log de connexion ISO 8601 UTC."""
    episodes, clients = fake_broker
    episodes.append(Episode(messages=[FakeMessage(b"hello")], drop=aiomqtt.MqttError("drop test")))
    stop = asyncio.Event()

    got = []
    async for message in iter_messages(ConfigStore(config), stop):
        got.append(message_payload(message))
        stop.set()

    assert got == ["hello"]
    assert clients[0].subscribed == [(config.rules[0].topic, 0)]
    out = get_lines(capsys)
    assert ISO_UTC.match(out[0])
    assert f"mqtt connected {config.broker_host}:{config.broker_port}" in out[0]
    assert f"topics={config.rules[0].topic} qos=0" in out[0]


async def test_multi_rule_subscriptions_and_resubscribe_on_reconnect(fake_broker, capsys):
    """AC-1.2.2 : chaque connexion souscrit l'ensemble complet des topics, multi-règles.

    Reconnexion après coupure : le nouveau client re-souscrit tout l'ensemble
    (une règle oubliée serait une perte de couverture silencieuse).
    """
    episodes, clients = fake_broker
    multi_config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(name="capteur", topic="home/salon/capteur", title="titre", short_message="message"),
            Rule(name="garage", topic="garage/#", title="titre", short_message="message"),
        ),
    )
    expected = [("garage/#", 0), ("home/salon/capteur", 0)]
    drop = aiomqtt.MqttError("broker coupé")
    episodes.append(Episode(messages=[FakeMessage(b"1", topic="garage/x")], drop=drop))
    episodes.append(Episode(messages=[FakeMessage(b"2", topic="home/salon/capteur")], drop=drop))
    stop = asyncio.Event()

    got = []
    async for message in iter_messages(ConfigStore(multi_config), stop):
        got.append(message_payload(message))
        if len(got) == 2:
            stop.set()

    assert got == ["1", "2"]
    assert clients[0].subscribed == expected
    assert clients[1].subscribed == expected
    # Les deux logs opérationnels montrent l'ensemble fusionné complet :
    # seule vue observable du sur-ensemble côté démon.
    out = get_lines(capsys)
    connection_lines = [
        line for line in out if "mqtt connected" in line or "mqtt reconnected" in line
    ]
    assert len(connection_lines) == 2
    assert any("mqtt connected" in line for line in connection_lines)
    assert any("mqtt reconnected" in line for line in connection_lines)
    assert all("topics=garage/#,home/salon/capteur qos=0" in line for line in connection_lines)


async def test_fake_broker_delivers_once_per_matching_subscription():
    """Le faux broker modélise la livraison par souscription : filtres chevauchants -> 2 livraisons.

    C'est l'état known-bad évité par la fusion disjointe : si le démon
    s'abonnait à deux filtres s'intersectant, chaque message partirait deux fois.
    """
    episodes = [Episode(messages=[FakeMessage(b"x", topic="home/salon/capteur")])]
    client = FakeClient(episodes)
    async with client:
        await client.subscribe("home/+/capteur", qos=0)
        await client.subscribe("home/salon/+", qos=0)
        deliveries = []
        async for message in client.messages:
            deliveries.append(message)
            if len(deliveries) == 2:
                break
    assert len(deliveries) == 2


async def test_fake_broker_single_delivery_for_disjoint_subscriptions():
    """Souscriptions deux à deux disjointes : le même topic n'est livré qu'une fois.

    L'épisode se termine par un drop : sans break prématuré, une livraison
    dupliquée serait effectivement observée et ferait échouer l'assertion.
    """
    episodes = [
        Episode(messages=[FakeMessage(b"x", topic="home/salon/capteur")], drop=aiomqtt.MqttError("drop"))
    ]
    client = FakeClient(episodes)
    async with client:
        await client.subscribe("home/+/+", qos=0)  # plus petit sur-ensemble fusionné
        await client.subscribe("garage/#", qos=0)
        deliveries = []
        with pytest.raises(aiomqtt.MqttError):
            async for message in client.messages:
                deliveries.append(message)
    assert len(deliveries) == 1



async def test_reconnect_after_drop(fake_broker, config, capsys):
    """Broker coupé puis revenu : reconnexion, message suivant livré, log de reconnexion."""
    episodes, clients = fake_broker
    drop = aiomqtt.MqttError("broker coupé")
    episodes.append(Episode(messages=[FakeMessage(b"1")], drop=drop))
    episodes.append(Episode(messages=[FakeMessage(b"2")], drop=drop))
    stop = asyncio.Event()

    got = []
    async for message in iter_messages(ConfigStore(config), stop):
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

    async for _message in iter_messages(ConfigStore(config), stop):
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

    async for _message in iter_messages(ConfigStore(config), stop):
        pass

    assert max(recorded) == RECONNECT_BACKOFF_MAX


async def test_stop_during_backoff_returns(fake_broker, config, monkeypatch, capsys):
    """Stop pendant l'attente de reconnexion : retour propre bien avant l'expiration."""
    episodes, _clients = fake_broker  # épuisé : chaque connexion est refusée
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 10.0)
    stop = asyncio.Event()

    async def consume():
        async for _message in iter_messages(ConfigStore(config), stop):
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
