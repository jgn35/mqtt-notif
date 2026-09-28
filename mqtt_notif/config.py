"""Lecture et validation du YAML minimal, avec échec rapide au démarrage.

Clés attendues : broker.host, broker.port, device_token, topic.
Le chemin du fichier vient de MQTT_NOTIF_CONFIG (variable d'environnement).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import yaml


class ConfigError(Exception):
    """Erreur de configuration : fichier absent/corrompu, clé manquante ou invalide."""


@dataclass(frozen=True)
class Config:
    """Configuration figée lue au démarrage (jamais relue en cours d'exécution)."""

    broker_host: str
    broker_port: int
    device_token: str
    topic: str


def config_path_from_env() -> str:
    """Retourne le chemin du YAML depuis MQTT_NOTIF_CONFIG (échec rapide si absent)."""
    path = os.environ.get("MQTT_NOTIF_CONFIG")
    if not path:
        raise ConfigError(
            "Variable d'environnement MQTT_NOTIF_CONFIG absente "
            "(chemin du fichier de configuration YAML requis)"
        )
    return path


def load_config(path: str) -> Config:
    """Lit et valide le YAML ; lève ConfigError avec message explicite, jamais de retour à moitié rempli."""
    try:
        with open(path, encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"Fichier de configuration introuvable : {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path} : {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"YAML invalide dans {path} : un mapping est attendu en racine")

    broker = raw.get("broker")
    if not isinstance(broker, dict):
        raise ConfigError("Clé manquante ou invalide : 'broker' (mapping attendu)")

    host = broker.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("Clé manquante ou invalide : 'broker.host' (chaîne non vide attendue)")

    port = broker.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError("Clé manquante ou invalide : 'broker.port' (entier 1-65535 attendu)")

    device_token = raw.get("device_token")
    if not isinstance(device_token, str) or not device_token.strip():
        raise ConfigError("Clé manquante ou invalide : 'device_token' (chaîne non vide attendue)")

    topic = raw.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise ConfigError("Clé manquante ou invalide : 'topic' (chaîne non vide attendue)")

    return Config(
        broker_host=host,
        broker_port=port,
        device_token=device_token,
        topic=topic,
    )


def load_config_from_env() -> Config:
    """Charge la configuration depuis MQTT_NOTIF_CONFIG ; échec rapide au démarrage."""
    return load_config(config_path_from_env())
