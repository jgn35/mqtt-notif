"""AC-1.1.2 / AC-1.1.4 / AC-1.3.8 / AC-1.3.9 / AC-1.4.x : envoi FCM data-only
construit depuis le rendu de l'événement, contrat mcp-notif validé et tronqué
à la frontière de caractère, politique d'erreurs retry/sans-retry, frontière
à rédacteur unique, erreurs jamais fatales."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path

import firebase_admin
import pytest
import requests
from firebase_admin import exceptions, messaging

import mqtt_notif
from mqtt_notif.main import Event, SHUTDOWN_TIMEOUT_S
from mqtt_notif.notifier import (
    CONTRACT_MAX_BYTES,
    CREDENTIALS_ENV,
    DETAILED_MESSAGE_MAX_BYTES,
    NO_RETRY_ERRORS,
    Notifier,
    RETRYABLE_ERRORS,
    RETRY_BACKOFF_DELAYS_S,
    SHORT_MESSAGE_MAX_BYTES,
    TITLE_MAX_BYTES,
)
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


# --- Story 1.4 : contrat FCM et robustesse de l'émetteur ---

LONG_TITLE_RULE = Rule(name="titre-long", topic="home/long/titre", title="é" * 60, short_message="ok")
LONG_TITLE_EVENT = Event(topic="home/long/titre", payload={}, rule=LONG_TITLE_RULE)
# 99 octets de 'a' + 'é' (2 octets) = 101 octets : la coupe à 100 ampute 'é'.
MID_CHAR_CUT_RULE = Rule(
    name="coupe-milieu", topic="home/coupe", title="a" * 99 + "é", short_message="ok"
)
MID_CHAR_CUT_EVENT = Event(topic="home/coupe", payload={}, rule=MID_CHAR_CUT_RULE)
LONG_FIELDS_RULE = Rule(
    name="champs-longs",
    topic="home/long/champs",
    title="T",
    short_message="è" * 300,
    detailed_message="é" * 2000,
)
LONG_FIELDS_EVENT = Event(topic="home/long/champs", payload={}, rule=LONG_FIELDS_RULE)
EXACT_LIMITS_RULE = Rule(
    name="limites-exactes",
    topic="home/limites",
    title="é" * 50,  # 100 octets exactement
    short_message="è" * 250,  # 500 octets exactement
    # 3400 octets : à son plafond (3500) le total déborderait la garde
    # totale FCM (100 + 500 + 3500 > 4096), voir TOTAL_OVERFLOW_RULE.
    detailed_message="ê" * 1700,
)
EXACT_LIMITS_EVENT = Event(topic="home/limites", payload={}, rule=EXACT_LIMITS_RULE)
# Les trois champs à leurs plafonds : 100 + 500 + 3500 = 4100 > 4096.
TOTAL_OVERFLOW_RULE = Rule(
    name="deborde-total",
    topic="home/deborde",
    title="é" * 50,
    short_message="è" * 250,
    detailed_message="ê" * 1750,
)
TOTAL_OVERFLOW_EVENT = Event(topic="home/deborde", payload={}, rule=TOTAL_OVERFLOW_RULE)
EMPTY_TITLE_RULE = Rule(name="titre-vide", topic="home/vide", title="", short_message="msg")
EMPTY_TITLE_EVENT = Event(topic="home/vide", payload={}, rule=EMPTY_TITLE_RULE)
EMPTY_SHORT_RULE = Rule(name="court-vide", topic="home/vide2", title="Titre", short_message="")
EMPTY_SHORT_EVENT = Event(topic="home/vide2", payload={}, rule=EMPTY_SHORT_RULE)
WHITESPACE_TITLE_RULE = Rule(name="titre-blanc", topic="home/blanc", title="   ", short_message="msg")
WHITESPACE_TITLE_EVENT = Event(topic="home/blanc", payload={}, rule=WHITESPACE_TITLE_RULE)


async def test_over_limit_title_truncated_at_char_boundary(fcm_mock):
    """AC-1.4.1 / AC-1.4.3 : title > 100 octets UTF-8 tronqué à 100 octets
    exactement, à la frontière de caractère, puis envoyé."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(LONG_TITLE_EVENT)

    assert len(calls) == 1
    title = calls[0].data["title"]
    assert title == "é" * 50  # 50 caractères × 2 octets = 100 octets exactement
    assert len(title.encode("utf-8")) == TITLE_MAX_BYTES
    assert calls[0].data["short_message"] == "ok"  # sous la limite : inchangé


