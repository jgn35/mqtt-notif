"""AC-1.2.1 : schéma `rules`, validation stricte citant la règle fautive,
échec rapide au démarrage."""

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
      - name: maison
        topic: 'home/#'
    """
)

BROKER_YAML = "broker:\n  host: h\n  port: 1883\ndevice_token: t\n"


def write_yaml(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def rule_yaml(topic, name="salon"):
    return f'rules:\n  - name: {name}\n    topic: "{topic}"\n'


def test_valid_yaml_loads(tmp_path):
    config = load_config(str(write_yaml(tmp_path, VALID_YAML)))
    assert config.broker_host == "localhost"
    assert config.broker_port == 1883
    assert config.device_token == "abc123"
    assert config.rules == (
        Rule(name="salon", topic="home/salon/capteur"),
        Rule(name="maison", topic="home/#"),
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
        ("home/\\x00salon", "caractères de contrôle interdits"),
        ("home/salon\\x7f", "caractères de contrôle interdits"),
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


def test_duplicate_rule_names_rejected(tmp_path):
    content = BROKER_YAML + rule_yaml("home/x", name="salon") + "  - name: salon\n    topic: home/y\n"
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
        """
    )
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.broker_host == "localhost"
    assert config.device_token == "tok"
    assert config.rules == (Rule(name="salon", topic="home/salon"),)


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
