"""Story 2.1 : rechargement à chaud avec swap atomique — ConfigStore (sondage
mtime, validation avant swap, rejet explicite YAML invalide et host/port),
effet à l'événement suivant, resynchronisation des souscriptions sans
déconnexion, rotation du device token, lecteur unique du YAML (grep)."""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import signal
import time
from pathlib import Path

import aiomqtt
import firebase_admin
import pytest
from firebase_admin import messaging

import mqtt_notif
import mqtt_notif.config as config_module
from mqtt_notif.config import Config, ConfigStore, load_config
from mqtt_notif.main import Event, build_event, serve
from mqtt_notif.mqtt import iter_messages, message_payload
from mqtt_notif.notifier import CREDENTIALS_ENV, Notifier
from mqtt_notif.render import render_message
from mqtt_notif.rules import Rule

from .conftest import Episode, FakeMessage

ISO_UTC = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00\] ")

DEFAULT_RULES = (("salon", "home/salon/capteur", "Capteur salon", "Valeur: {valeur}"),)

_MTIME_NS = itertools.count(1)


def bump_mtime(path: Path) -> None:
    """mtimes strictement croissants et déterministes (indépendants du FS)."""
    ns = next(_MTIME_NS) * 1_000_000
    os.utime(path, ns=(ns, ns))


def write_yaml(
    path: Path,
    *,
    host: str = "localhost",
    port: int = 1883,
    token: str = "token-a",
    rules=DEFAULT_RULES,
) -> Path:
    """Écrit un YAML valide (règles : name, topic, title, short_message) et
    bombe le mtime : le sondage du store verra le changement."""
    lines = f"broker:\n  host: {host}\n  port: {port}\ndevice_token: {token}\nrules:\n"
    for name, topic, title, short_message in rules:
        lines += (
            f"  - name: {name}\n"
            f"    topic: {topic}\n"
            f"    title: {title}\n"
            f"    short_message: '{short_message}'\n"
        )
    path.write_text(lines, encoding="utf-8")
    bump_mtime(path)
    return path


def file_store(path: Path) -> ConfigStore:
    """Store sur fichier réel : rechargement à chaud actif."""
    return ConfigStore(load_config(str(path)), str(path))


class SilentNotifier:
    """Notifier bouchon pour serve : aucune dépendance FCM."""

    async def notify(self, event) -> None:
        pass


@pytest.fixture
def fcm_mock(monkeypatch, tmp_path):
    """Mocke la frontière firebase-admin ; capture les messages envoyés."""
    credentials = tmp_path / "service-account.json"
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(CREDENTIALS_ENV, str(credentials))
    calls = []
    monkeypatch.setattr(
        firebase_admin, "initialize_app", lambda credential=None, **kwargs: "fake-app"
    )
    monkeypatch.setattr("firebase_admin.credentials.Certificate", lambda path: "credential")
    monkeypatch.setattr(messaging, "send", lambda message, **kwargs: calls.append(message))
    return calls


# --- ConfigStore : chargement initial, sondage, swap atomique, rejets ---


def test_store_get_returns_initial_snapshot(tmp_path):
    """AC-2.1.5 : get() renvoie la configuration initiale figée."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    config = store.get()
    assert config.device_token == "token-a"
    assert [rule.name for rule in config.rules] == ["salon"]


async def test_poll_without_change_is_silent(tmp_path, capsys):
    """Aucun changement de mtime : aucun rechargement, aucune ligne de journal."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    before = store.get()

    await store.poll()

    assert store.get() is before
    assert capsys.readouterr().out == ""


