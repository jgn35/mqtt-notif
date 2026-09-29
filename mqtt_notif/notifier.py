"""Frontière FCM : envoi data-only construit depuis le rendu, via asyncio.to_thread.

Seul module qui construit et envoie des messages FCM : notify(event) rend
les gabarits de la règle figée dans l'événement (title, short_message,
detailed_message seulement si la règle le définit) et envoie le message
data-only. Initialisation firebase-admin paresseuse depuis
MQTT_NOTIF_FIREBASE_CREDENTIALS ; toute erreur (rendu compris) est
journalisée sans retry et ne fait jamais planter le service.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING

import firebase_admin
from firebase_admin import credentials, messaging

from . import log_line
from .render import render_message

if TYPE_CHECKING:  # évite l'import circulaire main -> notifier -> main
    from .main import Event

CREDENTIALS_ENV = "MQTT_NOTIF_FIREBASE_CREDENTIALS"


class Notifier:
    """Émetteur FCM à rédacteur unique : un envoi = une tâche asyncio indépendante."""

    def __init__(self, device_token: str) -> None:
        self._device_token = device_token
        self._app: firebase_admin.App | None = None
        self._init_lock = threading.Lock()

    async def notify(self, event: "Event") -> None:
        """Rend le message depuis l'événement puis envoie le FCM via to_thread.

        Ne lève jamais : toute erreur (rendu, identifiants absents, token
        refusé, échec réseau) est journalisée, le service continue.
        """
        try:
            data = render_message(event.rule, event.payload)
            await asyncio.to_thread(self._send, data)
            log_line(f"fcm sent topic={event.topic}")
        except messaging.UnregisteredError:
            log_line(
                "fcm send failed: device token absent ou refusé (unregistered), "
                "messages suivants traités normalement"
            )
        except Exception as exc:  # noqa: BLE001 — erreurs d'envoi jamais fatales
            log_line(f"fcm send failed error={type(exc).__name__}: {exc}")

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