async def test_truncation_never_cuts_mid_character(fcm_mock):
    """AC-1.4.3 : la coupe à 100 octets tombe au milieu d'un 'é' (2 octets) ->
    le caractère amputé est abandonné, jamais de séquence UTF-8 invalide."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(MID_CHAR_CUT_EVENT)

    title = calls[0].data["title"]
    assert title == "a" * 99  # 'é' amputé : 99 octets, la frontière la plus proche ≤ 100
    title.encode("utf-8")  # ne lève jamais : la chaîne envoyée reste de l'UTF-8 valide


async def test_short_and_detailed_messages_truncated_to_limits(fcm_mock):
    """AC-1.4.1 : short_message > 500 et detailed_message > 3500 octets ->
    tronqués à leurs limites respectives, toutes les clés dans le contrat."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(LONG_FIELDS_EVENT)

    data = calls[0].data
    assert data["short_message"] == "è" * 250
    assert data["detailed_message"] == "é" * 1750
    assert all(
        len(value.encode("utf-8")) <= CONTRACT_MAX_BYTES[key] for key, value in data.items()
    )


async def test_fields_at_exact_limits_sent_untouched(fcm_mock):
    """AC-1.4.1 : valeurs exactement aux limites par champ (100/500 octets)
    et total sous la garde FCM -> rien n'est tronqué, le message part
    byte-identique au rendu."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(EXACT_LIMITS_EVENT)

    data = calls[0].data
    assert data == {
        "title": "é" * 50,
        "short_message": "è" * 250,
        "detailed_message": "ê" * 1700,
    }
    assert len(data["title"].encode("utf-8")) == TITLE_MAX_BYTES
    assert len(data["short_message"].encode("utf-8")) == SHORT_MESSAGE_MAX_BYTES
    assert len(data["detailed_message"].encode("utf-8")) <= DETAILED_MESSAGE_MAX_BYTES


@pytest.mark.parametrize(
    "event",
    [EMPTY_TITLE_EVENT, EMPTY_SHORT_EVENT, WHITESPACE_TITLE_EVENT],
    ids=["titre-vide", "court-vide", "titre-blanc"],
)
async def test_empty_required_field_refused_no_send_then_service_continues(
    fcm_mock, capsys, event
):
    """AC-1.4.2 : title ou short_message vide (ou blanc) après rendu ->
    envoi refusé, erreur explicite journalisée, aucun appel FCM, message
    suivant traité normalement."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(event)

    out = capsys.readouterr().out
    assert "fcm send refused" in out
    assert "vide après rendu" in out
    assert f"topic={event.topic}" in out
    assert f"rule={event.rule.name}" in out
    assert "fcm sent" not in out
    assert calls == []

    await notifier.notify(MINIMAL_EVENT)  # service continue
    assert "fcm sent" in capsys.readouterr().out
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("error", "label"),
    [
        (messaging.UnregisteredError("token inconnu"), "unregistered"),
        (exceptions.InvalidArgumentError("mauvais argument"), "invalid_argument"),
        (exceptions.PermissionDeniedError("interdit"), "permission_denied"),
    ],
    ids=["unregistered", "invalid-argument", "permission-denied"],
)
async def test_no_retry_error_logged_once_no_retry(fcm_mock, monkeypatch, capsys, error, label):
    """AC-1.4.4 : unregistered / invalid_argument / permission_denied ->
    journalisées avec leur étiquette, exactement une tentative, aucun retry,
    message suivant traité normalement."""
    calls, _threads = fcm_mock
    attempts = []

    def fake_send(message, **kwargs):
        attempts.append(1)
        raise error

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert f"fcm send failed: {label} (erreur sans retry)" in out
    assert "fcm send retry" not in out
    assert f"topic={MINIMAL_EVENT.topic}" in out
    assert f"rule={MINIMAL_EVENT.rule.name}" in out
    assert len(attempts) == 1  # jamais de second essai

    monkeypatch.setattr(messaging, "send", lambda *a, **kw: calls.append(a[0]))
    await notifier.notify(MINIMAL_EVENT)  # service continue
    assert "fcm sent" in capsys.readouterr().out
    assert len(calls) == 1


def test_retry_backoff_is_bounded_by_constants():
    """NFR : le nombre de retries et les délais de backoff sont bornés par
    des constantes — jamais de retry illimité ni de délai non borné. La somme
    du cycle complet reste sous le budget de drain d'arrêt de main.py."""
    assert isinstance(RETRY_BACKOFF_DELAYS_S, tuple)
    assert 1 <= len(RETRY_BACKOFF_DELAYS_S) <= 5
    assert all(0 < delay <= 10.0 for delay in RETRY_BACKOFF_DELAYS_S)
    assert sum(RETRY_BACKOFF_DELAYS_S) < SHUTDOWN_TIMEOUT_S


