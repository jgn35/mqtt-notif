"""AC-1.2.1 / AC-1.3.5 / AC-1.3.7 : schéma `rules`, validation stricte citant
la règle fautive (topics, gabarits, payload_format), échec rapide au démarrage."""

from __future__ import annotations

import re
import textwrap

import pytest

from mqtt_notif.config import ConfigError, config_path_from_env, load_config, load_config_from_env
from mqtt_notif.rules import Rule

VALID_YAML = textwrap.dedent(
    """\
    broker:
      host: localhost
      port: 1883
    device_token: abc123
    rules:
      - name: salon
        topic: home/salon/capteur
        title: Capteur salon
        short_message: 'Valeur: {valeur}'
      - name: maison
        topic: 'home/#'
        title: Maison
        short_message: '{event} détecté'
    """
)

BROKER_YAML = "broker:\n  host: h\n  port: 1883\ndevice_token: t\n"


def write_yaml(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def rule_yaml(
    topic,
    name="salon",
    payload_format=None,
    title="Titre salon",
    short_message="Message {champ}",
    detailed_message=None,
    extra="",
):
    r"""Section rules minimale avec une seule règle (gabarits valides par défaut).

    Le topic est écrit en style double-quoted : PyYAML y décode les escapes
    ("\x00", "\u200b") en caractères réels. Passer en style plain rendrait
    ces tests trivialement verts sans couvrir la validation Cc/Cf.
    Chaque champ est émis une seule fois (jamais de clé dupliquée, dont
    PyYAML ne garantirait pas l'ordre) ; `extra` ajoute des lignes YAML
    supplémentaires (ex. payload_format: null).
    """
    lines = f'rules:\n  - name: {name}\n    topic: "{topic}"\n'
    if payload_format is not None:
        lines += f"    payload_format: {payload_format}\n"
    lines += f"    title: '{title}'\n    short_message: '{short_message}'\n"
    if detailed_message is not None:
        lines += f"    detailed_message: '{detailed_message}'\n"
    return lines + extra


def test_valid_yaml_loads(tmp_path):
    config = load_config(str(write_yaml(tmp_path, VALID_YAML)))
    assert config.broker_host == "localhost"
    assert config.broker_port == 1883
    assert config.device_token == "abc123"
    assert config.rules == (
        Rule(
            name="salon",
            topic="home/salon/capteur",
            title="Capteur salon",
            short_message="Valeur: {valeur}",
        ),
        Rule(name="maison", topic="home/#", title="Maison", short_message="{event} détecté"),
    )


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(str(tmp_path / "absent.yaml"))


def test_path_is_a_directory(tmp_path):
    """Un chemin répertoire est une erreur explicite, pas un traceback brut."""
    with pytest.raises(ConfigError, match="illisible"):
        load_config(str(tmp_path))


@pytest.mark.parametrize(
    "topic",
    [
        "home/+/capteur",
        "home/#",
        "#",
        "+",
        "home/+/+",
        "home//capteur",  # niveau vide légal (MQTT 4.7.1, aligné sur '+' acceptant le vide)
    ],
)
def test_wildcard_patterns_are_legal(tmp_path, topic):
    """Les motifs '+'/'#' sont désormais légaux (la validation 1.1 disparaît)."""
    config = load_config(str(write_yaml(tmp_path, BROKER_YAML + rule_yaml(topic))))
    assert config.rules[0].topic == topic


@pytest.mark.parametrize(
    "topic, expected",
    [
        ("home/#/x", "'#' doit occuper seul le dernier niveau"),
        ("home/#x", "'#'"),
        ("home/ho+me", "'+' doit occuper un niveau entier"),
        ("home/salon capteur", "espaces interdits"),
        ("home/\\x00salon", "caractères de contrôle ou de format interdits"),
        ("home/salon\\x7f", "caractères de contrôle ou de format interdits"),
        ("home/\\u200bsalon", "caractères de contrôle ou de format interdits"),
        ("home/\\ufeff", "caractères de contrôle ou de format interdits"),
    ],
)
def test_invalid_topic_syntax_rejected(tmp_path, topic, expected):
    """Motif illégal : rejet au démarrage, erreur citant la règle fautive."""
    content = BROKER_YAML + rule_yaml(topic)
    with pytest.raises(ConfigError, match=re.escape(expected)) as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


def test_rule_missing_topic_key(tmp_path):
    content = BROKER_YAML + "rules:\n  - name: salon\n"
    with pytest.raises(ConfigError, match="'topic'") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


def test_rule_empty_topic(tmp_path):
    content = BROKER_YAML + rule_yaml("  ")
    with pytest.raises(ConfigError, match="'topic'") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


def test_rule_missing_or_empty_name(tmp_path):
    content = BROKER_YAML + "rules:\n  - topic: home/x\n"
    with pytest.raises(ConfigError, match="'name'"):
        load_config(str(write_yaml(tmp_path, content)))
    content = BROKER_YAML + "rules:\n  - name: '  '\n    topic: home/x\n"
    with pytest.raises(ConfigError, match="'name'"):
        load_config(str(write_yaml(tmp_path, content)))


def test_rule_name_not_a_string(tmp_path):
    content = BROKER_YAML + "rules:\n  - name: 42\n    topic: home/x\n"
    with pytest.raises(ConfigError, match="'name'"):
        load_config(str(write_yaml(tmp_path, content)))


def test_rule_unknown_key_rejected(tmp_path):
    """Une clé inconnue dans une entrée de règle (typo) ne passe pas en silence."""
    content = BROKER_YAML + "rules:\n  - name: salon\n    topick: home/x\n"
    with pytest.raises(ConfigError, match="inconnue") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "topick" in str(excinfo.value)
    assert "salon" in str(excinfo.value)


def test_rule_unknown_keys_mixed_types(tmp_path):
    """Clés inconnues de types mixtes (YAML '42:' + 'topick:') : ConfigError, pas de TypeError."""
    content = BROKER_YAML + "rules:\n  - name: salon\n    42: x\n    topick: home/x\n"
    with pytest.raises(ConfigError, match="inconnue") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "42" in str(excinfo.value)
    assert "topick" in str(excinfo.value)


def test_root_unknown_key_rejected(tmp_path):
    """Les clés racine inconnues sont rejetées, comme celles des entrées de règle."""
    content = VALID_YAML + "\nextra: valeur\n"
    with pytest.raises(ConfigError, match="inconnue") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "extra" in str(excinfo.value)


def test_legacy_topic_root_key_migration_hint(tmp_path):
    """La clé 'topic' de la story 1.1 est rejetée avec un indice de migration vers 'rules'."""
    content = VALID_YAML + "\ntopic: home/salon/capteur\n"
    with pytest.raises(ConfigError, match="remplacé") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "'topic'" in str(excinfo.value)
    assert "rules" in str(excinfo.value)


def test_duplicate_rule_names_rejected(tmp_path):
    content = (
        BROKER_YAML
        + rule_yaml("home/x", name="salon")
        + "  - name: salon\n    topic: home/y\n    title: T\n    short_message: M\n"
    )
    with pytest.raises(ConfigError, match="dupliqué") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


def test_rule_entry_not_a_mapping(tmp_path):
    content = BROKER_YAML + "rules:\n  - juste_une_chaine\n"
    with pytest.raises(ConfigError, match="Règle #1"):
        load_config(str(write_yaml(tmp_path, content)))


def test_rules_missing_or_not_a_list(tmp_path):
    content = BROKER_YAML + "rules: pas_une_liste\n"
    with pytest.raises(ConfigError, match="'rules'"):
        load_config(str(write_yaml(tmp_path, content)))
    content = BROKER_YAML + "rules:\n  salon: home/x\n"
    with pytest.raises(ConfigError, match="'rules'"):
        load_config(str(write_yaml(tmp_path, content)))


def test_empty_rules_list_rejected(tmp_path):
    """Un démon sans règle ne ferait rien silencieusement : échec rapide."""
    content = BROKER_YAML + "rules: []\n"
    with pytest.raises(ConfigError, match="'rules'"):
        load_config(str(write_yaml(tmp_path, content)))


def test_values_are_stripped(tmp_path):
    """Les valeurs sont stockées débarrassées des espaces environnants."""
    content = textwrap.dedent(
        """\
        broker:
          host: '  localhost  '
          port: 1883
        device_token: '  tok  '
        rules:
          - name: '  salon  '
            topic: '  home/salon  '
            title: '  Titre  '
            short_message: '  Message {champ}  '
        """
    )
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.broker_host == "localhost"
    assert config.device_token == "tok"
    assert config.rules == (
        Rule(name="salon", topic="home/salon", title="Titre", short_message="Message {champ}"),
    )


def test_corrupt_yaml(tmp_path):
    with pytest.raises(ConfigError, match="YAML invalide"):
        load_config(str(write_yaml(tmp_path, "broker: [non, fermé\n  host: x")))


def test_non_mapping_root(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(str(write_yaml(tmp_path, "- juste\n- une liste\n")))


@pytest.mark.parametrize(
    "content, expected",
    [
        ("port: 1883\ndevice_token: t\nrules: []\n", "'broker'"),
        ("host: localhost\nport: 1883\ndevice_token: t\nrules: []\n", "'broker'"),
        ("broker:\n  port: 1883\ndevice_token: t\nrules: []\n", "broker.host"),
        ("broker:\n  host: localhost\ndevice_token: t\nrules: []\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: pas_un_entier\ndevice_token: t\nrules: []\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: true\ndevice_token: t\nrules: []\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: 65536\ndevice_token: t\nrules: []\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: 0\ndevice_token: t\nrules: []\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: 1883\nrules: []\n", "device_token"),
        ("broker:\n  host: localhost\n  port: 1883\ndevice_token: ''\nrules: []\n", "device_token"),
        ("broker:\n  host: localhost\n  port: 1883\ndevice_token: t\n", "'rules'"),
    ],
)
def test_missing_or_invalid_keys(tmp_path, content, expected):
    with pytest.raises(ConfigError, match=expected):
        load_config(str(write_yaml(tmp_path, content)))


def test_env_var_absent(monkeypatch):
    monkeypatch.delenv("MQTT_NOTIF_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="MQTT_NOTIF_CONFIG"):
        load_config_from_env()


def test_env_var_path_used(monkeypatch, tmp_path):
    path = write_yaml(tmp_path, VALID_YAML)
    monkeypatch.setenv("MQTT_NOTIF_CONFIG", str(path))
    config = load_config_from_env()
    assert config.broker_host == "localhost"
    assert config.rules[0].name == "salon"
    assert config_path_from_env() == str(path)


# --- Story 1.3 : payload_format et gabarits par règle ---


def test_rule_templates_and_default_payload_format(tmp_path):
    """Sans payload_format : json par défaut ; gabarits portés par la règle."""
    content = BROKER_YAML + rule_yaml("home/x", detailed_message="Détail {a.b}")
    config = load_config(str(write_yaml(tmp_path, content)))
    rule = config.rules[0]
    assert rule.payload_format == "json"
    assert rule.title == "Titre salon"
    assert rule.short_message == "Message {champ}"
    assert rule.detailed_message == "Détail {a.b}"


def test_rule_payload_format_raw_accepted(tmp_path):
    """Mode raw : seul '{payload}' est autorisé dans les gabarits."""
    content = BROKER_YAML + rule_yaml("home/x", payload_format="raw", title="Alerte", short_message="{payload}")
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.rules[0].payload_format == "raw"
    assert config.rules[0].short_message == "{payload}"


@pytest.mark.parametrize("payload_format", ["xml", "JSON", 3])
def test_rule_payload_format_invalid_rejected(tmp_path, payload_format):
    content = BROKER_YAML + rule_yaml("home/x", extra=f"    payload_format: {payload_format}\n")
    with pytest.raises(ConfigError, match="'payload_format'") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


@pytest.mark.parametrize("yaml_literal", ["null", "~", ""])
def test_rule_payload_format_yaml_null_rejected(tmp_path, yaml_literal):
    """Littéraux YAML de null (null, ~, valeur vide) : pas de payload_format."""
    content = BROKER_YAML + rule_yaml("home/x", extra=f"    payload_format: {yaml_literal}\n")
    with pytest.raises(ConfigError, match="'payload_format'") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


@pytest.mark.parametrize("field", ["title", "short_message"])
def test_rule_missing_template_rejected(tmp_path, field):
    """title et short_message sont obligatoires ; l'erreur cite règle et champ."""
    fields = {"title": "    title: T\n", "short_message": "    short_message: M\n"}
    del fields[field]
    content = BROKER_YAML + "rules:\n  - name: salon\n    topic: home/x\n" + "".join(
        fields.values()
    )
    with pytest.raises(ConfigError, match=f"'{field}'") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


@pytest.mark.parametrize("field", ["title", "short_message", "detailed_message"])
def test_rule_empty_template_rejected(tmp_path, field):
    content = BROKER_YAML + rule_yaml("home/x", **{field: ""})
    with pytest.raises(ConfigError, match=f"'{field}'") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


@pytest.mark.parametrize("value", ["42", "true", "[a, b]", "{surtitre: x}"])
def test_rule_non_string_template_rejected(tmp_path, value):
    """Gabarit non-chaîne (scalaire, booléen, liste, mapping) : rejet au chargement."""
    content = (
        BROKER_YAML
        + "rules:\n  - name: salon\n    topic: home/x\n"
        + f"    title: {value}\n    short_message: M\n"
    )
    with pytest.raises(ConfigError, match="'title'") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    assert "salon" in str(excinfo.value)


def test_rule_detailed_message_optional(tmp_path):
    """Sans detailed_message : None, la clé n'existe pas dans la règle."""
    content = BROKER_YAML + rule_yaml("home/x")
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.rules[0].detailed_message is None


@pytest.mark.parametrize(
    "field, template, expected",
    [
        ("title", "Alerte { champ}", "placeholder avec espace interdit"),
        ("title", "Alerte {champ", "accolade non fermée"),
        ("title", "Alerte {}", "placeholder vide"),
        ("title", "Alerte {ch{amp}", "'{' interdit dans un placeholder"),
        ("title", "Alerte {a.}", "segment de chemin vide"),
        ("title", "Alerte {.a}", "segment de chemin vide"),
        ("short_message", "Valeur {champ", "accolade non fermée"),
        ("short_message", "Valeur { champ }", "placeholder avec espace interdit"),
        ("short_message", "Valeur {a..b}", "segment de chemin vide"),
        ("detailed_message", "Détail {champ", "accolade non fermée"),
    ],
)
def test_rule_malformed_template_rejected(tmp_path, field, template, expected):
    """AC-1.3.1 : gabarit malformé -> démarrage refusé, règle et champ cités."""
    content = BROKER_YAML + rule_yaml("home/x", **{field: template})
    with pytest.raises(ConfigError, match=re.escape(expected)) as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    message = str(excinfo.value)
    assert "salon" in message
    assert f"'{field}'" in message
    assert template in message


@pytest.mark.parametrize(
    "template",
    ["{payload}", "Alerte {payload}", "{{x}} {payload}", "{payload} !"],
)
def test_rule_raw_mode_only_payload_placeholder_allowed(tmp_path, template):
    """Mode raw : '{payload}' seul est légal, y compris avec '{{' littéral."""
    content = BROKER_YAML + rule_yaml("home/x", payload_format="raw", short_message=template)
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.rules[0].short_message == template


@pytest.mark.parametrize(
    "template, field",
    [
        ("Valeur {champ}", "short_message"),
        ("{capteur.lieu}", "title"),
        ("{payload} et {autre}", "detailed_message"),
    ],
)
def test_rule_raw_mode_other_placeholders_rejected(tmp_path, template, field):
    """AC-1.3.5 : placeholder autre que '{payload}' en mode raw -> rejet au chargement."""
    kwargs = {"payload_format": "raw", "short_message": "{payload}"}
    if field == "title":
        kwargs["title"] = template
    else:
        kwargs[field] = template
    content = BROKER_YAML + rule_yaml("home/x", **kwargs)
    with pytest.raises(ConfigError, match="mode raw") as excinfo:
        load_config(str(write_yaml(tmp_path, content)))
    message = str(excinfo.value)
    assert "salon" in message
    assert f"'{field}'" in message
