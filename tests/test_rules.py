"""AC-1.2.2 / AC-1.2.4 : motifs '+'/'#', première-gagnante, souscriptions
deux à deux disjointes (fusion en plus petit sur-ensemble MQTT)."""

from __future__ import annotations

import dataclasses

import pytest

from mqtt_notif.rules import (
    Rule,
    find_rule,
    subscription_topics,
    topic_matches,
)


@pytest.mark.parametrize(
    "pattern, topic, expected",
    [
        # Topic exact
        ("home/salon/capteur", "home/salon/capteur", True),
        ("home/salon/capteur", "home/salon/temp", False),
        ("home/salon/capteur", "home/salon/capteur/x", False),
        # '+' : un niveau entier, vide accepté (sémantique MQTT 4.7.1)
        ("home/+/capteur", "home/salon/capteur", True),
        ("home/+/capteur", "home//capteur", True),
        ("home/+/capteur", "home/salon/bureau/capteur", False),
        ("home/+", "home/salon", True),
        ("home/+", "home/salon/capteur", False),
        ("+", "un-seul-niveau", True),
        ("+", "a/b", False),
        # '#' : sous-hiérarchie entière, y compris le niveau parent
        ("home/#", "home", True),
        ("home/#", "home/salon/capteur", True),
        ("home/#", "homex", False),
        ("#", "nimporte/quoi", True),
        # Sensible à la casse
        ("home/+/capteur", "home/salon/CAPTEUR", False),
        # Topic exact avec métacaractères regex (chemin re.escape : '.', '*')
        ("home/a.b/x", "home/a.b/x", True),
        ("home/a.b/x", "home/aXb/x", False),
        ("home/a.b/x", "home/a.b", False),
        ("home/a*b/x", "home/a*b/x", True),
        ("home/a*b/x", "home/aXb/x", False),
        ("home/+/c.d", "home/salon/c.d", True),
        ("home/+/c.d", "home/salon/cXd", False),
    ],
)
def test_topic_matches(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected


def test_rule_is_frozen():
    """La règle est immuable : toute mutation lève (figée dans l'événement)."""
    rule = Rule(name="salon", topic="home/#", title="titre", short_message="message")
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.name = "autre"


def test_find_rule_first_wins_and_counts():
    """AC-1.2.3 : première règle déclarée gagnante, toutes les correspondances comptées."""
    rules = (
        Rule(name="a", topic="home/+/capteur", title="titre", short_message="message"),
        Rule(name="b", topic="home/salon/+", title="titre", short_message="message"),
        Rule(name="c", topic="home/#", title="titre", short_message="message"),
    )
    winner, matches = find_rule(rules, "home/salon/capteur")
    assert winner == rules[0]
    assert matches == 3


def test_find_rule_single_match():
    rules = (Rule(name="a", topic="home/+/capteur", title="titre", short_message="message"), Rule(name="b", topic="garage/#", title="titre", short_message="message"))
    winner, matches = find_rule(rules, "home/bureau/capteur")
    assert winner == rules[0]
    assert matches == 1


def test_find_rule_no_match():
    rules = (Rule(name="a", topic="home/+/capteur", title="titre", short_message="message"), Rule(name="b", topic="garage/#", title="titre", short_message="message"))
    winner, matches = find_rule(rules, "home/salon/temp")
    assert winner is None
    assert matches == 0


def test_subscription_exact_duplicates_subscribed_once():
    rules = (Rule(name="a", topic="home/x", title="titre", short_message="message"), Rule(name="b", topic="home/x", title="titre", short_message="message"))
    assert subscription_topics(rules) == ("home/x",)


def test_subscription_disjoint_filters_kept():
    rules = (
        Rule(name="capteur", topic="home/salon/capteur", title="titre", short_message="message"),
        Rule(name="garage", topic="garage/#", title="titre", short_message="message"),
    )
    assert subscription_topics(rules) == ("garage/#", "home/salon/capteur")


def test_subscription_containment_keeps_superset():
    rules = (Rule(name="maison", topic="home/#", title="titre", short_message="message"), Rule(name="salon", topic="home/salon/temp", title="titre", short_message="message"))
    assert subscription_topics(rules) == ("home/#",)


def test_subscription_partial_overlap_merged_to_least_upper_bound():
    """Cas du finding high : chevauchement partiel -> fusion, pas deux souscriptions.

    'home/+/capteur' et 'home/salon/+' s'intersectent (ex. 'home/salon/capteur') :
    abonner les deux ferait livrer le message deux fois. Fusion en leur plus
    petit sur-ensemble MQTT : 'home/+/+'.
    """
    rules = (
        Rule(name="capteur", topic="home/+/capteur", title="titre", short_message="message"),
        Rule(name="salon", topic="home/salon/+", title="titre", short_message="message"),
    )
    assert subscription_topics(rules) == ("home/+/+",)


def test_subscription_merge_iterated_to_fixed_point():
    """La fusion est réitérée : le résultat de la fusion peut chevaucher un tiers."""
    rules = (
        Rule(name="capteur", topic="home/+/capteur", title="titre", short_message="message"),
        Rule(name="salon", topic="home/salon/+", title="titre", short_message="message"),
        Rule(name="bureau", topic="home/bureau/capteur", title="titre", short_message="message"),
    )
    assert subscription_topics(rules) == ("home/+/+",)


def test_subscription_hash_exhaustion_merge():
    rules = (
        Rule(name="salon", topic="home/salon/#", title="titre", short_message="message"),
        Rule(name="profonde", topic="home/salon/temp/x", title="titre", short_message="message"),
    )
    assert subscription_topics(rules) == ("home/salon/#",)


def test_subscription_two_hash_filters_merge_on_common_prefix():
    rules = (Rule(name="a", topic="home/+/x/#", title="titre", short_message="message"), Rule(name="b", topic="home/salon/#", title="titre", short_message="message"))
    assert subscription_topics(rules) == ("home/+/#",)


def test_subscriptions_cover_union_and_are_pairwise_disjoint():
    """Propriété : chaque topic de règle est couvert, aucun topic par deux souscriptions."""
    rules = (
        Rule(name="a", topic="home/+/capteur", title="titre", short_message="message"),
        Rule(name="b", topic="home/salon/+", title="titre", short_message="message"),
        Rule(name="c", topic="garage/#", title="titre", short_message="message"),
        Rule(name="d", topic="home/#", title="titre", short_message="message"),
    )
    topics = subscription_topics(rules)
    probes = [
        "home/salon/capteur",  # couvert par a, b et d
        "home/bureau/capteur",  # couvert par a et d
        "home/salon/temp",  # couvert par b et d
        "garage/portail/etat",  # couvert par c
        "home",  # couvert par d
    ]
    for probe in probes:
        assert sum(topic_matches(pattern, probe) for pattern in topics) == 1
