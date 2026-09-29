"""Lecture et validation du YAML minimal, avec échec rapide au démarrage.

Clés attendues : broker.host, broker.port, device_token, rules
(liste ordonnée de règles : name non vide unique, topic exact ou motif
'+'/'#' légal, payload_format 'json' par défaut ou 'raw', gabarits
title/short_message non vides, detailed_message optionnel — syntaxe des
gabarits validée ici, jamais seulement au rendu). Le chemin du fichier
vient de MQTT_NOTIF_CONFIG (variable d'environnement).

Le ConfigStore est l'unique lecteur du fichier YAML : il détient la
configuration courante, la recharge à chaud par sondage mtime (tâche
asyncio, aucune écriture du fichier) et la remplace en une opération
atomique. Un YAML invalide (ou broker.host/port modifiés) est rejeté avec
erreur explicite, l'ancienne configuration reste active.
"""

from __future__ import annotations

import asyncio
import os
import unicodedata
from dataclasses import dataclass

import yaml

from . import log_line
from .render import TemplateError, validate_template
from .rules import (
    LEVEL_HASH,
    LEVEL_PLUS,
    PAYLOAD_FORMAT_JSON,
    PAYLOAD_FORMAT_RAW,
    Rule,
    subscription_topics,
)

# Période de sondage mtime du fichier YAML (constante d'implémentation) :
# le changement prend effet à l'événement suivant, la latence du sondage
# est masquée par le « prochain événement ».
POLL_INTERVAL_S = 1.0


class ConfigError(Exception):
    """Erreur de configuration : fichier absent/corrompu, clé manquante ou invalide."""


