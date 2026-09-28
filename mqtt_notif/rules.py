"""Sélection de règle première-gagnante et correspondance de motifs MQTT.

Une règle (nom unique, topic exact ou motif '+'/'#') est figée dans
l'événement au moment de la sélection. L'évaluation première-gagnante
porte sur la liste complète des règles ; la couverture minimale des
souscriptions garantit qu'un message chevauchant plusieurs filtres
n'est livré qu'une fois par le broker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LEVEL_PLUS = "+"
LEVEL_HASH = "#"


@dataclass(frozen=True)
class Rule:
    """Règle de notification : nom unique et topic exact ou motif '+'/'#'."""

    name: str
    topic: str


_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Convertit un filtre MQTT en expression régulière ancrée par niveaux.

    '+' correspond à un niveau entier ('[^/+]+') ; '#' au dernier niveau
    correspond à zéro, un ou plusieurs niveaux restants ('(?:/.*)?'),
    y compris le niveau parent immédiat, conformément à MQTT.
    """
    segments = pattern.split("/")
    if segments == [LEVEL_HASH]:
        return re.compile(r".*")
    parts: list[str] = []
    for segment in segments[:-1]:
        parts.append(r"[^/+]+" if segment == LEVEL_PLUS else re.escape(segment))
    last = segments[-1]
    if last == LEVEL_HASH:
        return re.compile("/".join(parts) + r"(?:/.*)?")
    parts.append(r"[^/+]+" if last == LEVEL_PLUS else re.escape(last))
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


def _covers(covering: str, covered: str) -> bool:
    """Vrai si le filtre `covering` couvre tous les topics du filtre `covered`.

    Décidable niveau par niveau : '#' couvre tout le reste (y compris zéro
    niveau restant), '+' un niveau exactement, l'exact doit être égal.
    """
    covering_levels = covering.split("/")
    covered_levels = covered.split("/")
    index = 0
    while True:
        if index >= len(covering_levels):
            return index >= len(covered_levels)
        level = covering_levels[index]
        if level == LEVEL_HASH:
            return True
        if index >= len(covered_levels):
            return False
        if covered_levels[index] == LEVEL_HASH:
            # '#' en fin de filtre couvre aussi le niveau parent :
            # seul un '#' au même rang peut le couvrir.
            return level == LEVEL_HASH
        if level == LEVEL_PLUS or level == covered_levels[index]:
            index += 1
            continue
        return False


def minimal_subscription_topics(rules: tuple[Rule, ...]) -> tuple[str, ...]:
    """Ensemble de souscriptions réduit à une couverture minimale.

    Un filtre couvert par un autre n'est pas abonné (le broker livrerait
    le message deux fois) ; les doublons exacts ne sont abonnés qu'une
    fois. L'ordre de déclaration est préservé ; l'évaluation des règles
    reste, elle, sur la liste complète.
    """
    filters = list(dict.fromkeys(rule.topic for rule in rules))
    return tuple(
        pattern
        for position, pattern in enumerate(filters)
        if not any(
            _covers(other, pattern)
            for other in filters[:position] + filters[position + 1 :]
        )
    )
