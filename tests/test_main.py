"""AC-1.1.5 / AC-1.1.6 : assemblage, tâche d'envoi indépendante, SIGTERM -> exit 0.
AC-1.2.3 / AC-1.2.5 / AC-1.2.6 : sélection première-gagnante, au plus une
notification par message, événement immuable portant la règle figée."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import select
import signal
import socket
import subprocess
import sys
import textwrap
import time

import aiomqtt
import pytest

import mqtt_notif.main as main_module
import mqtt_notif.mqtt as mqtt_module
from mqtt_notif.config import Config
from mqtt_notif.main import Event, build_event, main, run_pipeline, serve
from mqtt_notif.rules import Rule

from .conftest import Episode, FakeMessage

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SlowNotifier:
    """Notifier lent : enregistre les démarrages et les fins d'envoi."""

    def __init__(self, delay=0.3):
        self.delay = delay
        self.started: list[float] = []
        self.finished: list[float] = []
        self.send_started = asyncio.Event()
        self.all_sent = asyncio.Event()

    async def notify(self):
        self.started.append(time.monotonic())
        if len(self.started) >= 3:
            self.send_started.set()
        await asyncio.sleep(self.delay)
        self.finished.append(time.monotonic())
        if len(self.finished) >= 3:
            self.all_sent.set()


