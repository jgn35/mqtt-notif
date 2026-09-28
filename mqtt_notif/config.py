"""Lecture et validation du YAML minimal, avec échec rapide au démarrage.

Clés attendues : broker.host, broker.port, device_token, rules
(liste ordonnée de règles : name non vide unique, topic exact ou motif '+/#').
Le chemin du fichier vient de MQTT_NOTIF_CONFIG (variable d'environnement).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import yaml

from .rules import LEVEL_HASH, LEVEL_PLUS, Rule


class ConfigError(Exception):
    """Erreur de configuration : fichier absent/corrompu, clé manquante ou invalide."""


@dataclass(frozen=True)
class Config:
    """Configuration figée lue au démarrage (jamais relue en cours d'exécution)."""

    broker_host: str
    broker_port: int
    device_token: str
    rules: tuple[Rule, ...]


def config_path_from_env() -> str:
    """Retourne le chemin du YAML depuis MQTT_NOTIF_CONFIG (échec rapide si absent)."""
    path = os.environ.get("MQTT_NOTIF_CONFIG")
    if not path:
        raise ConfigError(
            "Variable d'environnement MQTT_NOTIF_CONFIG absente "
            "(chemin du fichier de configuration YAML requis)"
        )
    return path


def _validate_topic_pattern(topic: str, name: str) -> None:
    """Valide un topic exact ou motif MQTT ; l'erreur cite la règle fautive.

    '+' doit occuper un niveau entier, '#' doit occéder seul le dernier
    niveau, aucun espace ni niveau vide n'est toléré.
    """

    def invalid(reason: str) -> None:
        raise ConfigError(f"Règle '{name}' : topic invalide '{topic}' ({reason})")

    if any(char.isspace() for char in topic):
        invalid("espaces interdits")
    if topic.startswith("/") or topic.endswith("/") or "//" in topic:
        invalid("niveau vide")
    segments = topic.split("/")
    for position, segment in enumerate(segments):
        is_last = position == len(segments) - 1
        if LEVEL_HASH in segment:
            if segment != LEVEL_HASH or not is_last:
                invalid("'#' doit occuper seul le dernier niveau")
        elif segment != LEVEL_PLUS and LEVEL_PLUS in segment:
            invalid("'+' doit occuper un niveau entier")


def _validate_rule(entry: object, index: int) -> Rule:
    """Valide une entrée de la section rules ; l'erreur cite la règle fautive."""
    label = f"Règle #{index + 1}"
    if not isinstance(entry, dict):
        raise ConfigError(f"{label} : mapping attendu (clés 'name' et 'topic')")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(
            f"{label} : clé 'name' manquante ou invalide (chaîne non vide attendue)"
        )
    name = name.strip()
    topic = entry.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise ConfigError(
            f"Règle '{name}' : clé 'topic' manquante ou invalide (chaîne non vide attendue)"
        )
    topic = topic.strip()
    _validate_topic_pattern(topic, name)
    return Rule(name=name, topic=topic)


def _validate_rules(raw_rules: object) -> tuple[Rule, ...]:
    """Valide la section rules : liste non vide, noms uniques, motifs légaux."""
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigError(
            "Clé manquante ou invalide : 'rules' (liste non vide de règles attendue)"
        )
    seen: set[str] = set()
    validated: list[Rule] = []
    for index, entry in enumerate(raw_rules):
        rule = _validate_rule(entry, index)
        if rule.name in seen:
            raise ConfigError(
                f"Règle '{rule.name}' : nom dupliqué (noms de règles uniques requis)"
            )
        seen.add(rule.name)
        validated.append(rule)
    return tuple(validated)


def load_config(path: str) -> Config:
    """Lit et valide le YAML ; lève ConfigError avec message explicite, jamais de retour à moitié rempli."""
    try:
        with open(path, encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"Fichier de configuration introuvable : {path}") from exc
    except OSError as exc:
        raise ConfigError(f"Configuration illisible : {path} : {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Configuration illisible (UTF-8 attendu) : {path} : {exc}") from exc
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
    host = host.strip()

    port = broker.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ConfigError("Clé manquante ou invalide : 'broker.port' (entier 1-65535 attendu)")

    device_token = raw.get("device_token")
    if not isinstance(device_token, str) or not device_token.strip():
        raise ConfigError("Clé manquante ou invalide : 'device_token' (chaîne non vide attendue)")
    device_token = device_token.strip()

    rules = _validate_rules(raw.get("rules"))

    return Config(
        broker_host=host,
        broker_port=port,
        device_token=device_token,
        rules=rules,
    )


def load_config_from_env() -> Config:
    """Charge la configuration depuis MQTT_NOTIF_CONFIG ; échec rapide au démarrage."""
    return load_config(config_path_from_env())
