"""AC-1.1.2 / AC-1.1.4 : envoi FCM data-only à message fixe, erreurs jamais fatales."""

from __future__ import annotations

import asyncio
import threading

import firebase_admin
import pytest
from firebase_admin import messaging

from mqtt_notif.notifier import (
    CREDENTIALS_ENV,
    FIXED_SHORT_MESSAGE,
    FIXED_TITLE,
    Notifier,
)


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


async def test_send_is_data_only_with_fixed_message(fcm_mock):
    """AC-1.1.2 : message FCM data-only, clés exactement title/short_message, token visé."""
    calls, threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify()

    assert len(calls) == 1
    message = calls[0]
    assert message.data == {"title": FIXED_TITLE, "short_message": FIXED_SHORT_MESSAGE}
    assert message.data["title"] == "mqtt-notif"
    assert message.data["short_message"] == "Événement reçu"
    assert message.token == "token-abc"
    assert message.notification is None  # data-only, jamais de notification visible
    assert threads[0] != threading.get_ident()  # exécuté via to_thread, pas dans la boucle


async def test_send_error_logged_service_continues(fcm_mock, monkeypatch, capsys):
    """AC-1.1.4 : échec d'envoi journalisé sans crash, envoi suivant traité normalement."""
    calls, _threads = fcm_mock
    monkeypatch.setattr(messaging, "send", lambda *a, **kw: (_ for _ in ()).throw(ValueError("boom")))
    notifier = Notifier("token-abc")

    await notifier.notify()  # échec : jamais d'exception
    first_out = capsys.readouterr().out
    assert "fcm send failed" in first_out
    assert "boom" in first_out

    monkeypatch.setattr(messaging, "send", lambda *a, **kw: calls.append(a[0]))
    await notifier.notify()  # envoi suivant OK

    second_out = capsys.readouterr().out
    assert "fcm sent" in second_out
    assert len(calls) == 1


async def test_unregistered_token_logged(fcm_mock, monkeypatch, capsys):
    """Token refusé (fcm_unregistered) : erreur explicite, pas d'exception."""
    def fake_send(message, **kwargs):
        raise messaging.UnregisteredError("requested entity was not found")

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-refuse")
    await notifier.notify()

    out = capsys.readouterr().out
    assert "unregistered" in out
    assert "fcm send failed" in out


async def test_credentials_env_absent_logged(monkeypatch, capsys):
    """MQTT_NOTIF_FIREBASE_CREDENTIALS absent : erreur journalisée, service continue."""
    monkeypatch.delenv(CREDENTIALS_ENV, raising=False)
    notifier = Notifier("token-abc")
    await notifier.notify()

    out = capsys.readouterr().out
    assert CREDENTIALS_ENV in out
    assert "fcm send failed" in out


async def test_empty_token_logged(credentials_env, capsys):
    """Token vide : erreur explicite journalisée, jamais d'exception."""
    notifier = Notifier("   ")
    await notifier.notify()

    out = capsys.readouterr().out
    assert "token vide" in out


async def test_initialize_app_called_once(fcm_mock, monkeypatch):
    """Plusieurs envois : firebase-admin initialisé une seule fois."""
    calls, _threads = fcm_mock
    init_calls = []

    def fake_initialize_app(credential=None, **kwargs):
        init_calls.append(credential)
        return "fake-app"

    monkeypatch.setattr(firebase_admin, "initialize_app", fake_initialize_app)
    notifier = Notifier("token-abc")
    await asyncio.gather(notifier.notify(), notifier.notify(), notifier.notify())

    assert len(init_calls) == 1
    assert len(calls) == 3
