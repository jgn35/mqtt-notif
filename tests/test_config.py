"""AC-1.1.7 : YAML absent/corrompu/clé manquante -> échec rapide, message explicite."""

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
    topic: home/salon/capteur
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
    assert config.topic == "home/salon/capteur"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(str(tmp_path / "absent.yaml"))


def test_path_is_a_directory(tmp_path):
    """Un chemin répertoire est une erreur explicite, pas un traceback brut."""
    with pytest.raises(ConfigError, match="illisible"):
        load_config(str(tmp_path))


@pytest.mark.parametrize(
    "topic, expected",
    [
        ("home/+/capteur", "Topic invalide"),
        ("home/#", "Topic invalide"),
        ("home/salon capteur", "Topic invalide"),
    ],
)
def test_invalid_topic_syntax_rejected(tmp_path, topic, expected):
    """Topic avec jokers ou espaces : rejet au démarrage (topic exact attendu en 1.1)."""
    content = f"broker:\n  host: h\n  port: 1883\ndevice_token: t\ntopic: '{topic}'\n"
    with pytest.raises(ConfigError, match=expected):
        load_config(str(write_yaml(tmp_path, content)))


def test_values_are_stripped(tmp_path):
    """Les valeurs sont stockées débarrassées des espaces environnants."""
    content = textwrap.dedent(
        """\
        broker:
          host: '  localhost  '
          port: 1883
        device_token: '  tok  '
        topic: '  home/salon  '
        """
    )
    config = load_config(str(write_yaml(tmp_path, content)))
    assert config.broker_host == "localhost"
    assert config.device_token == "tok"
    assert config.topic == "home/salon"


def test_corrupt_yaml(tmp_path):
    with pytest.raises(ConfigError, match="YAML invalide"):
        load_config(str(write_yaml(tmp_path, "broker: [non, fermé\n  host: x")))


def test_non_mapping_root(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(str(write_yaml(tmp_path, "- juste\n- une liste\n")))


@pytest.mark.parametrize(
    "content, expected",
    [
        ("port: 1883\ndevice_token: t\ntopic: a/b\n", "'broker'"),
        ("host: localhost\nport: 1883\ndevice_token: t\ntopic: a/b\n", "'broker'"),
        ("broker:\n  port: 1883\ndevice_token: t\ntopic: a/b\n", "broker.host"),
        ("broker:\n  host: localhost\ndevice_token: t\ntopic: a/b\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: pas_un_entier\ndevice_token: t\ntopic: a/b\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: true\ndevice_token: t\ntopic: a/b\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: 65536\ndevice_token: t\ntopic: a/b\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: 0\ndevice_token: t\ntopic: a/b\n", "broker.port"),
        ("broker:\n  host: localhost\n  port: 1883\ntopic: a/b\n", "device_token"),
        ("broker:\n  host: localhost\n  port: 1883\ndevice_token: ''\ntopic: a/b\n", "device_token"),
        ("broker:\n  host: localhost\n  port: 1883\ndevice_token: t\n", "'topic'"),
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
