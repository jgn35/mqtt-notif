"""AC-1.1.5 / AC-1.1.6 / AC-1.2.3 / AC-1.2.5 / AC-1.2.6 / AC-1.3.x : assemblage,
sélection première-gagnante, payload parsé à la sélection, rendu des gabarits,
tâche d'envoi indépendante, SIGTERM -> exit 0."""

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
import firebase_admin
import pytest
from firebase_admin import messaging

import mqtt_notif.main as main_module
import mqtt_notif.mqtt as mqtt_module
from mqtt_notif.config import Config, ConfigStore
from mqtt_notif.main import build_event, main, run_pipeline, serve
from mqtt_notif.notifier import CREDENTIALS_ENV, Notifier
from mqtt_notif.render import MISSING, render_message
from mqtt_notif.rules import PAYLOAD_FORMAT_RAW, Rule

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

    async def notify(self, event):
        self.started.append(time.monotonic())
        if len(self.started) >= 3:
            self.send_started.set()
        await asyncio.sleep(self.delay)
        self.finished.append(time.monotonic())
        if len(self.finished) >= 3:
            self.all_sent.set()


class CountingNotifier:
    """Notifier compteur : une notification = un appel notify(event)."""

    def __init__(self):
        self.calls = 0
        self.events = []

    async def notify(self, event):
        self.calls += 1
        self.events.append(event)