async def test_reload_swaps_whole_config_logs_once(tmp_path, capsys):
    """AC-2.1.1 / AC-2.1.5 : mtime changé -> validation puis swap intégral
    (token, règles, libellés : jamais un mélange), une seule ligne de journal."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()

    write_yaml(
        path,
        token="token-b",
        rules=(
            ("salon", "home/salon/capteur", "Capteur salon v2", "Valeur: {valeur} (v2)"),
            ("garage", "home/garage/#", "Garage", "Portail: {action}"),
        ),
    )
    await store.poll()

    new = store.get()
    assert new is not initial
    assert new.device_token == "token-b"
    assert new.rules[0].title == "Capteur salon v2"
    assert [rule.name for rule in new.rules] == ["salon", "garage"]
    assert new.broker_host == initial.broker_host  # inchangé : swap intégral, pas créatif
    assert store.changed.is_set()  # armé : la source MQTT resynchronisera

    out = capsys.readouterr().out
    assert out.count("config reloaded") == 1
    assert "config reloaded rules=2 topics=home/garage/#,home/salon/capteur" in out
    assert ISO_UTC.match(out)
    # Sondage suivant sans nouveau changement : aucune ligne en double.
    await store.poll()
    assert capsys.readouterr().out == ""


async def test_swap_atomic_never_mixed_view(tmp_path, monkeypatch):
    """AC-2.1.5 : chaque snapshot est intégralement l'ancienne ou la nouvelle
    version — token et règles d'un même fichier, jamais un mélange, même en
    rechargement continu."""
    monkeypatch.setattr(config_module, "POLL_INTERVAL_S", 0.001)
    path = write_yaml(tmp_path / "config.yaml", token="token-a")
    store = file_store(path)
    stop = asyncio.Event()
    watcher = asyncio.create_task(store.watch(stop))

    versions = {
        ("token-a", 1),
        ("token-b", 2),
    }
    try:
        for index in range(30):
            two_rules = (
                DEFAULT_RULES[0],
                ("garage", "home/garage/#", "Garage", "Portail: {action}"),
            )
            write_yaml(
                path,
                token="token-b" if index % 2 else "token-a",
                rules=two_rules if index % 2 else DEFAULT_RULES,
            )
            snapshot = store.get()
            assert (snapshot.device_token, len(snapshot.rules)) in versions
            await asyncio.sleep(0.002)
            snapshot = store.get()
            assert (snapshot.device_token, len(snapshot.rules)) in versions
    finally:
        stop.set()
        await asyncio.wait_for(watcher, timeout=5)


@pytest.mark.parametrize(
    "broken",
    [
        "broker: [non, fermé\n  host: x\n",  # YAML corrompu
        "broker:\n  host: localhost\n  port: 1883\ndevice_token: t\n"
        "rules:\n  - name: salon\n    topic: home/x\n    title: T\n"
        "    short_message: 'Valeur {champ\n",  # gabarit cassé
        "broker:\n  host: localhost\n  port: 1883\ndevice_token: t\n"
        "rules:\n  - name: salon\n    topic: home/x\n    title: T\n"
        "    short_message: M\nextra: inconnu\n",  # clé racine inconnue
    ],
    ids=["yaml-corrompu", "gabarit-casse", "cle-inconnue"],
)
async def test_invalid_yaml_rejected_service_continues(tmp_path, capsys, broken):
    """AC-2.1.2 : YAML invalide -> erreur explicite journalisée, ancienne config
    conservée (identité), service continue : le YAML valide suivant recharge."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()

    path.write_text(broken, encoding="utf-8")
    bump_mtime(path)
    await store.poll()

    assert store.get() is initial  # l'ancienne config reste active, en bloc
    assert not store.changed.is_set()
    out = capsys.readouterr().out
    assert out.count("config reload rejected") == 1
    assert ISO_UTC.match(out)

    # Le service continue : un YAML valide suivant recharge normalement.
    write_yaml(path, token="token-b")
    await store.poll()
    assert store.get().device_token == "token-b"
    assert "config reloaded" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("host", "port"),
    [("autre-hote", 1883), ("localhost", 1884)],
    ids=["host-modifie", "port-modifie"],
)
async def test_broker_change_rejected_restart_required(tmp_path, capsys, host, port):
    """broker.host/port modifiés : rejet explicite « redémarrage requis »,
    ancienne config conservée."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()

    write_yaml(path, host=host, port=port)
    await store.poll()

    assert store.get() is initial
    assert not store.changed.is_set()
    out = capsys.readouterr().out
    assert out.count("config reload rejected") == 1
    assert "redémarrage requis" in out


async def test_store_without_file_never_reloads():
    """path=None (configuration en mémoire, tests) : poll et watch sont des
    no-op, get() reste le snapshot initial."""
    config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(Rule(name="r", topic="home/x", title="T", short_message="M"),),
    )
    store = ConfigStore(config)
    stop = asyncio.Event()
    stop.set()

    await store.poll()
    await store.watch(stop)  # retourne immédiatement

    assert store.get() is config


# --- watch : tâche asyncio de sondage mtime ---


async def test_watch_reloads_until_stop(tmp_path, monkeypatch, capsys):
    """Le watcher sonde en tâche asyncio : rechargement observé sans action
    extérieure, retour propre quand stop est déclenché."""
    monkeypatch.setattr(config_module, "POLL_INTERVAL_S", 0.01)
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    stop = asyncio.Event()

    watcher = asyncio.create_task(store.watch(stop))
    write_yaml(path, token="token-b")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and store.get().device_token != "token-b":
        await asyncio.sleep(0.01)
    assert store.get().device_token == "token-b"
    assert "config reloaded" in capsys.readouterr().out

    stop.set()
    start = time.monotonic()
    await asyncio.wait_for(watcher, timeout=5)
    assert time.monotonic() - start < 4  # bien avant l'intervalle de sondage restant


async def test_watch_returns_promptly_on_stop(tmp_path, monkeypatch):
    """Stop pendant l'attente de sondage : retour immédiat, pas de hang."""
    monkeypatch.setattr(config_module, "POLL_INTERVAL_S", 30.0)
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    stop = asyncio.Event()

    watcher = asyncio.create_task(store.watch(stop))
    await asyncio.sleep(0.01)
    stop.set()
    start = time.monotonic()
    await asyncio.wait_for(watcher, timeout=2)
    assert time.monotonic() - start < 1


