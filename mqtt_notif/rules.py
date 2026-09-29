"""Sélection de règle première-gagnante et correspondance de motifs MQTT.

Une règle (nom unique, topic exact ou motif '+'/'#') est figée dans
l'événement au moment de la sélection. L'évaluation première-gagnante
porte sur la liste complète des règles ; les souscriptions retenues
sont deux à deux disjointes — un broker MQTT peut livrer un message
une fois par souscription correspondante (MQTT 3.1.1 § 3.3.5 autorise
aussi une copie unique), des filtres chevauchants entraîneraient
des notifications en double.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LEVEL_PLUS = "+"
LEVEL_HASH = "#"
PAYLOAD_FORMAT_JSON = "json"
PAYLOAD_FORMAT_RAW = "raw"


@dataclass(frozen=True)
class Rule:
    """Règle de notification : nom unique, topic exact ou motif '+'/'#',
    gabarits de message et format du payload (json par défaut, raw)."""

    name: str
    topic: str
    title: str
    short_message: str
    payload_format: str = PAYLOAD_FORMAT_JSON
    detailed_message: str | None = None


_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Convertit un filtre MQTT en expression régulière ancrée par niveaux.

    '+' correspond à un niveau entier, vide accepté ('[^/+]*', sémantique
    MQTT 4.7.1 — un broker livre 'home//x' à un filtre 'home/+/x') ;
    '#' en dernier niveau correspond à zéro, un ou plusieurs niveaux
    restants ('(?:/.*)?'), y compris le niveau parent immédiat.
    """
    segments = pattern.split("/")
    if segments == [LEVEL_HASH]:
        return re.compile(r".*")
    parts: list[str] = []
    for segment in segments[:-1]:
        parts.append(r"[^/+]*" if segment == LEVEL_PLUS else re.escape(segment))
    last = segments[-1]
    if last == LEVEL_HASH:
        return re.compile("/".join(parts) + r"(?:/.*)?")
    parts.append(r"[^/+]*" if last == LEVEL_PLUS else re.escape(last))
    return re.compile("/".join(parts))


def topic_matches(pattern: str, topic: str) -> bool:
    """Vrai si `topic` est couvert par le filtre `pattern` (exact, '+', '#')."""
    regex = _REGEX_CACHE.get(pattern)
    if regex is None:
        regex = _compile_pattern(pattern)
        _REGEX_CACHE[pattern] = regex
    return regex.fullmatch(topic) is not None


def find_rule(rules: tuple[Rule, ...], topic: str) -> tuple[Rule | None, int]:
    """Première-gagnante : première règle correspondante + nombre total de correspondances.

    Renvoie (None, 0) quand aucune règle ne correspond ; les correspondances
    suivantes ne sont comptées que pour l'avertissement multi-correspondances.
    """
    winner: Rule | None = None
    matches = 0
    for rule in rules:
        if topic_matches(rule.topic, topic):
            matches += 1
            if winner is None:
                winner = rule
    return winner, matches


def _levels_match(left: str, right: str) -> bool:
    """Un niveau concret unique peut-il satisfaire les deux niveaux de filtre ?"""
    return left == LEVEL_PLUS or right == LEVEL_PLUS or left == right


def _filters_intersect(first: str, second: str) -> bool:
    """Vrai si au moins un topic concret correspond aux deux filtres."""
    first_levels = first.split("/")
    second_levels = second.split("/")
    first_hash = first_levels[-1] == LEVEL_HASH
    second_hash = second_levels[-1] == LEVEL_HASH
    if first_hash and second_hash:
        # Deux sous-arbres '#' s'intersectent dès que leurs préfixes sont
        # compatibles (l'un des deux préfixes recouvre l'autre).
        return _prefixes_intersect(first_levels[:-1], second_levels[:-1])
    if first_hash:
        return _hash_intersects_levels(first_levels[:-1], second_levels)
    if second_hash:
        return _hash_intersects_levels(second_levels[:-1], first_levels)
    if len(first_levels) != len(second_levels):
        return False
    return all(_levels_match(x, y) for x, y in zip(first_levels, second_levels))


def _prefixes_intersect(first: list[str], second: list[str]) -> bool:
    """Préfixes compatibles : chaque paire de niveaux doit être satisfaisable."""
    common = min(len(first), len(second))
    return all(_levels_match(x, y) for x, y in zip(first[:common], second[:common]))


def _hash_intersects_levels(prefix: list[str], levels: list[str]) -> bool:
    """Sous-arbre 'prefix/#' contre filtre sans '#' : intersection par préfixe.

    Le filtre couvre le parent lui-même (longueurs égales) ou le parent
    suivi des niveaux restants du filtre, chacun satisfaisable seul.
    """
    if len(levels) < len(prefix):
        return False
    return all(_levels_match(x, y) for x, y in zip(prefix, levels[: len(prefix)]))


def _least_upper_bound(first: str, second: str) -> str:
    """Plus petit sur-ensemble MQTT de deux filtres chevauchants.

    Position par position : exacts égaux -> exact, sinon '+' ;
    épuisement d'un filtre ou '#' rencontré -> '#' en fin.
    """
    first_levels = first.split("/")
    second_levels = second.split("/")
    merged: list[str] = []
    for index in range(max(len(first_levels), len(second_levels))):
        if index >= len(first_levels) or index >= len(second_levels):
            merged.append(LEVEL_HASH)
            break
        first_level = first_levels[index]
        second_level = second_levels[index]
        if first_level == LEVEL_HASH or second_level == LEVEL_HASH:
            merged.append(LEVEL_HASH)
            break
        merged.append(first_level if first_level == second_level else LEVEL_PLUS)
    return "/".join(merged)


def subscription_topics(rules: tuple[Rule, ...]) -> tuple[str, ...]:
    """Souscriptions deux à deux disjointes couvrant l'union des topics des règles.

    Un broker MQTT peut livrer un message une fois par souscription
    correspondante : des filtres chevauchants (partiellement ou par
    contenance) provoqueraient des doublons. Deux filtres dont les langages
    s'intersectent sont fusionnés
    en leur plus petit sur-ensemble MQTT, itéré jusqu'au point fixe ; les
    doublons exacts sont abonnés une seule fois. Les topics supplémentaires
    couverts par un sur-ensemble mais hors règles sont ignorés par la
    sélection première-gagnante, qui reste sur la liste complète des règles.
    """
    filters = list(dict.fromkeys(rule.topic for rule in rules))
    merging = True
    while merging:
        merging = False
        for first in range(len(filters)):
            for second in range(first + 1, len(filters)):
                if _filters_intersect(filters[first], filters[second]):
                    merged = _least_upper_bound(filters[first], filters[second])
                    remaining = [
                        pattern
                        for position, pattern in enumerate(filters)
                        if position not in (first, second)
                    ]
                    filters = list(dict.fromkeys(remaining + [merged]))
                    merging = True
                    break
            if merging:
                break
    return tuple(sorted(filters))