def test_retry_policy_classes_are_disjoint():
    """Invariant de la politique : aucune classe d'exception ne relève des
    deux tables — la classification par isinstance reste déterministe,
    quel que soit l'ordre de parcours des politiques."""
    no_retry = {exc_type for exc_type, _label in NO_RETRY_ERRORS}
    retryable = {exc_type for exc_type, _label in RETRYABLE_ERRORS}
    assert no_retry & retryable == set()


@pytest.mark.parametrize(
    ("error_factory", "label"),
    [
        (lambda: messaging.QuotaExceededError("ralenti"), "throttled"),
        (lambda: exceptions.InternalError("panne passagère"), "internal"),
        (lambda: exceptions.UnavailableError("indisponible"), "network_error"),
        (lambda: exceptions.DeadlineExceededError("délai dépassé"), "network_error"),
        (lambda: ConnectionError("réseau coupé"), "network_error"),
        (lambda: requests.exceptions.ConnectionError("dns cassé"), "network_error"),
    ],
    ids=["throttled", "internal", "unavailable", "deadline-exceeded", "network-error", "requests-network-error"],
)
async def test_retryable_error_retried_with_backoff_then_abandoned(
    fcm_mock, monkeypatch, capsys, error_factory, label
):
    """AC-1.4.5 : throttled / internal / network_error -> retries à backoff
    borné puis abandon journalisé ; aucune exception, service continue."""
    calls, _threads = fcm_mock
    monkeypatch.setattr("mqtt_notif.notifier.RETRY_BACKOFF_DELAYS_S", (0.0, 0.0, 0.0))
    attempts = []

    def fake_send(message, **kwargs):
        attempts.append(1)
        raise error_factory()

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert len(attempts) == 1 + len((0.0, 0.0, 0.0))  # borné : 1 tentative + 3 retries
    assert f"fcm send retry: {label}" in out
    assert f"fcm send failed: {label} abandon après 4 tentatives" in out
    assert f"topic={MINIMAL_EVENT.topic}" in out
    assert f"rule={MINIMAL_EVENT.rule.name}" in out

    monkeypatch.setattr(messaging, "send", lambda *a, **kw: calls.append(a[0]))
    await notifier.notify(MINIMAL_EVENT)  # service continue
    assert "fcm sent" in capsys.readouterr().out
    assert len(calls) == 1


async def test_internal_error_retried_until_success(fcm_mock, monkeypatch, capsys):
    """AC-1.4.5 : erreur retryable passagère -> le retry porte le message,
    'fcm sent' journalisé après le rétablissement."""
    calls, _threads = fcm_mock
    monkeypatch.setattr("mqtt_notif.notifier.RETRY_BACKOFF_DELAYS_S", (0.0,))
    attempts = []

    def fake_send(message, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise exceptions.InternalError("panne passagère")
        calls.append(message)

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)

    out = capsys.readouterr().out
    assert len(attempts) == 2
    assert "fcm send retry: internal" in out
    assert "fcm send failed" not in out
    assert "fcm sent" in out
    assert len(calls) == 1


async def test_unmapped_error_is_conservative_no_retry(fcm_mock, monkeypatch, capsys):
    """Erreurs non mappées : traitées comme sans retry (conservateur),
    exactement une tentative."""
    calls, _threads = fcm_mock
    monkeypatch.setattr("mqtt_notif.notifier.RETRY_BACKOFF_DELAYS_S", (5.0, 5.0, 5.0))
    attempts = []

    def fake_send(message, **kwargs):
        attempts.append(1)
        raise KeyError("erreur inconnue")

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    await asyncio.wait_for(notifier.notify(MINIMAL_EVENT), timeout=1.0)

    out = capsys.readouterr().out
    assert len(attempts) == 1
    assert "fcm send failed error=KeyError" in out
    assert "fcm send retry" not in out