# --- Rotation du device token : lue au moment de l'envoi (AC-2.1.7) ---


async def test_token_rotation_applies_at_next_send(tmp_path, fcm_mock):
    """AC-2.1.7 : token modifié -> le prochain envoi utilise le nouveau token,
    sans redémarrage ; l'envoi déjà parti reste figé sur l'ancien."""
    path = write_yaml(tmp_path / "config.yaml", token="token-v1")
    store = file_store(path)
    notifier = Notifier(store)
    event = Event(
        topic="home/x",
        payload={"valeur": "1"},
        rule=Rule(name="r", topic="home/x", title="T", short_message="M"),
    )

    await notifier.notify(event)
    assert fcm_mock[0].token == "token-v1"

    write_yaml(path, token="token-v2")
    await store.poll()

    await notifier.notify(event)
    assert fcm_mock[1].token == "token-v2"
    assert fcm_mock[0].token == "token-v1"  # l'ancien envoi n'est pas rétro-touché


# --- Resynchronisation des souscriptions au swap, sans déconnexion (AC-2.1.4) ---


async def test_resync_subscriptions_without_disconnection(fake_broker, tmp_path, capsys):
    """AC-2.1.3 / AC-2.1.4 : règles ajoutée et retirée -> nouveaux topics
    abonnés, disparus désabonnés, sur la même connexion (jamais de
    déconnexion) ; un message sur un topic retiré ne déclenche plus rien."""
    episodes, clients = fake_broker
    path = write_yaml(
        tmp_path / "config.yaml",
        rules=(
            ("cave", "home/cave/temp", "Cave", "Temp: {valeur}"),
            ("salon", "home/salon/capteur", "Salon", "Valeur: {valeur}"),
        ),
    )
    store = file_store(path)
    episode = Episode(messages=[FakeMessage(b'{"valeur": "1"}', topic="home/cave/temp")])
    episodes.append(episode)
    stop = asyncio.Event()

    it = iter_messages(store, stop)
    first = await asyncio.wait_for(anext(it), timeout=5)
    assert str(first.topic) == "home/cave/temp"  # encore abonné avant le swap
    assert sorted(clients[0].subscribed) == [
        ("home/cave/temp", 0),
        ("home/salon/capteur", 0),
    ]

    # Swap : retire 'cave', ajoute 'garage' — pendant la connexion établie.
    write_yaml(
        path,
        rules=(
            ("salon", "home/salon/capteur", "Salon", "Valeur: {valeur}"),
            ("garage", "home/garage/#", "Garage", "Portail: {action}"),
        ),
    )
    await store.poll()
    assert store.changed.is_set()

    # Les deux messages suivants : le topic retiré ne doit plus rien livrer,
    # le nouveau topic doit livrer via la souscription ajoutée.
    episode.messages.append(FakeMessage(b'{"valeur": "2"}', topic="home/cave/temp"))
    episode.messages.append(FakeMessage(b'{"action": "x"}', topic="home/garage/porte"))
    second = await asyncio.wait_for(anext(it), timeout=5)

    assert str(second.topic) == "home/garage/porte"  # le topic retiré a été sauté
    assert clients[0].connected_count == 1  # jamais déconnecté, jamais reconnecté
    assert clients[0].unsubscribed == ["home/cave/temp"]
    assert sorted(clients[0].subscribed) == [
        ("home/garage/#", 0),
        ("home/salon/capteur", 0),
    ]
    out = capsys.readouterr().out
    assert "mqtt resync subscribed=home/garage/# unsubscribed=home/cave/temp qos=0" in out
    assert "mqtt disconnected" not in out
    assert "mqtt reconnected" not in out

    stop.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(it), timeout=5)


