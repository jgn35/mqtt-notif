"""Frontière FCM : envoi data-only validé, tronqué, résilient, via asyncio.to_thread.

Seul module qui construit et envoie des messages FCM : notify(event) rend
les gabarits de la règle figée dans l'événement (title, short_message,
detailed_message seulement si la règle le définit), valide le contrat de
l'app mcp-notif (champs non vides, tailles en octets UTF-8 bornées, charge
utile totale sous la limite FCM), tronque à la frontière de caractère
puis envoie le message data-only. Erreurs sans retry (unregistered,
invalid_argument, permission_denied et toute erreur non mappée —
conservateur) : journalisées, aucun retry. Erreurs avec retry (throttled,
internal, network_error — envoi bloqué compris) : retries à backoff borné
puis abandon journalisé. Initialisation firebase-admin paresseuse depuis
MQTT_NOTIF_FIREBASE_CREDENTIALS ; aucune erreur (rendu compris) n'arrête
jamais le service, chaque envoi reste une tâche asyncio indépendante.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING

import firebase_admin
import requests
from firebase_admin import credentials, exceptions, messaging

from . import log_line
from .render import render_message

if TYPE_CHECKING:  # évite l'import circulaire main -> notifier -> main
    from .main import Event

CREDENTIALS_ENV = "MQTT_NOTIF_FIREBASE_CREDENTIALS"

# Limites du contrat de l'app mcp-notif, comptées en octets UTF-8.
TITLE_MAX_BYTES = 100
SHORT_MESSAGE_MAX_BYTES = 500
DETAILED_MESSAGE_MAX_BYTES = 3500
CONTRACT_MAX_BYTES = {
    "title": TITLE_MAX_BYTES,
    "short_message": SHORT_MESSAGE_MAX_BYTES,
    "detailed_message": DETAILED_MESSAGE_MAX_BYTES,
}

# Limite totale de la charge utile data d'un message FCM (4 KiB) : les
# plafonds par champ totalisent 4100 octets, detailed_message sert de soupape.
FCM_DATA_MAX_BYTES = 4096

# Backoff borné : une tentative initiale puis un retry par délai, jamais plus.
# La somme (3.5s) reste sous SHUTDOWN_TIMEOUT_S de main.py (5.0s) : le drain
# d'arrêt n'attend jamais plus qu'un cycle de retries complet.
RETRY_BACKOFF_DELAYS_S = (0.5, 1.0, 2.0)

# Un appel FCM qui ne répond pas occupe sa tâche au plus SEND_TIMEOUT_S
# (le thread sous-jacent, lui, ne peut pas être interrompu).
SEND_TIMEOUT_S = 30.0

# Erreurs FCM sans retry : le réessayer ne peut pas réussir.
NO_RETRY_ERRORS = (
    (messaging.UnregisteredError, "unregistered"),
    (exceptions.InvalidArgumentError, "invalid_argument"),
    (exceptions.PermissionDeniedError, "permission_denied"),
)
# Erreurs FCM avec retry : passagères, un nouvel essai peut réussir.
RETRYABLE_ERRORS = (
    (messaging.QuotaExceededError, "throttled"),
    (exceptions.InternalError, "internal"),
    (exceptions.UnavailableError, "network_error"),
    (exceptions.DeadlineExceededError, "network_error"),
    (ConnectionError, "network_error"),
    (requests.exceptions.RequestException, "network_error"),
    (TimeoutError, "network_error"),  # SEND_TIMEOUT_S expiré : envoi bloqué
)


def _truncate_utf8(text: str, limit_bytes: int) -> str:
    """Tronque au plus grand préfixe tenant dans limit_bytes octets UTF-8.

    Coupe à la frontière de caractère : l'octet éventuellement amputé d'un
    caractère multi-octets est abandonné au décodage — jamais de séquence
    UTF-8 invalide envoyée.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    return encoded[:limit_bytes].decode("utf-8", errors="ignore")


def _error_label(exc: BaseException, policy: tuple[tuple[type[BaseException], str], ...]) -> str | None:
    """Retourne l'étiquette du contrat FCM pour l'exception selon la politique
    donnée, None si l'exception n'y figure pas."""
    for exc_type, label in policy:
        if isinstance(exc, exc_type):
            return label
    return None