async def test_slow_send_does_not_block_pipeline(fake_broker, config, monkeypatch):
    """Envoi FCM qui traîne : les messages suivants sont traités sans attendre."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    drop = aiomqtt.MqttError("drop")
    episodes.append(Episode(messages=[FakeMessage(b'{"n": "1"}')], drop=drop))
    episodes.append(
        Episode(messages=[FakeMessage(b'{"n": "2"}'), FakeMessage(b'{"n": "3"}')], drop=drop)
    )
    stop = asyncio.Event()
    notifier = SlowNotifier(delay=0.3)

    async def stop_when_three_started():
        await notifier.send_started.wait()
        stop.set()

    guard = asyncio.create_task(stop_when_three_started())
    start = time.monotonic()
    await asyncio.wait_for(run_pipeline(ConfigStore(config), notifier, stop), timeout=10)
    elapsed = time.monotonic() - start
    await guard

    assert len(notifier.started) == 3
    assert len(notifier.finished) == 3
    # Les trois envois ont démarré avant que le premier ne se termine :
    # la boucle n'a jamais attendu la fin d'un envoi.
    assert max(notifier.started) < min(notifier.finished)
    # Concurrent et non séquentiel : bien plus rapide que 3 x delay.
    assert elapsed < 3 * notifier.delay


def test_build_event_first_wins_with_multi_match_warning(capsys):
    """AC-1.2.3 / AC-1.2.5 : deux règles couvrent le message -> première traite, avertissement."""
    rules = (
        Rule(name="capteur", topic="home/+/capteur", title="T1", short_message="M1"),
        Rule(name="salon", topic="home/salon/+", title="T2", short_message="M2"),
    )
    event = build_event(rules, "home/salon/capteur", '{"action": "x"}')
    assert event.topic == "home/salon/capteur"
    assert event.payload == {"action": "x"}
    assert event.rule == rules[0]
    out = capsys.readouterr().out
    assert "warning" in out
    assert "2 règles correspondent" in out
    assert "rule=capteur" in out


def test_build_event_single_match_no_warning(capsys):
    rules = (
        Rule(name="capteur", topic="home/+/capteur", title="T1", short_message="M1"),
        Rule(name="garage", topic="garage/#", title="T2", short_message="M2"),
    )
    event = build_event(rules, "home/bureau/capteur", '{"a": "b"}')
    assert event.rule.name == "capteur"
    assert event.payload == {"a": "b"}
    assert "warning" not in capsys.readouterr().out


def test_build_event_no_match_returns_none(capsys):
    """Aucune correspondance : pas d'événement, pas d'avertissement multi-correspondances."""
    event = build_event(
        (Rule(name="salon", topic="home/salon/capteur", title="T", short_message="M"),),
        "home/cave/temp",
        '{"a": "b"}',
    )
    assert event is None
    assert "warning" not in capsys.readouterr().out


def test_event_is_frozen(config):
    """AC-1.2.6 : l'événement et sa règle sont immuables — toute mutation lève."""
    event = build_event(config.rules, "home/salon/capteur", '{"valeur": "1"}')
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.topic = "autre"
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.payload = {"autre": "x"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.rule.name = "autre"


def test_event_rule_snapshotted_not_reread():
    """AC-1.2.6 : la règle est figée à la sélection, jamais relue depuis la liste."""
    rules = [
        Rule(name="a", topic="home/x", title="T1", short_message="M1"),
        Rule(name="b", topic="home/y", title="T2", short_message="M2"),
    ]
    event = build_event(tuple(rules), "home/x", '{"a": "1"}')
    # La config "changerait" en mémoire de test : l'événement en cours
    # de traversée garde la règle figée à la sélection.
    rules[0] = Rule(name="zz", topic="zz", title="T", short_message="M")
    rules.pop()
    assert event.rule == Rule(name="a", topic="home/x", title="T1", short_message="M1")


# --- Story 1.3 : payload parsé à la sélection, figé dans l'événement ---


def test_build_event_parses_json_payload_at_selection():
    """AC-1.3.4 : règle json -> payload parsé en dict, figé (pas de re-parsing)."""
    event = build_event(
        (Rule(name="c", topic="home/x", title="T", short_message="{a.b}"),),
        "home/x",
        '{"a": {"b": "valeur"}}',
    )
    assert event.payload == {"a": {"b": "valeur"}}


def test_build_event_unparsable_json_payload_warns_and_renders_absent(capsys):
    """AC-1.3.2 : payload json non parsable -> dict vide + avertissement, jamais ignoré."""
    event = build_event(
        (Rule(name="c", topic="home/x", title="T", short_message="Valeur: {a}"),),
        "home/x",
        "pas du tout du json",
    )
    out = capsys.readouterr().out
    assert "payload json non parsable" in out
    assert "topic=home/x" in out
    assert "rule=c" in out
    assert event.payload == {}
    # Le rendu voit tous les champs absents : la notification part quand même.
    assert render_message(event.rule, event.payload)["short_message"] == f"Valeur: {MISSING}"


def test_build_event_non_dict_json_payload_warns_and_renders_absent(capsys):
    """JSON parsable mais non-objet (liste, scalaire) : message distinct + champs absents."""
    event = build_event(
        (Rule(name="c", topic="home/x", title="T", short_message="{a}"),),
        "home/x",
        "[1, 2]",
    )
    out = capsys.readouterr().out
    assert "payload json non-objet" in out
    assert "payload json non parsable" not in out
    assert event.payload == {}
    assert render_message(event.rule, event.payload)["short_message"] == MISSING


def test_build_event_deeply_nested_json_payload_never_crashes(capsys):
    """Payload profondément imbriqué : RecursionError capté, jamais de crash
    ni de message ignoré en bloc — rendu comme si tous les champs étaient absents."""
    deep = "[" * 100000 + "]" * 100000
    event = build_event(
        (Rule(name="c", topic="home/x", title="T", short_message="{a}"),),
        "home/x",
        deep,
    )
    out = capsys.readouterr().out
    assert "payload json non parsable" in out
    assert event.payload == {}
    assert render_message(event.rule, event.payload)["short_message"] == MISSING


def test_build_event_raw_payload_kept_as_string(capsys):
    """AC-1.3.5 : règle raw -> payload brut figé tel quel, aucun parsing."""
    event = build_event(
        (
            Rule(
                name="c",
                topic="home/x",
                payload_format=PAYLOAD_FORMAT_RAW,
                title="T",
                short_message="{payload}",
            ),
        ),
        "home/x",
        "texte brut quelconque {pas du json}",
    )
    assert "warning" not in capsys.readouterr().out
    assert event.payload == "texte brut quelconque {pas du json}"
    assert render_message(event.rule, event.payload)["short_message"] == event.payload


async def test_overlapping_filters_single_notification_per_message(fake_broker, monkeypatch, capsys):
    """AC-1.2.2 / NFR-3 : filtres en chevauchement partiel -> une seule notification par message.

    Le faux broker livre chaque message une fois par souscription correspondante :
    si la couverture laissait 'home/+/capteur' et 'home/salon/+' tous deux abonnés
    (réduction par contenance seule), le message 'home/salon/capteur' partirait
    deux fois. La fusion disjointe en 'home/+/+' ne le livre qu'une fois.
    """
    episodes, clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    overlap_config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(name="capteur", topic="home/+/capteur", title="T1", short_message="M1"),
            Rule(name="salon", topic="home/salon/+", title="T2", short_message="M2"),
        ),
    )
    drop = aiomqtt.MqttError("drop")
    episodes.append(
        Episode(
            messages=[
                FakeMessage(b'{"n": "1"}', topic="home/salon/capteur"),  # couvert par les 2 règles
                FakeMessage(b'{"n": "2"}', topic="home/bureau/capteur"),  # couvert par la 1re règle
                FakeMessage(b'{"n": "3"}', topic="home/cave/temp"),  # couvert par la fusion, aucune règle
            ],
            drop=drop,
        )
    )
    notifier = CountingNotifier()
    stop = asyncio.Event()
    task = asyncio.create_task(run_pipeline(ConfigStore(overlap_config), notifier, stop))

    seen = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        seen += capsys.readouterr().out
        if notifier.calls == 2 and "no matching rule" in seen:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert clients[0].subscribed == [("home/+/+", 0)]
    assert notifier.calls == 2
    out = seen + capsys.readouterr().out
    assert "2 règles correspondent" in out
    assert out.count("mqtt message topic=") == 3
    assert "message ignored topic=home/cave/temp no matching rule" in out
    assert "message ignored topic=home/salon/capteur" not in out