async def test_mixed_error_burst_never_crashes_and_service_continues(
    fcm_mock, monkeypatch, capsys
):
    """AC-1.4.7 : rafale d'erreurs mixtes (sans retry, avec retry, champ vide,
    message sain) -> aucun crash, le message sain part, chaque politique
    s'applique, la file enchaîne."""
    calls, _threads = fcm_mock
    monkeypatch.setattr("mqtt_notif.notifier.RETRY_BACKOFF_DELAYS_S", (0.0, 0.0, 0.0))

    def fake_send(message, **kwargs):
        title = message.data["title"]
        if title == "Unreg":
            raise messaging.UnregisteredError("token inconnu")
        if title == "BadArg":
            raise exceptions.InvalidArgumentError("mauvais argument")
        if title == "Perm":
            raise exceptions.PermissionDeniedError("interdit")
        if title == "Throttle":
            raise messaging.QuotaExceededError("ralenti")
        calls.append(message)

    monkeypatch.setattr(messaging, "send", fake_send)
    rules_and_events = [
        Event(topic="home/m1", payload={}, rule=Rule(name="m1", topic="home/m1", title="Unreg", short_message="s")),
        Event(topic="home/m2", payload={}, rule=Rule(name="m2", topic="home/m2", title="BadArg", short_message="s")),
        Event(topic="home/m3", payload={}, rule=Rule(name="m3", topic="home/m3", title="Perm", short_message="s")),
        Event(topic="home/m4", payload={}, rule=Rule(name="m4", topic="home/m4", title="Throttle", short_message="s")),
        EMPTY_TITLE_EVENT,
        MINIMAL_EVENT,
    ]
    notifier = Notifier("token-abc")
    await asyncio.gather(*(notifier.notify(event) for event in rules_and_events))

    out = capsys.readouterr().out
    assert "fcm send failed: unregistered (erreur sans retry)" in out
    assert "fcm send failed: invalid_argument (erreur sans retry)" in out
    assert "fcm send failed: permission_denied (erreur sans retry)" in out
    assert "fcm send failed: throttled abandon après 4 tentatives" in out
    assert "fcm send refused: title vide après rendu" in out
    assert "fcm sent" in out  # le message sain est parti malgré la rafale
    assert len(calls) == 1
    assert calls[0].data["title"] == "Température"


SLOW_FAIL_RULE = Rule(name="lent", topic="home/lent", title="Lent", short_message="traîne")
SLOW_FAIL_EVENT = Event(topic="home/lent", payload={}, rule=SLOW_FAIL_RULE)


async def test_slow_failing_send_does_not_block_following_sends(fcm_mock, monkeypatch, capsys):
    """AC-1.4.7 (matrice) : envoi lent puis en erreur (avec retries) ->
    l'envoi suivant aboutit pendant que le lent traîne, chaque envoi
    reste une tâche indépendante, la boucle ne bloque jamais.
    Synchronisation déterministe : le faux FCM bloque jusqu'à libération."""
    calls, _threads = fcm_mock
    monkeypatch.setattr("mqtt_notif.notifier.RETRY_BACKOFF_DELAYS_S", (0.0, 0.0, 0.0))
    slow_started = threading.Event()
    slow_release = threading.Event()

    def fake_send(message, **kwargs):
        if message.data["title"] == "Lent":
            slow_started.set()
            slow_release.wait(5)  # mock FCM qui traîne, borné par sécurité
            raise messaging.QuotaExceededError("ralenti")
        calls.append(message)

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    slow_task = asyncio.create_task(notifier.notify(SLOW_FAIL_EVENT))
    await asyncio.to_thread(slow_started.wait)  # le lent est bien en cours dans to_thread

    # L'envoi suivant aboutit pendant que le lent traîne encore.
    await asyncio.wait_for(notifier.notify(MINIMAL_EVENT), timeout=1.0)
    assert not slow_task.done()
    assert calls[0].data["title"] == "Température"

    slow_release.set()
    await asyncio.wait_for(slow_task, timeout=5)
    out = capsys.readouterr().out
    assert "fcm sent" in out
    assert "fcm send failed: throttled abandon après 4 tentatives" in out


async def test_backoff_delays_actually_slept_in_order(fcm_mock, monkeypatch, capsys):
    """Les délais réels du backoff (0.5, 1.0, 2.0) sont dormis, dans l'ordre,
    sur un cycle complet d'abandon — pas seulement déclarés."""
    calls, _threads = fcm_mock
    real_sleep = asyncio.sleep
    slept: list[float] = []

    async def recording_sleep(delay, *args, **kwargs):
        slept.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    def fake_send(message, **kwargs):
        raise messaging.QuotaExceededError("ralenti")

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    await asyncio.wait_for(notifier.notify(MINIMAL_EVENT), timeout=5)

    assert slept == [0.5, 1.0, 2.0]
    out = capsys.readouterr().out
    assert "fcm send failed: throttled abandon après 4 tentatives" in out