@dataclass(frozen=True)
class Config:
    """Configuration figée : jamais mutée en place, remplacée en bloc par
    le ConfigStore (swap atomique) — tout lecteur voit intégralement
    l'ancienne ou la nouvelle, jamais un mélange."""

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

    '+' doit occuper un niveau entier, '#' doit occuper seul le dernier
    niveau ; espaces et caractères de contrôle interdits (le broker
    rejetterait le SUBSCRIBE et le démon bouclerait en reconnexion).
    """

    def invalid(reason: str) -> None:
        raise ConfigError(f"Règle '{name}' : topic invalide '{topic}' ({reason})")

    if any(char.isspace() for char in topic):
        invalid("espaces interdits")
    if any(unicodedata.category(char) in ("Cc", "Cf") for char in topic):
        invalid("caractères de contrôle ou de format interdits")
    segments = topic.split("/")
    for position, segment in enumerate(segments):
        is_last = position == len(segments) - 1
        if LEVEL_HASH in segment and (segment != LEVEL_HASH or not is_last):
            invalid("'#' doit occuper seul le dernier niveau")
        if segment != LEVEL_PLUS and LEVEL_PLUS in segment:
            invalid("'+' doit occuper un niveau entier")


RULE_KEYS = ("name", "topic", "payload_format", "title", "short_message", "detailed_message")
TEMPLATE_FIELDS = ("title", "short_message", "detailed_message")


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
    unknown_keys = sorted(set(entry) - set(RULE_KEYS), key=str)
    if unknown_keys:
        raise ConfigError(
            f"Règle '{name}' : clé(s) inconnue(s) {unknown_keys} "
            f"(clés {list(RULE_KEYS)} attendues)"
        )
    topic = entry.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        raise ConfigError(
            f"Règle '{name}' : clé 'topic' manquante ou invalide (chaîne non vide attendue)"
        )
    topic = topic.strip()
    _validate_topic_pattern(topic, name)

    payload_format = entry.get("payload_format", PAYLOAD_FORMAT_JSON)
    if payload_format not in (PAYLOAD_FORMAT_JSON, PAYLOAD_FORMAT_RAW):
        raise ConfigError(
            f"Règle '{name}' : clé 'payload_format' invalide '{payload_format}' "
            f"('{PAYLOAD_FORMAT_JSON}' ou '{PAYLOAD_FORMAT_RAW}' attendu)"
        )

    templates: dict[str, str] = {}
    for field in TEMPLATE_FIELDS:
        template = entry.get(field)
        if template is None:
            if field == "detailed_message":
                continue
            raise ConfigError(
                f"Règle '{name}' : clé '{field}' manquante (gabarit non vide attendu)"
            )
        if not isinstance(template, str) or not template.strip():
            raise ConfigError(
                f"Règle '{name}' : clé '{field}' manquante ou invalide "
                "(gabarit non vide attendu)"
            )
        templates[field] = template.strip()
    try:
        for field, template in templates.items():
            validate_template(template, payload_format, name, field)
    except TemplateError as exc:
        raise ConfigError(str(exc)) from exc

    return Rule(
        name=name,
        topic=topic,
        payload_format=payload_format,
        title=templates["title"],
        short_message=templates["short_message"],
        detailed_message=templates.get("detailed_message"),
    )


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

    unknown_root_keys = sorted(set(raw) - {"broker", "device_token", "rules"}, key=str)
    if unknown_root_keys:
        hint = (
            " ('topic' a été remplacé par la section 'rules')"
            if "topic" in unknown_root_keys
            else ""
        )
        raise ConfigError(f"Clé(s) inconnue(s) à la racine : {unknown_root_keys}{hint}")

    return Config(
        broker_host=host,
        broker_port=port,
        device_token=device_token,
        rules=rules,
    )


def _stat(path: str) -> tuple[int, int]:
    """Signature de sondage : (mtime_ns, taille). La taille couvre les
    réécritures à mtime identique (granularité du système de fichiers)."""
    stat = os.stat(path)
    return stat.st_mtime_ns, stat.st_size


class ConfigStore:
    """Unique lecteur du YAML : détient la configuration courante et la
    recharge à chaud par sondage mtime.

    `get()` renvoie la Config figée courante : le swap est une simple
    réaffectation d'attribut, atomique pour tout lecteur — aucune vue
    mixte possible par construction. Validation intégrale avant tout
    remplacement : un YAML invalide est rejeté avec erreur explicite
    journalisée et l'ancienne config reste active. `broker.host/port`
    modifiés -> rejet explicite « redémarrage requis ». `changed` est armé
    après chaque swap réussi (consommé par la source MQTT pour
    resynchroniser ses souscriptions). `path=None` : configuration figée
    sans fichier, aucun rechargement (tests, assemblages en mémoire).
    """

    def __init__(self, config: Config, path: str | None = None) -> None:
        self._config = config
        self._path = path
        self._signature: tuple[int, int] | None = None
        self._absent_logged = False
        if path is not None:
            # Un fichier qui disparaît entre le chargement initial et la
            # construction du store : ConfigError explicite (échec rapide
            # au démarrage), jamais d'OSError brut hors de main().
            try:
                self._signature = _stat(path)
            except FileNotFoundError as exc:
                raise ConfigError(f"Fichier de configuration introuvable : {path}") from exc
            except OSError as exc:
                raise ConfigError(f"Configuration illisible : {path} : {exc}") from exc
        self._changed = asyncio.Event()

    def get(self) -> Config:
        """Snapshot figé de la configuration courante (jamais de vue mixte)."""
        return self._config

    @property
    def changed(self) -> asyncio.Event:
        """Armé après chaque swap réussi ; consommé (clear) par la source MQTT."""
        return self._changed

    async def watch(self, stop: asyncio.Event) -> None:
        """Sonde le fichier à intervalle POLL_INTERVAL_S, en tâche asyncio
        (aucun watcher externe) ; retourne quand `stop` est déclenché."""
        if self._path is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
                return
            except TimeoutError:
                pass
            await self.poll()

    async def poll(self) -> None:
        """Un sondage : recharge le YAML si sa signature (mtime, taille) a
        changé depuis le dernier rechargement (réussi ou rejeté).

        Lecture du fichier hors boucle (to_thread) ; validation intégrale
        avant tout swap ; une ligne de journal par rechargement, réussi ou
        rejeté — toute erreur de chargement (YAML cassé compris) est
        journalisée et l'ancienne config reste active : le watcher ne peut
        jamais mourir d'un mauvais fichier. La signature est enregistrée dès
        la tentative : un fichier cassé n'est pas re-testé au sondage
        suivant tant qu'il ne change plus. Après un swap, le fichier est
        re-staté : un remplacement pendant le chargement ne fige pas une
        base périmée — re-sondage immédiat.
        """
        if self._path is None:
            return
        try:
            signature = await asyncio.to_thread(_stat, self._path)
        except OSError:
            # Fichier absent (édition atomique ou suppression définitive) :
            # journalisé une seule fois par transition, rechargements
            # inopérants jusqu'au retour du fichier.
            if not self._absent_logged:
                log_line(
                    f"config file disappeared: {self._path} — rechargements "
                    "à chaud inopérants jusqu'à son retour"
                )
                self._absent_logged = True
            return
        self._absent_logged = False
        if signature == self._signature:
            return
        try:
            new_config = await asyncio.to_thread(load_config, self._path)
        except Exception as exc:  # noqa: BLE001 — un fichier cassé ne tue jamais le watcher
            detail = str(exc) if isinstance(exc, ConfigError) else f"{type(exc).__name__}: {exc}"
            log_line(f"config reload rejected: {detail}")
            self._signature = signature
            return
        current = self._config
        if (new_config.broker_host, new_config.broker_port) != (
            current.broker_host,
            current.broker_port,
        ):
            self._signature = signature
            log_line(
                "config reload rejected: broker.host/broker.port modifiés "
                "(redémarrage requis), ancienne configuration conservée — "
                "les changements suivants du fichier sont ignorés tant que "
                "host/port ne sont pas rétablis ou que le démon n'est pas "
                "redémarré"
            )
            return
        self._config = new_config
        self._changed.set()
        self._signature = signature
        topics = ",".join(subscription_topics(new_config.rules))
        log_line(
            f"config reloaded rules={len(new_config.rules)} topics={topics}"
        )
        try:
            post = await asyncio.to_thread(_stat, self._path)
        except OSError:
            post = None
        if post is not None and post != signature:
            # Remplacement pendant le chargement : la signature enregistrée
            # serait périmée — re-sondage immédiat.
            await self.poll()


def load_store_from_env() -> ConfigStore:
    """Construit le ConfigStore depuis MQTT_NOTIF_CONFIG ; échec rapide au démarrage."""
    path = config_path_from_env()
    return ConfigStore(load_config(path), path)
