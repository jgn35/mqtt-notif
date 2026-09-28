"""Frontière FCM : envoi data-only à message fixe via asyncio.to_thread.

Seul module qui construit et envoie des messages FCM. Initialisation
firebase-admin paresseuse depuis MQTT_NOTIF_FIREBASE_CREDENTIALS ; toute erreur
d'envoi est journalisée sans retry et ne fait jamais planter le service.
"""

from __future__ import annotations

import asyncio
import os
import threading

import firebase_admin
from firebase_admin import credentials, messaging

from . import log_line

CREDENTIALS_ENV = "MQTT_NOTIF_FIREBASE_CREDENTIALS"
FIXED_TITLE = "mqtt-notif"
FIXED_SHORT_MESSAGE = "Événement reçu"


class Notifier:
    """Émetteur FCM à rédacteur unique : un envoi = une tâche asyncio indépendante."""

    def __init__(self, device_token: str) -> None:
        self._device_token = device_token
        self._app: firebase_admin.App | None = None
        self._init_lock = threading.Lock()

    async def notify(self) -> None:
        """Déclenche l'envoi du message FCM fixe dans un thread via to_thread.

        Ne lève jamais : toute erreur (identifiants absents, token refusé,
        échec réseau) est journalisée, le service continue.
        """
        try:
            await asyncio.to_thread(self._send)
            log_line("fcm sent")
        except messaging.UnregisteredError:
            log_line(
                "fcm send failed: device token absent ou refusé (unregistered), "
                "messages suivants traités normalement"
            )
        except Exception as exc:  # noqa: BLE001 — erreurs d'envoi jamais fatales
            log_line(f"fcm send failed error={type(exc).__name__}: {exc}")

    def _send(self) -> None:
        """Envoi bloquant firebase-admin ; s'exécute exclusivement via to_thread."""
        if not self._device_token.strip():
            raise ValueError("device token vide")
        app = self._get_app()
        message = messaging.Message(
            data={
                "title": FIXED_TITLE,
                "short_message": FIXED_SHORT_MESSAGE,
            },
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