async def test_hung_send_bounded_by_send_timeout(fcm_mock, monkeypatch, capsys):
    """Envoi FCM qui ne répond jamais : chaque tentative est bornée par
    SEND_TIMEOUT_S (TimeoutError -> network_error, retryable), le cycle
    entier reste borné malgré le hang."""
    calls, _threads = fcm_mock
    monkeypatch.setattr("mqtt_notif.notifier.SEND_TIMEOUT_S", 0.05)
    monkeypatch.setattr("mqtt_notif.notifier.RETRY_BACKOFF_DELAYS_S", (0.0, 0.0, 0.0))
    attempts = []

    def fake_send(message, **kwargs):
        attempts.append(1)
        time.sleep(1.0)  # HTTP qui ne revient jamais

    monkeypatch.setattr(messaging, "send", fake_send)
    notifier = Notifier("token-abc")
    start = time.monotonic()
    await asyncio.wait_for(notifier.notify(MINIMAL_EVENT), timeout=5)

    assert len(attempts) == 4  # borné : le hang ne consomme qu'un cycle
    assert time.monotonic() - start < 2.0
    out = capsys.readouterr().out
    assert "fcm send retry: network_error" in out
    assert "fcm send failed: network_error abandon après 4 tentatives" in out
    assert calls == []


async def test_truncation_is_logged_with_field_topic_and_rule(fcm_mock, capsys):
    """AC-1.4.1 : jamais de perte de contenu silencieuse — chaque troncature
    réelle journalise le champ coupé, avec topic et règle."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(LONG_TITLE_EVENT)

    out = capsys.readouterr().out
    assert "fcm contract truncated: title 120 -> 100 octets" in out
    assert "topic=home/long/titre" in out
    assert "rule=titre-long" in out
    assert calls[0].data["title"] == "é" * 50


async def test_total_payload_kept_under_fcm_limit_via_detailed_valve(fcm_mock, capsys):
    """Les trois champs à leurs plafonds totalisent 4100 octets > limite FCM
    (4096) : la garde totale trimé detailed_message (soupape) à la frontière
    de caractère, les autres champs restent à leurs plafonds."""
    calls, _threads = fcm_mock
    notifier = Notifier("token-abc")
    await notifier.notify(TOTAL_OVERFLOW_EVENT)

    data = calls[0].data
    assert data["title"] == "é" * 50
    assert data["short_message"] == "è" * 250
    assert data["detailed_message"] == "ê" * 1748  # 3496 octets : 3500 - 4 de débordement
    assert sum(len(value.encode("utf-8")) for value in data.values()) <= 4096
    out = capsys.readouterr().out
    assert "fcm contract truncated: detailed_message 3500 -> 3496 octets (limite totale 4096)" in out


@pytest.mark.parametrize("detailed", ["", "   "], ids=["vide", "blanc"])
async def test_empty_detailed_message_key_not_sent(fcm_mock, detailed):
    """AC-1.3.9 : detailed_message vide (ou blanc) après rendu -> clé non
    envoyée, comme une règle qui ne le définit pas."""
    calls, _threads = fcm_mock
    rule = Rule(
        name="detail-vide",
        topic="home/detail",
        title="T",
        short_message="s",
        detailed_message=detailed,
    )
    event = Event(topic="home/detail", payload={}, rule=rule)
    notifier = Notifier("token-abc")
    await notifier.notify(event)

    assert calls[0].data == {"title": "T", "short_message": "s"}


async def test_contract_defensive_against_unexpected_render_output(fcm_mock, monkeypatch):
    """Le bloc validation/truncation ne lève jamais : clé inconnue ignorée,
    valeur non-string coercée, clé detailed_message vide omise — aucun échec
    ne fuit hors de notify()."""
    calls, _threads = fcm_mock
    monkeypatch.setattr(
        "mqtt_notif.notifier.render_message",
        lambda rule, payload: {
            "title": "Titre",
            "short_message": 21,  # non-string : coercé, pas d'AttributeError
            "detailed_message": "   ",  # vide : clé omise
            "hors_contrat": "x",  # clé inconnue : ignorée
        },
    )
    notifier = Notifier("token-abc")
    await notifier.notify(MINIMAL_EVENT)  # ne lève jamais

    assert calls[0].data == {"title": "Titre", "short_message": "21"}


def test_only_notifier_module_references_firebase():
    """AC-1.4.6 : frontière à rédacteur unique — seul notifier.py référence
    firebase_admin/messaging dans le paquet (grep automatisé : ce test échoue
    si un autre module franchit la frontière)."""
    package_dir = Path(mqtt_notif.__file__).parent
    for path in sorted(package_dir.glob("*.py")):
        if path.name == "notifier.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "firebase_admin" not in source, f"{path.name} référence firebase_admin"
        assert not re.search(r"\bmessaging\b", source), f"{path.name} référence messaging"