async def test_slow_send_does_not_block_pipeline(fake_broker, config, monkeypatch):
    """Envoi FCM qui traîne : les messages suivants sont traités sans attendre."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    drop = aiomqtt.MqttError("drop")
    episodes.append(Episode(messages=[FakeMessage(b"1")], drop=drop))
    episodes.append(Episode(messages=[FakeMessage(b"2"), FakeMessage(b"3")], drop=drop))
    stop = asyncio.Event()
    notifier = SlowNotifier(delay=0.3)

    async def stop_when_three_started():
        await notifier.send_started.wait()
        stop.set()

    guard = asyncio.create_task(stop_when_three_started())
    start = time.monotonic()
    await asyncio.wait_for(run_pipeline(config, notifier, stop), timeout=10)
    elapsed = time.monotonic() - start
    await guard

    assert len(notifier.started) == 3
    assert len(notifier.finished) == 3
    # Les trois envois ont démarré avant que le premier ne se termine :
    # la boucle n'a jamais attendu la fin d'un envoi.
    assert max(notifier.started) < min(notifier.finished)
    # Concurrent et non séquentiel : bien plus rapide que 3 x delay.
    assert elapsed < 3 * notifier.delay


async def test_sigterm_stops_serve_cleanly(fake_broker, config, capsys):
    """SIGTERM pendant une connexion silencieuse : arrêt propre (délégation par cancellation)."""
    episodes, _clients = fake_broker
    episodes.append(Episode())  # connexion établie, aucun message
    notifier = SlowNotifier(delay=0.01)

    loop = asyncio.get_running_loop()
    loop.call_later(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))

    await asyncio.wait_for(serve(config, notifier), timeout=5)

    out = capsys.readouterr().out
    assert "stopped cleanly" in out


async def test_pipeline_error_logged_and_restarted(config, monkeypatch, capsys):
    """Erreur interne du pipeline : journalisée, pipeline relancé, pas de propagation."""
    monkeypatch.setattr(main_module, "RESTART_DELAY_S", 0)
    calls = []

    async def flaky_iter_messages(config, stop):
        calls.append("call")
        if len(calls) == 1:
            raise ValueError("bug interne")
        yield FakeMessage(b"x")
        stop.set()

    monkeypatch.setattr(main_module, "iter_messages", flaky_iter_messages)
    notifier = SlowNotifier(delay=0.01)

    await asyncio.wait_for(serve(config, notifier), timeout=5)

    out = capsys.readouterr().out
    assert "pipeline error (restarting)" in out
    assert "bug interne" in out
    assert len(calls) == 2
    assert "stopped cleanly" in out


async def test_shutdown_drain_is_bounded(config, monkeypatch, capsys):
    """SHUTDOWN_TIMEOUT_S expiré : les envois récalcitrants sont annulés et journalisés."""
    monkeypatch.setattr(main_module, "SHUTDOWN_TIMEOUT_S", 0.05)

    class StuckNotifier:
        def __init__(self):
            self.calls = 0

        async def notify(self):
            self.calls += 1
            await asyncio.Event().wait()  # ne se termine jamais seul

    notifier = StuckNotifier()
    stop = asyncio.Event()

    async def one_message_iter(config, stop):
        yield FakeMessage(b"1")
        stop.set()

    monkeypatch.setattr(main_module, "iter_messages", one_message_iter)

    await asyncio.wait_for(run_pipeline(config, notifier, stop), timeout=2)

    assert notifier.calls == 1
    out = capsys.readouterr().out
    assert "fcm sends abandoned" in out


class CountingNotifier:
    """Notifier qui compte les notifications déclenchées."""

    def __init__(self):
        self.calls = 0

    async def notify(self):
        self.calls += 1


def _multi_rule_config() -> Config:
    return Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(name="exact", topic="home/salon/capteur"),
            Rule(name="motif", topic="home/#"),
        ),
    )


async def _drain_pipeline(config, notifier, stop, timeout=5):
    """Lance le pipeline et déclenche l'arrêt peu après le démarrage.

    Le faux broker se reconnecte indéfiniment ; un arrêt planifié borne
    le test (le vrai service, lui, tournerait sans fin).
    """
    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    guard = asyncio.create_task(stop_soon())
    await asyncio.wait_for(run_pipeline(config, notifier, stop), timeout=timeout)
    await guard


async def test_pipeline_matching_message_triggers_one_notification(fake_broker, config, monkeypatch):
    """AC-1.2.3 : message couvert par une règle -> un événement, une notification."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    episodes.append(
        Episode(
            messages=[
                FakeMessage(b"couvert", topic="home/salon/capteur"),
                FakeMessage(b"ignore", topic="garage/porte"),
            ],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    stop = asyncio.Event()
    notifier = CountingNotifier()

    await _drain_pipeline(config, notifier, stop)

    assert notifier.calls == 1


async def test_pipeline_at_most_one_notification_on_overlap(fake_broker, capsys, monkeypatch):
    """AC-1.2.5 : deux règles couvrent le même message -> première seule, avertissement, un envoi."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    episodes.append(
        Episode(
            messages=[FakeMessage(b"double", topic="home/salon/capteur")],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    stop = asyncio.Event()
    notifier = CountingNotifier()
    config = _multi_rule_config()

    await _drain_pipeline(config, notifier, stop)

    assert notifier.calls == 1
    out = capsys.readouterr().out
    assert "warning" in out
    assert "2 règles correspondent" in out
    assert "rule=exact" in out


async def test_pipeline_ignores_unmatched_message_with_dedicated_log(
    fake_broker, config, capsys, monkeypatch
):
    """AC-1.2.6 : aucune règle correspondante -> aucune notification, ligne dédiée."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    episodes.append(
        Episode(
            messages=[FakeMessage(b"x", topic="garage/porte")],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    stop = asyncio.Event()
    notifier = CountingNotifier()

    await _drain_pipeline(config, notifier, stop)

    assert notifier.calls == 0
    out = capsys.readouterr().out
    assert "message ignored topic=garage/porte" in out


def test_build_event_returns_none_when_no_rule_matches(config):
    event = build_event(config.rules, "garage/porte", "x")
    assert event is None


def test_event_is_frozen(config):
    """Toute mutation de l'événement ou de la règle figée lève."""
    event = build_event(config.rules, "home/salon/capteur", "x")
    assert event is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.topic = "autre/chose"
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.payload = "y"
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.rule = Rule(name="autre", topic="home/#")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.rule.name = "autre"


async def test_pipeline_builds_event_with_rule_frozen_at_selection(
    fake_broker, config, monkeypatch
):
    """AC-1.2.3 : l'événement porte la règle figée à la sélection, jamais relue.

    On intercepte l'étape émetteur : l'événement reçu est bien celui construit
    à la sélection, avec la règle d'origine — une « nouvelle config » chargée
    en mémoire de test n'y change rien, et le notifier n'est pas rappelé au
    ConfigStore.
    """
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    episodes.append(
        Episode(
            messages=[FakeMessage(b"payload-1", topic="home/salon/capteur")],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    stop = asyncio.Event()
    recorded: list[Event] = []

    async def record_dispatch(notifier, event):
        recorded.append(event)
        stop.set()

    monkeypatch.setattr(main_module, "_dispatch", record_dispatch)

    class UnusedNotifier:
        async def notify(self):
            raise AssertionError("le contenu fixe ne lit pas l'événement ici")

    await asyncio.wait_for(run_pipeline(config, UnusedNotifier(), stop), timeout=5)

    assert len(recorded) == 1
    event = recorded[0]
    assert event.topic == "home/salon/capteur"
    assert event.payload == "payload-1"
    assert event.rule is config.rules[0]
    assert event.rule.name == "capteur"
    # La config « change en mémoire de test » : l'événement déjà en traversée
    # garde sa règle figée, sans relecture.
    fresh_config = Config(
        broker_host="h",
        broker_port=1,
        device_token="t",
        rules=(Rule(name="autre", topic="home/#"),),
    )
    assert event.rule is not fresh_config.rules[0]
    assert event.rule.name == "capteur"


def test_main_returns_zero_on_valid_config(fake_broker, monkeypatch, tmp_path, capsys):
    """Démarrage valide : pipeline exécuté, arrêt propre, exit code 0."""
    episodes, _clients = fake_broker
    episodes.append(Episode(messages=[FakeMessage(b"event")], drop=aiomqtt.MqttError("drop")))
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    monkeypatch.delenv("MQTT_NOTIF_FIREBASE_CREDENTIALS", raising=False)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            """\
            broker:
              host: localhost
              port: 1883
            device_token: token-abc
            rules:
              - name: capteur-salon
                topic: home/salon/capteur
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MQTT_NOTIF_CONFIG", str(config_path))

    async def fake_iter_messages(config, stop):
        yield FakeMessage(b"event")
        stop.set()

    monkeypatch.setattr("mqtt_notif.main.iter_messages", fake_iter_messages)

    assert main() == 0
    out = capsys.readouterr().out
    # L'envoi réel a été tenté puis journalisé (identifiants absents), sans crash.
    assert "MQTT_NOTIF_FIREBASE_CREDENTIALS" in out


@pytest.mark.parametrize(
    "yaml_content",
    [
        "broker:\n  port: 1883\n",  # clé host manquante
        "ceci n'est pas : [du yaml\n",  # corrompu
        "broker:\n  host: localhost\n  port: 1883\n",  # device_token/rules manquants
        "broker:\n  host: localhost\n  port: 1883\ndevice_token: t\nrules:\n  - name: ''\n    topic: a\n",  # règle fautive
    ],
)
def test_main_fails_fast_on_invalid_config(monkeypatch, tmp_path, capsys, yaml_content):
    """YAML absent/corrompu/clé manquante : erreur explicite, exit code non nul."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_content, encoding="utf-8")
    monkeypatch.setenv("MQTT_NOTIF_CONFIG", str(config_path))

    assert main() == 1
    assert "erreur de configuration" in capsys.readouterr().err


def test_main_fails_fast_when_env_absent(monkeypatch, capsys):
    monkeypatch.delenv("MQTT_NOTIF_CONFIG", raising=False)
    assert main() == 1
    assert "erreur de configuration" in capsys.readouterr().err


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_sigterm_subprocess_exit_code_zero(tmp_path):
    """Bout en bout réel : broker injoignable -> backoff croissant, SIGTERM -> exit 0.

    Broker volontairement injoignable (port libre) : aucune dépendance externe,
    la boucle de reconnexion et l'arrêt propre sont observés sur le vrai process.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""\
            broker:
              host: 127.0.0.1
              port: {_free_port()}
            device_token: token-abc
            rules:
              - name: capteur-salon
                topic: home/salon/capteur
            """
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["MQTT_NOTIF_CONFIG"] = str(config_path)
    env.pop("MQTT_NOTIF_FIREBASE_CREDENTIALS", None)

    proc = subprocess.Popen(
        [sys.executable, "-m", "mqtt_notif.main"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=PROJECT_ROOT,
        env=env,
    )
    try:
        deadline = time.monotonic() + 20
        seen: list[str] = []
        fd = proc.stdout.fileno()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            seen.append(line.strip())
            if "retry in 2.0s" in line:
                break
        assert any("mqtt connect failed" in line for line in seen), seen
        assert any("retry in 1.0s" in line for line in seen), seen
        assert any("retry in 2.0s" in line for line in seen), seen  # backoff croissant

        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    remaining = proc.stdout.read()

    assert returncode == 0
    assert "stopped cleanly" in remaining
    # Timestamps ISO 8601 UTC sur stdout.
    assert all(line.startswith("[2") for line in seen if line)
