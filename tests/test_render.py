"""AC-1.3.1 à AC-1.3.7 : moteur de rendu — matrice I/O complète,
substitution json/raw, '{{' littéral, '[absent]', jamais d'exception."""

from __future__ import annotations

import pytest

from mqtt_notif.render import (
    MISSING,
    TemplateError,
    render_message,
    render_template,
    validate_template,
)
from mqtt_notif.rules import PAYLOAD_FORMAT_JSON, PAYLOAD_FORMAT_RAW, Rule

JSON_RULE = Rule(
    name="salon",
    topic="home/salon/capteur",
    title="Capteur {capteur.lieu}",
    short_message="Valeur: {valeur}",
    detailed_message="Détail {absent} et {valeur}",
)
RAW_RULE = Rule(
    name="brut",
    topic="home/brut",
    payload_format=PAYLOAD_FORMAT_RAW,
    title="Alerte",
    short_message="{payload}",
)


@pytest.mark.parametrize(
    "template, payload, expected",
    [
        # Substitution simple (mode json)
        ("Bouton appuye ({action})", {"action": "simple"}, "Bouton appuye (simple)"),
        # Chemin pointé imbriqué
        ("{capteur.lieu}", {"capteur": {"lieu": "salon"}}, "salon"),
        # Chemin inexistant -> [absent]
        ("{capteur.absent}", {"capteur": {"lieu": "salon"}}, MISSING),
        ("{a.b.c}", {"a": {"b": {}}}, MISSING),
        # Champ absent -> [absent]
        ("Valeur: {valeur}", {}, "Valeur: " + MISSING),
        # Chemin traversant un non-dict -> [absent]
        ("{a.b}", {"a": "texte"}, MISSING),
        ("{a.b}", {"a": 3}, MISSING),
        # '{{' -> '{' littéral, pas de substitution ; '}' isolée reste littérale
        # (seul '{{' est un échappement : '{{action}}' rend '{action}}')
        ("{{action}}", {"action": "x"}, "{action}}"),
        ("{{action}", {"action": "x"}, "{action}"),
        ("{{ brut", {}, "{ brut"),
        # Accolade fermante isolée : littérale
        ("a } b", {}, "a } b"),
        # Valeurs non-chaîne : scalaires, None, booléens, JSON compact
        ("n={n}", {"n": 42}, "n=42"),
        ("f={f}", {"f": 1.5}, "f=1.5"),
        ("x={x}", {"x": None}, "x=null"),
        ("v={v}", {"v": True}, "v=true"),
        ("v={v}", {"v": False}, "v=false"),
        ("d={d}", {"d": {"a": 1, "b": "é"}}, 'd={"a":1,"b":"é"}'),
        ("l={l}", {"l": [1, 2]}, "l=[1,2]"),
        # dict imbriqué au bout d'un chemin
        ("{a.b}", {"a": {"b": {"c": 1}}}, '{"c":1}'),
        # Plusieurs placeholders et texte mélangé
        ("{a} et {b.c}", {"a": "x", "b": {"c": "y"}}, "x et y"),
    ],
)
def test_render_template_matrix(template, payload, expected):
    assert render_template(template, payload) == expected


def test_render_template_raw_payload():
    """Mode raw : '{payload}' substitue le payload brut décodé UTF-8."""
    assert render_template("{payload}", "texte brut") == "texte brut"


@pytest.mark.parametrize("payload", [{}, {"payload": "piege"}])
def test_render_json_mode_payload_is_just_a_field(payload):
    """En mode json, 'payload' est un champ comme un autre (pas de substitution brute)."""
    expected = payload.get("payload", MISSING)
    assert render_template("{payload}", payload) == expected


def test_render_message_without_detailed_message():
    """Sans detailed_message dans la règle : seules title et short_message rendues."""
    rule = Rule(name="r", topic="home/x", title="Titre {a}", short_message="Message {a}")
    assert render_message(rule, {"a": "v"}) == {"title": "Titre v", "short_message": "Message v"}


def test_render_message_with_detailed_message():
    """AC-1.3.8 : detailed_message présent -> clé ajoutée au rendu complet."""
    message = render_message(JSON_RULE, {"valeur": "12", "capteur": {"lieu": "salon"}})
    assert message == {
        "title": "Capteur salon",
        "short_message": "Valeur: 12",
        "detailed_message": f"Détail {MISSING} et 12",
    }


def test_render_message_raw_rule():
    message = render_message(RAW_RULE, "porte ouverte")
    assert message == {"title": "Alerte", "short_message": "porte ouverte"}


def test_render_message_never_raises_on_any_payload():
    """Propriété : quel que soit le payload (dict figé à la sélection), le rendu
    est total — champ absent -> [absent], jamais d'exception."""
    for payload in ({}, {"a": {"b": "c"}}, {"capteur": 3}):
        message = render_message(JSON_RULE, payload)
        assert set(message) == {"title", "short_message", "detailed_message"}
        assert MISSING in message["title"] or payload == {"a": {"b": "c"}}


@pytest.mark.parametrize(
    "template",
    ["{ champ}", "{champ }", "{cha mp}", "{\tchamp}"],
)
def test_validate_template_rejects_placeholder_with_space(template):
    with pytest.raises(TemplateError, match="placeholder avec espace interdit"):
        validate_template(template, PAYLOAD_FORMAT_JSON, "salon", "title")


@pytest.mark.parametrize(
    "template, reason",
    [
        ("Alerte {champ", "accolade non fermée"),
        ("{", "accolade non fermée"),
        ("Alerte {}", "placeholder vide"),
        ("Alerte {ch{amp}", "'{' interdit dans un placeholder"),
        ("{ champ}", "placeholder avec espace interdit"),
    ],
)
def test_validate_template_malformed(template, reason):
    with pytest.raises(TemplateError) as excinfo:
        validate_template(template, PAYLOAD_FORMAT_JSON, "salon", "title")
    message = str(excinfo.value)
    assert reason in message
    assert "Règle 'salon'" in message
    assert "champ 'title'" in message
    assert template in message


def test_validate_template_raw_mode_rejects_other_placeholders():
    with pytest.raises(TemplateError, match="mode raw") as excinfo:
        validate_template("{champ}", PAYLOAD_FORMAT_RAW, "brut", "short_message")
    message = str(excinfo.value)
    assert "Règle 'brut'" in message
    assert "champ 'short_message'" in message


@pytest.mark.parametrize(
    "template",
    ["{payload}", "Alerte {payload}", "{{x}} {payload}", "sans placeholder"],
)
def test_validate_template_raw_mode_accepts_payload_only(template):
    validate_template(template, PAYLOAD_FORMAT_RAW, "brut", "short_message")


@pytest.mark.parametrize(
    "template",
    ["{champ}", "{a.b}", "{payload}", "{{x}} {a}"],
)
def test_validate_template_json_mode_accepts_any_field(template):
    """En mode json, tout placeholder de champ ou chemin pointé est légal."""
    validate_template(template, PAYLOAD_FORMAT_JSON, "salon", "title")


def test_validate_template_json_mode_has_no_raw_restriction_error():
    """La restriction raw ne s'applique pas en mode json (message d'erreur distinct)."""
    with pytest.raises(TemplateError) as excinfo:
        validate_template("Alerte {champ", PAYLOAD_FORMAT_JSON, "salon", "title")
    assert "mode raw" not in str(excinfo.value)
