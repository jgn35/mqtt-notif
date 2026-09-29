"""AC-1.1.2 / AC-1.1.4 / AC-1.3.8 / AC-1.3.9 : envoi FCM data-only construit
depuis le rendu de l'événement, erreurs jamais fatales."""

from __future__ import annotations

import asyncio
import threading

import firebase_admin
import pytest
from firebase_admin import messaging

from mqtt_notif.main import Event
from mqtt_notif.notifier import CREDENTIALS_ENV, Notifier
from mqtt_notif.rules import PAYLOAD_FORMAT_RAW, Rule

JSON_RULE = Rule(
    name="bouton",
    topic="home/salon/bouton",
    title="Bouton appuyé ({action})",
    short_message="Pièce: {capteur.lieu}",
    detailed_message="Valeur: {capteur.lieu}",
)
JSON_EVENT = Event(
    topic="home/salon/bouton",
    payload={"action": "simple", "capteur": {"lieu": "salon"}},
    rule=JSON_RULE,
)
MINIMAL_RULE = Rule(name="temp", topic="home/temp", title="Température", short_message="{valeur}")
MINIMAL_EVENT = Event(topic="home/temp", payload={"valeur": "21"}, rule=MINIMAL_RULE)
RAW_RULE = Rule(
    name="brut",
    topic="home/brut",
    payload_format=PAYLOAD_FORMAT_RAW,
    title="Alerte",
    short_message="{payload}",
)
RAW_EVENT = Event(topic="home/brut", payload="texte brut", rule=RAW_RULE)


@pytest.fixture
def credentials_env(monkeypatch, tmp_path):
    path = tmp_path / "service-account.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(CREDENTIALS_ENV, str(path))
    return str(path)


@pytest.fixture
def fcm_mock(monkeypatch, credentials_env):
    """Mocke la frontière firebase-admin ; capture app, messages et thread d'exécution."""
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


async def test_send_is_data_only_with_rendered_message(fcm_mock):
    """AC-1.1.2 / AC-1.3.9 : message FCM data-only construit depuis le rendu,
    clés exactes du gabarit, token visé."""
    calls, threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(JSON_EVENT)

    assert len(calls) == 1
    message = calls[0]
    assert message.data == {
        "title": "Bouton appuyé (simple)",
        "short_message": "Pièce: salon",
        "detailed_message": "Valeur: salon",
    }
    assert message.token == "token-abc"
    assert message.notification is None  # data-only, jamais de notification visible
    assert threads[0] != threading.get_ident()  # exécuté via to_thread, pas dans la boucle


async def test_send_logs_fcm_sent(fcm_mock, capsys):
    _, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)
    out = capsys.readouterr().out
    assert "fcm sent" in out
    assert "topic=home/temp" in out


async def test_detailed_message_key_only_when_rule_defines_it(fcm_mock):
    """AC-1.3.8 : sans detailed_message dans la règle, seules title et short_message."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)

    assert calls[0].data == {"title": "Température", "short_message": "21"}


async def test_send_raw_rule_renders_payload(fcm_mock):
    """Règle raw : '{payload}' substitue le payload brut figé dans l'événement."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(RAW_EVENT)

    assert calls[0].data == {"title": "Alerte", "short_message": "texte brut"}


async def test_missing_fields_render_as_absent_and_still_send(fcm_mock):
    """Champ absent -> '[absent]' substitué, notification envoyée quand même."""
    calls, _threads = fcm_mock
    event = Event(topic="home/temp", payload={}, rule=MINIMAL_RULE)
    notifier = Notifier("token-abc")
    await notifier.notify(event)

    assert calls[0].data == {"title": "Température", "short_message": "[absent]"}


async def test_send_error_logged_service_continues(fcm_mock, monkeypatch, capsys):
    """AC-1.1.4 : échec d'envoi journalisé sans crash, envoi suivant traité normalement."""
    calls, _threads = fcm_mock
    monkeypatch.setattr(messaging, "send", lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")))
    notifier = Notifier("token-abc")

    await notifier.notify(MINIMAL_EVENT)  # échec : jamais d'exception
    first_out = capsys.readouterr().out
    assert "fcm send failed" in first_out
    assert "boom" in first_out

    monkeypatch.setattr(messaging, "send", lambda *a, **kw: calls.append(a[0]))
    await notifier.notify(MINIMAL_EVENT)  # envoi suivant OK

    second_out = capsys.readouterr().out
    assert "fcm sent" in second_out
    assert len(calls) == 1


async def test_unregistered_token_logged(fcm_mock, monkeypatch, capsys):
    """Token refusé (fcm_unregistered) : erreur explicite, pas d'exception."""
    def fake_send(message, **kwargs):
        raise messaging.UnregisteredError("requested entity was not found")

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-refuse")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert "unregistered" in out
    assert "fcm send failed" in out


async def test_credentials_env_absent_logged(monkeypatch, capsys):
    """MQTT_NOTIF_FIREBASE_CREDENTIALS absent : erreur journalisée, service continue."""
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert CREDENTIALS_ENV in out
    assert "fcm send failed" in out


async def test_empty_token_logged(credentials_env, capsys):
    """Token vide : erreur explicite journalisée, jamais d'exception."""
    notifier = Notifier("   ")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert "token vide" in out


async def test_send_failure_log_traces_topic_and_rule(fcm_mock, monkeypatch, capsys):
    """Échec d'envoi : la ligne de log porte topic et règle (notification traçable)."""

    def fake_send(message, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert "fcm send failed" in out
    assert "topic=home/temp" in out
    assert "rule=temp" in out


async def test_render_failure_logged_with_distinct_label(fcm_mock, monkeypatch, capsys):
    """Erreur de rendu : étiquette 'render failed' distincte, jamais confondue
    avec un échec d'envoi FCM, et aucun envoi n'est tenté."""
    calls, _threads = fcm_mock

    def broken_render(rule, payload):
        raise RuntimeError("render casse")

    monkeypatch.setattr("mqtt_notif.notifier.render_message", broken_render)
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert "render failed" in out
    assert "render casse" in out
    assert "topic=home/temp" in out
    assert "rule=temp" in out
    assert "fcm send failed" not in out
    assert "fcm sent" not in out
    assert calls == []


async def test_initialize_app_called_once(fcm_mock, monkeypatch):
    """Plusieurs envois : firebase-admin initialisé une seule fois."""
    calls, _threads = fcm_mock
    init_calls = []

    def fake_initialize_app(credential=None, **kwargs):
        init_calls.append(credential)
        return "fake-app"

    monkeypatch.setattr(firebase_admin, "initialize_app", fake_initialize_app)
    notifier = Notifier("token-abc")
    await asyncio.gather(
        notifier.notify(MINIMAL_EVENT),
        notifier.notify(MINIMAL_EVENT),
        notifier.notify(MINIMAL_EVENT),
    )

    assert len(init_calls) == 1
    assert len(calls) == 3