class Notifier:
    """Émetteur FCM à rédacteur unique : un envoi = une tâche asyncio indépendante."""

    def __init__(self, device_token: str) -> None:
        self._device_token = device_token
        self._app: firebase_admin.App | None = None
        self._init_lock = threading.Lock()

    async def notify(self, event: "Event") -> None:
        """Rend le message depuis l'événement, valide le contrat puis envoie.

        Ne lève jamais : rendu cassé, champ vide, hors limites, identifiants
        absents, token refusé, échec réseau, envoi bloqué — tout est journalisé
        avec topic et règle pour retracer la notification fautive, le service
        continue.
        """
        try:
            data = render_message(event.rule, event.payload)
        except Exception as exc:  # noqa: BLE001 — erreurs de rendu jamais fatales
            log_line(
                f"render failed error={type(exc).__name__}: {exc} "
                f"topic={event.topic} rule={event.rule.name}"
            )
            return
        try:
            contract = self._apply_contract(data, event)
        except Exception as exc:  # noqa: BLE001 — le contrat ne lève jamais hors de ce try
            log_line(
                f"contract failed error={type(exc).__name__}: {exc} "
                f"topic={event.topic} rule={event.rule.name}"
            )
            return
        if contract is None:
            return
        await self._send_with_retries(contract, event)

    def _apply_contract(self, data: dict[str, object], event: "Event") -> dict[str, str] | None:
        """Applique le contrat mcp-notif au rendu brut, défensivement.

        Ne se fie pas au rendu : clés du contrat uniquement, valeurs coercées
        en str, champ requis vide (ou blanc) -> envoi refusé (None), clé
        detailed_message vide omise (clé non envoyée), troncature par champ
        à la frontière de caractère (journalisée), garde totale : la charge
        utile entière tient dans FCM_DATA_MAX_BYTES, detailed_message sert
        de soupape. Chaque troncature réelle est journalisée — jamais de
        perte de contenu silencieuse.
        """
        cleaned: dict[str, str] = {}
        empty_fields: list[str] = []
        for key, limit in CONTRACT_MAX_BYTES.items():
            if key not in data:
                continue
            text = data[key]
            if not isinstance(text, str):
                text = str(text)
            if not text.strip():
                if key == "detailed_message":
                    continue  # clé optionnelle : vide -> non envoyée
                empty_fields.append(key)
                continue
            cleaned[key] = text
        if empty_fields:
            log_line(
                f"fcm send refused: {', '.join(empty_fields)} vide après rendu, "
                f"notification ignorée topic={event.topic} rule={event.rule.name}"
            )
            return None
        result: dict[str, str] = {}
        for key, text in cleaned.items():
            truncated = _truncate_utf8(text, CONTRACT_MAX_BYTES[key])
            if truncated != text:
                log_line(
                    f"fcm contract truncated: {key} "
                    f"{len(text.encode('utf-8'))} -> {len(truncated.encode('utf-8'))} octets "
                    f"topic={event.topic} rule={event.rule.name}"
                )
            result[key] = truncated
        total = sum(len(value.encode("utf-8")) for value in result.values())
        overflow = total - FCM_DATA_MAX_BYTES
        if overflow > 0 and "detailed_message" in result:
            before = len(result["detailed_message"].encode("utf-8"))
            trimmed = _truncate_utf8(result["detailed_message"], max(0, before - overflow))
            log_line(
                f"fcm contract truncated: detailed_message {before} -> "
                f"{len(trimmed.encode('utf-8'))} octets (limite totale {FCM_DATA_MAX_BYTES}) "
                f"topic={event.topic} rule={event.rule.name}"
            )
            if not trimmed:
                del result["detailed_message"]
            else:
                result["detailed_message"] = trimmed
        return result

    async def _send_with_retries(self, data: dict[str, str], event: "Event") -> None:
        """Envoie via to_thread (borné par SEND_TIMEOUT_S) ; retries à backoff
        borné sur les erreurs retryables uniquement, puis abandon journalisé.

        Le backoff s'applique au seul appel FCM (jamais au rendu) et ne
        bloque jamais la boucle : il dort dans cette tâche indépendante.
        """
        delays = RETRY_BACKOFF_DELAYS_S
        attempts = len(delays) + 1
        for attempt in range(attempts):
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._send, data), timeout=SEND_TIMEOUT_S
                )
                log_line(f"fcm sent topic={event.topic}")
                return
            except Exception as exc:  # noqa: BLE001 — erreurs d'envoi jamais fatales
                label = _error_label(exc, NO_RETRY_ERRORS)
                if label is not None:
                    log_line(
                        f"fcm send failed: {label} (erreur sans retry) "
                        f"error={type(exc).__name__}: {exc} "
                        f"topic={event.topic} rule={event.rule.name}"
                    )
                    return
                label = _error_label(exc, RETRYABLE_ERRORS)
                if label is None:
                    # Erreur non mappée : conservateur, traitée sans retry.
                    log_line(
                        f"fcm send failed error={type(exc).__name__}: {exc} "
                        f"topic={event.topic} rule={event.rule.name}"
                    )
                    return
                if attempt == attempts - 1:
                    log_line(
                        f"fcm send failed: {label} abandon après {attempts} tentatives "
                        f"error={type(exc).__name__}: {exc} "
                        f"topic={event.topic} rule={event.rule.name}"
                    )
                    return
                log_line(
                    f"fcm send retry: {label} error={type(exc).__name__}: {exc} "
                    f"tentative {attempt + 2}/{attempts} dans {delays[attempt]}s "
                    f"topic={event.topic} rule={event.rule.name}"
                )
                await asyncio.sleep(delays[attempt])

    def _send(self, data: dict[str, str]) -> None:
        """Envoi bloquant firebase-admin ; s'exécute exclusivement via to_thread."""
        if not self._device_token.strip():
            raise ValueError("device token vide")
        app = self._get_app()
        message = messaging.Message(
            data=data,
            token=self._device_token,
        )
        messaging.send(message, app=app)

    def _get_app(self) -> firebase_admin.App:
        """Initialise (une fois) l'app firebase-admin depuis la variable d'environnement.

        Verrouillé : plusieurs envoi concurrents (to_thread) ne doivent jamais
        appeler initialize_app en double — firebase-admin lèverait ValueError.
        """
        with self._init_lock:
            if self._app is not None:
                return self._app
            path = os.environ.get(CREDENTIALS_ENV)
            if not path:
                raise RuntimeError(
                    f"Variable d'environnement {CREDENTIALS_ENV} absente : "
                    "identifiants firebase requis pour l'envoi"
                )
            credential = credentials.Certificate(path)
            self._app = firebase_admin.initialize_app(credential)
            return self._app
