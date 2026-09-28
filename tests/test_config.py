"""AC-1.2.1 : YAML absent/corrompu/clé manquante -> échec rapide, message explicite.
AC-1.2.1 : section rules validée au chargement (nom unique non vide, motif légal)."""

from __future__ import annotations

import textwrap

import pytest

from mqtt_notif.config import ConfigError, config_path_from_env, load_config, load_config_from_env

VALID_YAML = textwrap.dedent(
    """\
    broker:
      host: localhost
      port: 1883
    device_token: abc123
    rules:
      - name: capteur-salon
        topic: home/salon/capteur
      - name: bouton-niveau
        topic: home/+/bouton
      - name: maison-entiere
        topic: home/#
    """
)


def write_yaml(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_yaml_loads(tmp_path):
    config = load_config(str(write_yaml(tmp_path, VALID_YAML)))
    assert config.broker_host == "localhost"
    assert config.broker_port == 1883
    assert config.device_token == "abc123"
    assert [rule.name for rule in config.rules] == [
        "capteur-salon",
        "bouton-niveau",
        "maison-entiere",
    ]
    assert [rule.topic for rule in config.rules] == [
        "home/salon/capteur",
        "home/+/bouton",
        "home/#",
    ]


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
        "home/salon/capteur",
        "home/+/bouton",
        "home/#",
        "#",
        "+",
    ],
)
def test_wildcard_patterns_now_accepted(tmp_path, topic):
    """Les motifs '+'/'#' sont légaux depuis 1.2 (l'interdit était le comportement 1.1)."""
    content = (
        "broker:\n  host: h\n  port: 1883\ndevice_token: t\n"
        f"rules:\n  - name: r\n    topic: '{topic}'\n"
    )
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.rules[0].topic == topic


@pytest.mark.parametrize(
    "topic",
    [
        "home/#/capteur",  # '#' pas au dernier niveau
        "home/capteur#",  # '#' collé à autre chose
        "home/+capteur",  # '+' pas un niveau entier
        "home/ca+pteur",  # '+' en plein milieu de niveau
        "home/salon capteur",  # espaces
        "home//capteur",  # niveau vide
        "/home",  # niveau vide en tête
        "home/",  # niveau vide en queue
    ],
)
def test_invalid_topic_syntax_rejected_citing_rule(tmp_path, topic):
    """Motif illégal : rejet au démarrage, erreur citant la règle fautive."""
    content = (
        "broker:\n  host: h\n  port: 1883\ndevice_token: t\n"
        f"rules:\n  - name: regle-fautive\n    topic: '{topic}'\n"
    )
    with pytest.raises(ConfigError, match="regle-fautive"):
        load_config(str(write_yaml(tmp_path, content)))


def test_rule_without_topic_rejected_citing_rule_name(tmp_path):
    """Règle sans topic : erreur citant la règle fautive."""
    content = textwrap.dedent(
        """\
        broker:
          host: h
          port: 1883
        device_token: t
        rules:
          - name: bonne
            topic: home/a
          - name: fautive-sans-topic
        """
    )
    with pytest.raises(ConfigError, match="fautive-sans-topic"):
        load_config(str(write_yaml(tmp_path, content)))


def test_empty_rule_name_rejected(tmp_path):
    content = (
        "broker:\n  host: h\n  port: 1883\ndevice_token: t\n"
        "rules:\n  - name: ''\n    topic: home/a\n"
    )
    with pytest.raises(ConfigError, match="'name'"):
        load_config(str(write_yaml(tmp_path, content)))


def test_missing_rule_name_rejected(tmp_path):
    content = (
        "broker:\n  host: h\n  port: 1883\ndevice_token: t\n"
        "rules:\n  - topic: home/a\n"
    )
    with pytest.raises(ConfigError, match="Règle #1"):
        load_config(str(write_yaml(tmp_path, content)))


def test_duplicate_rule_name_rejected_citing_name(tmp_path):
    content = textwrap.dedent(
        """\
        broker:
          host: h
          port: 1883
        device_token: t
        rules:
          - name: doublon
            topic: home/a
          - name: autre
            topic: home/b
          - name: doublon
            topic: home/c
        """
    )
    with pytest.raises(ConfigError, match="doublon.*dupliqué"):
        load_config(str(write_yaml(tmp_path, content)))


def test_rule_not_a_mapping_rejected(tmp_path):
    content = (
        "broker:\n  host: h\n  port: 1883\ndevice_token: t\n"
        "rules:\n  - juste_une_chaine\n"
    )
    with pytest.raises(ConfigError, match="Règle #1"):
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
          - name: '  capteur  '
            topic: '  home/salon  '
        """
    )
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.broker_host == "localhost"
    assert config.device_token == "tok"
    assert config.rules[0].name == "capteur"
    assert config.rules[0].topic == "home/salon"


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
        ("broker:\n  host: localhost\n  port: 1883\ndevice_token: t\nrules: pas_une_liste\n", "'rules'"),
        ("broker:\n  host: localhost\n  port: 1883\ndevice_token: t\nrules: []\n", "'rules'"),
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
    assert config_path_from_env() == str(path)