async def test_resync_without_topic_change_is_silent(fake_broker, tmp_path, capsys):
    """Rechargement sans changement de topics (libellés seulement) : aucun
    delta, aucune ligne resync, la connexion poursuit son service."""
    episodes, clients = fake_broker
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    episode = Episode(messages=[FakeMessage(b'{"valeur": "1"}')])
    episodes.append(episode)
    stop = asyncio.Event()

    it = iter_messages(store, stop)
    await asyncio.wait_for(anext(it), timeout=5)

    write_yaml(path, rules=(("salon", "home/salon/capteur", "Capteur salon v2", "V2: {valeur}"),))
    await store.poll()
    episode.messages.append(FakeMessage(b'{"valeur": "2"}'))
    message = await asyncio.wait_for(anext(it), timeout=5)  # reprise après resync vide

    assert str(message.topic) == "home/salon/capteur"
    assert clients[0].subscribed == [("home/salon/capteur", 0)]
    out = capsys.readouterr().out
    assert "mqtt resync" not in out
    assert "config reloaded" in out

    stop.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(it), timeout=5)


# --- Prise d'effet à l'événement suivant, événement figé (AC-2.1.1) ---


async def test_next_event_uses_reloaded_label(fake_broker, tmp_path):
    """AC-2.1.1 : libellé modifié -> l'événement suivant utilise le nouveau
    libellé ; l'événement en traversée reste figé sur l'ancienne règle."""
    episodes, _clients = fake_broker
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    episodes.append(
        Episode(
            messages=[
                FakeMessage(b'{"valeur": "1"}', topic="home/salon/capteur"),
                FakeMessage(b'{"valeur": "2"}', topic="home/salon/capteur"),
            ],
            drop=aiomqtt.MqttError("fin d'épisode"),
        )
    )
    stop = asyncio.Event()

    it = iter_messages(store, stop)
    first = await asyncio.wait_for(anext(it), timeout=5)
    event1 = build_event(store.get().rules, str(first.topic), message_payload(first))

    write_yaml(
        path,
        rules=(("salon", "home/salon/capteur", "Capteur salon v2", "Valeur: {valeur} (v2)"),),
    )
    await store.poll()

    second = await asyncio.wait_for(anext(it), timeout=5)
    event2 = build_event(store.get().rules, str(second.topic), message_payload(second))

    assert render_message(event1.rule, event1.payload)["title"] == "Capteur salon"
    assert render_message(event2.rule, event2.payload)["title"] == "Capteur salon v2"
    # La règle est figée dans l'événement en traversée : jamais relue.
    assert event1.rule.title == "Capteur salon"
    assert event2.rule is not event1.rule

    stop.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(it), timeout=5)


# --- Assemblage : le watcher vit dans serve, arrêt propre au signal ---


async def test_serve_reloads_at_runtime_and_stops_cleanly(
    fake_broker, tmp_path, monkeypatch, capsys
):
    """Le watcher est créé à l'assemblage : rechargement à chaud pendant le
    service, arrêt propre du watcher et du pipeline au SIGTERM."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(config_module, "POLL_INTERVAL_S", 0.01)
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    episodes.append(Episode())  # connexion silencieuse
    write_yaml(path, token="token-b")

    loop = asyncio.get_running_loop()
    loop.call_later(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))

    await asyncio.wait_for(serve(store, SilentNotifier()), timeout=5)

    out = capsys.readouterr().out
    assert "config reloaded" in out
    assert "stopped cleanly" in out


# --- Lecteur unique du YAML (AC-2.1.6), contrôlable par grep ---


def test_config_store_is_sole_yaml_reader():
    """AC-2.1.6 : seul config.py (le ConfigStore) lit le fichier YAML —
    échoue si un autre module du paquet référence yaml, ouvre un fichier
    ou appelle load_config directement."""
    package_dir = Path(mqtt_notif.__file__).parent
    for path in sorted(package_dir.glob("*.py")):
        if path.name == "config.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"\byaml\b", source), f"{path.name} référence yaml"
        assert not re.search(r"\bopen\(", source), f"{path.name} ouvre un fichier"
        assert not re.search(r"\bload_config\b", source), f"{path.name} charge la config hors store"
