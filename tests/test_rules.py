"""AC-1.2.4 : motifs '+'/'#', sélection première-gagnante, couverture minimale."""

from __future__ import annotations

import dataclasses

import pytest

from mqtt_notif.rules import (
    Rule,
    find_rule,
    minimal_subscription_topics,
    topic_matches,
)


@pytest.mark.parametrize(
    "pattern, topic, expected",
    [
        # Topic exact
        ("home/salon/capteur", "home/salon/capteur", True),
        ("home/salon/capteur", "home/salon/lampe", False),
        ("home/salon/capteur", "home/salon/capteur/x", False),
        # '+' : un niveau entier exactement
        ("home/+/bouton", "home/salon/bouton", True),
        ("home/+/bouton", "home/cave/bouton", True),
        ("home/+/bouton", "home/bouton", False),
        ("home/+/bouton", "home/salon/cave/bouton", False),
        ("home/+/+", "home/salon/capteur", True),
        ("+", "home", True),
        ("+", "home/salon", False),
        # '#' : dernier niveau, zéro, un ou plusieurs niveaux restants
        ("home/#", "home/salon/capteur", True),
        ("home/#", "home", True),
        ("home/#", "home/salon", True),
        ("home/#", "maison/salon", False),
        ("#", "nimporte/quoi", True),
        # Un niveau exact ne matche pas partiellement
        ("home/salon+", "home/salonx", False),
        ("home/sal#on", "home/salon", False),
    ],
)
def test_topic_matches(pattern, topic, expected):
    """Correspondance exacte, '+' un niveau, '#' fin de hiérarchie (y compris parent)."""
    assert topic_matches(pattern, topic) is expected


def test_find_rule_first_wins_with_match_count():
    """Première règle déclarée qui correspond gagne ; le compte liste toutes."""
    rules = (
        Rule(name="premiere", topic="home/#"),
        Rule(name="deuxieme", topic="home/+/capteur"),
        Rule(name="hors-jeu", topic="garage/#"),
    )
    rule, matches = find_rule(rules, "home/salon/capteur")
    assert rule is not None
    assert rule.name == "premiere"
    assert matches == 2


def test_find_rule_exact_beats_later_wildcard():
    rules = (
        Rule(name="exact", topic="home/salon/capteur"),
        Rule(name="motif", topic="home/#"),
    )
    rule, matches = find_rule(rules, "home/salon/capteur")
    assert rule is not None
    assert rule.name == "exact"
    assert matches == 2


def test_find_rule_no_match_returns_none():
    rules = (Rule(name="a", topic="home/#"),)
    rule, matches = find_rule(rules, "garage/porte")
    assert rule is None
    assert matches == 0


def test_rule_is_frozen():
    """La règle figée dans l'événement ne peut pas être mutée en traversée."""
    rule = Rule(name="a", topic="home/#")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.topic = "autre/chose"


def test_minimal_subscription_drops_covered_filters():
    """Un filtre couvert par un autre n'est pas abonné ; l'ordre est préservé."""
    rules = (
        Rule(name="exact", topic="home/salon/capteur"),
        Rule(name="motif-salon", topic="home/+/capteur"),
        Rule(name="toute-la-maison", topic="home/#"),
        Rule(name="garage", topic="garage/porte"),
    )
    assert minimal_subscription_topics(rules) == ("home/#", "garage/porte")


def test_minimal_subscription_dedups_identical_filters():
    rules = (
        Rule(name="a", topic="home/salon"),
        Rule(name="b", topic="home/salon"),
    )
    assert minimal_subscription_topics(rules) == ("home/salon",)


def test_minimal_subscription_hash_covers_parent_wildcards():
    """'home/#' couvre 'home/+' (le '+' attend un niveau, '#' zéro ou plus)."""
    rules = (
        Rule(name="niveau", topic="home/+"),
        Rule(name="reste", topic="home/#"),
    )
    assert minimal_subscription_topics(rules) == ("home/#",)


def test_minimal_subscription_keeps_independent_filters():
    """Deux filtres qui se chevauchent partiellement restent tous deux abonnés."""
    rules = (
        Rule(name="salon", topic="home/salon/#"),
        Rule(name="cave", topic="home/cave/#"),
    )
    assert minimal_subscription_topics(rules) == ("home/salon/#", "home/cave/#")