async def test_hash_rule_end_to_end_notifies_every_message(fake_broker, monkeypatch, capsys):
    """Règle attrape-tout '#' : chaque message livré produit exactement une notification.

    Deux topics sans rapport entre eux, tous deux couverts par la souscription
    unique '#' — aucun message ignoré, aucun avertissement multi-correspondances.
    """
    episodes, clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    catch_all_config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(Rule(name="tout", topic="#", title="Titre", short_message="Message"),),
    )
    episodes.append(
        Episode(
            messages=[
                FakeMessage(b'{"n": "1"}', topic="home/salon/capteur"),
                FakeMessage(b'{"n": "2"}', topic="garage/portail"),
            ],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    notifier = CountingNotifier()
    stop = asyncio.Event()
    task = asyncio.create_task(run_pipeline(ConfigStore(catch_all_config), notifier, stop))

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and notifier.calls < 2:
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert clients[0].subscribed == [("#", 0)]
    assert notifier.calls == 2
    out = capsys.readouterr().out
    assert "warning" not in out
    assert "no matching rule" not in out


async def test_two_rules_distinct_templates_two_distinct_notifications(
    fake_broker, monkeypatch, capsys
):
    """AC-1.3.3 (FR-4) : deux règles aux gabarits différents déclenchées ->
    deux notifications au contenu distinct, fidèles aux gabarits."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    two_rule_config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(
                name="bouton",
                topic="home/salon/bouton",
                title="Bouton salon",
                short_message="Bouton appuyé ({action})",
                detailed_message="Pièce: {capteur.lieu}",
            ),
            Rule(
                name="temperature",
                topic="home/salon/temperature",
                title="Température",
                short_message="{capteur.lieu} : {valeur} degrés",
            ),
        ),
    )
    episodes.append(
        Episode(
            messages=[
                FakeMessage(b'{"action": "simple"}', topic="home/salon/bouton"),
                FakeMessage(
                    b'{"capteur": {"lieu": "salon"}, "valeur": "21"}',
                    topic="home/salon/temperature",
                ),
            ],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    notifier = CountingNotifier()
    stop = asyncio.Event()
    task = asyncio.create_task(run_pipeline(ConfigStore(two_rule_config), notifier, stop))

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and notifier.calls < 2:
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert notifier.calls == 2
    by_topic = {event.topic: render_message(event.rule, event.payload) for event in notifier.events}
    assert by_topic == {
        "home/salon/bouton": {
            "title": "Bouton salon",
            "short_message": "Bouton appuyé (simple)",
            "detailed_message": f"Pièce: {MISSING}",
        },
        "home/salon/temperature": {
            "title": "Température",
            "short_message": "salon : 21 degrés",
        },
    }


async def test_pipeline_degraded_json_payload_still_notifies(fake_broker, monkeypatch, capsys):
    """AC-1.3.2 bout en bout : payload non-JSON sur une règle json -> avertissement
    journalisé, la notification part quand même, rendue tout '[absent]'."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    degraded_config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(
                name="capteur",
                topic="home/salon/capteur",
                title="Capteur",
                short_message="Valeur: {valeur}",
            ),
        ),
    )
    episodes.append(
        Episode(
            messages=[FakeMessage(b"pas du tout du json", topic="home/salon/capteur")],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    notifier = CountingNotifier()
    stop = asyncio.Event()
    task = asyncio.create_task(run_pipeline(ConfigStore(degraded_config), notifier, stop))

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and notifier.calls < 1:
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert notifier.calls == 1
    event = notifier.events[0]
    assert event.payload == {}
    assert render_message(event.rule, event.payload)["short_message"] == f"Valeur: {MISSING}"
    out = capsys.readouterr().out
    assert "payload json non parsable" in out
    assert "topic=home/salon/capteur" in out


async def test_pipeline_raw_rule_end_to_end(fake_broker, monkeypatch, capsys):
    """AC-1.3.5 bout en bout : règle raw '{payload}' -> payload brut figé à la
    sélection, rendu et envoyé sans parsing ni avertissement."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.01)
    raw_config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(
            Rule(
                name="brut",
                topic="home/brut",
                payload_format=PAYLOAD_FORMAT_RAW,
                title="Alerte",
                short_message="{payload}",
            ),
        ),
    )
    episodes.append(
        Episode(
            messages=[FakeMessage(b"porte ouverte", topic="home/brut")],
            drop=aiomqtt.MqttError("drop"),
        )
    )
    notifier = CountingNotifier()
    stop = asyncio.Event()
    task = asyncio.create_task(run_pipeline(ConfigStore(raw_config), notifier, stop))

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and notifier.calls < 1:
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert notifier.calls == 1
    event = notifier.events[0]
    assert event.payload == "porte ouverte"
    assert render_message(event.rule, event.payload) == {
        "title": "Alerte",
        "short_message": "porte ouverte",
    }
    assert "warning" not in capsys.readouterr().out


async def test_sigterm_stops_serve_cleanly(fake_broker, config, capsys):
    """SIGTERM pendant une connexion silencieuse : arrêt propre (délégation par cancellation)."""
    episodes, _clients = fake_broker
    episodes.append(Episode())  # connexion établie, aucun message
    notifier = SlowNotifier(delay=0.01)

    loop = asyncio.get_running_loop()
    loop.call_later(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))

    await asyncio.wait_for(serve(ConfigStore(config), notifier), timeout=5)

    out = capsys.readouterr().out
    assert "stopped cleanly" in out


async def test_pipeline_error_logged_and_restarted(config, monkeypatch, capsys):
    """Erreur interne du pipeline : journalisée, pipeline relancé, pas de propagation."""
    monkeypatch.setattr(main_module, "RESTART_DELAY_S", 0)
    calls = []

    async def flaky_iter_messages(config, stop, debouncer=None):
        calls.append("call")
        if len(calls) == 1:
            raise ValueError("bug interne")
        yield FakeMessage(b'{"n": "1"}')
        stop.set()

    monkeypatch.setattr(main_module, "iter_messages", flaky_iter_messages)
    notifier = SlowNotifier(delay=0.01)

    await asyncio.wait_for(serve(ConfigStore(config), notifier), timeout=5)

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

        async def notify(self, event):
            self.calls += 1
            await asyncio.Event().wait()  # ne se termine jamais seul

    notifier = StuckNotifier()
    stop = asyncio.Event()

    async def one_message_iter(config, stop, debouncer=None):
        yield FakeMessage(b'{"n": "1"}')
        stop.set()

    monkeypatch.setattr(main_module, "iter_messages", one_message_iter)

    await asyncio.wait_for(run_pipeline(ConfigStore(config), notifier, stop), timeout=2)

    assert notifier.calls == 1
    out = capsys.readouterr().out
    assert "fcm sends abandoned" in out


async def test_shutdown_drain_bounded_against_retry_loop(fake_broker, config, monkeypatch, capsys):
    """AC-1.4.5 régression : un envoi en pleine boucle de retries au moment de
    l'arrêt ne fait pas déborder le drain borné — la cancellation n'est jamais
    avalée par la boucle de retries, le pipeline revient vite et journalise
    l'abandon des envois restants."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(firebase_admin, "initialize_app", lambda credential=None, **kwargs: "fake-app")
    monkeypatch.setattr("firebase_admin.credentials.Certificate", lambda path: "credential")
    monkeypatch.setattr(
        messaging,
        "send",
        lambda *a, **kw: (_ for _ in ()).throw(messaging.QuotaExceededError("ralenti")),
    )
    monkeypatch.setenv(CREDENTIALS_ENV, "unused")
    monkeypatch.setattr("mqtt_notif.notifier.RETRY_BACKOFF_DELAYS_S", (3.0, 3.0, 3.0))
    monkeypatch.setattr(main_module, "SHUTDOWN_TIMEOUT_S", 0.1)
    episodes.append(
        Episode(messages=[FakeMessage(b'{"valeur": "1"}')], drop=aiomqtt.MqttError("drop"))
    )
    store = ConfigStore(config)
    notifier = Notifier(store)
    stop = asyncio.Event()

    task = asyncio.create_task(run_pipeline(store, notifier, stop))
    seen = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        seen += capsys.readouterr().out
        if "fcm send retry" in seen:
            break
        await asyncio.sleep(0.02)
    stop.set()
    start = time.monotonic()
    await asyncio.wait_for(task, timeout=5)
    elapsed = time.monotonic() - start

    out = seen + capsys.readouterr().out
    assert "fcm send retry" in out  # l'envoi était bien en pleine boucle de retries
    assert "fcm sends abandoned" in out
    assert "après 0.1s" in out
    assert elapsed < 1.0  # jamais les 3s de backoff restant


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
              - name: salon
                topic: home/salon/capteur
                title: Capteur salon
                short_message: 'Valeur: {valeur}'
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MQTT_NOTIF_CONFIG", str(config_path))

    async def fake_iter_messages(config, stop, debouncer=None):
        yield FakeMessage(b'{"valeur": "42"}')
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
              - name: salon
                topic: home/salon/capteur
                title: Capteur salon
                short_message: 'Valeur: {{valeur}}'
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
