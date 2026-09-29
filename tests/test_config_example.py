"""AC-3.2.1 (support) : l'exemple de YAML du dépôt (config/mqtt-notif.yaml)
reste un exemple valide — chargé par le loader réel du démon (schéma strict,
clés inconnues rejetées), sans aucun secret réel (device_token placeholder).
Anti-rot : si le schéma évolue ou qu'une valeur réelle est tapée dans
l'exemple, ce test échoue."""

from __future__ import annotations

import re
from pathlib import Path

from mqtt_notif.config import load_config
from mqtt_notif.rules import PAYLOAD_FORMAT_JSON, PAYLOAD_FORMAT_RAW

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = REPO_ROOT / "config" / "mqtt-notif.yaml"
EXPLOITATION_DOC = REPO_ROOT / "docs" / "EXPLOITATION.md"
DEVICE_TOKEN_PLACEHOLDER = "PLACEHOLDER-DEVICE-TOKEN-A-REMPLACER"


def test_config_example_loads_through_real_loader():
    """L'exemple charge via le loader du démon, sans ConfigError, et épingle
    la règle livrée : topic d'exemple, hypothèse raw (à confirmer sur le
    terrain), gabarits en {payload} conformes au mode raw."""
    config = load_config(str(EXAMPLE_PATH))
    assert config.broker_port == 1883
    assert config.broker_host == "192.0.2.10"
    rule = config.rules[0]
    assert rule.name == "bouton-salon"
    assert rule.topic == "home/salon/bouton"
    # Hypothèse livrée (à confirmer sur le terrain, section (1) de la doc).
    assert rule.payload_format == PAYLOAD_FORMAT_RAW
    assert "{payload}" in rule.short_message
    assert "{payload}" in rule.detailed_message


def test_config_example_keeps_placeholder_secrets():
    """Garde placeholders : le device_token de l'exemple du dépôt est le
    placeholder marqué, jamais une valeur réelle — un token réel tapé dans
    l'exemple fait échouer ce test."""
    config = load_config(str(EXAMPLE_PATH))
    assert config.device_token == DEVICE_TOKEN_PLACEHOLDER, (
        "config/mqtt-notif.yaml doit contenir le placeholder de device_token, "
        "pas une valeur réelle (les secrets vivent dans le YAML de production "
        "sur la Pi)"
    )


def test_exploitation_doc_rule_snippets_load(tmp_path):
    """Matrice I/O (cas JSON / cas brut) : les règles documentées dans la
    section (1) de docs/EXPLOITATION.md (cas A json, cas B raw) chargent
    via le loader réel — gabarits valides pour le format déclaré, schéma
    strict respecté. Un snippet de doc pourri fait échouer ce test."""
    doc = EXPLOITATION_DOC.read_text(encoding="utf-8")
    # Seule la section (1) documente les cas A/B : un futur bloc yaml
    # ajouté ailleurs dans la doc ne doit pas casser ce test.
    section = re.search(r"## \(1\).*?(?=## \(2\))", doc, flags=re.DOTALL)
    assert section is not None, "docs/EXPLOITATION.md : section (1) introuvable"
    snippets = re.findall(r"```yaml\n(.*?)```", section.group(0), flags=re.DOTALL)
    formats = []
    for indice, snippet in enumerate(snippets):
        complet = (
            "broker:\n"
            "  host: broker.lan\n"
            "  port: 1883\n"
            "device_token: token-exemple\n"
            "rules:\n"
            f"{snippet}"
        )
        chemin = tmp_path / f"snippet-{indice}.yaml"
        chemin.write_text(complet, encoding="utf-8")
        config = load_config(str(chemin))
        assert len(config.rules) == 1
        formats.append(config.rules[0].payload_format)
    assert formats == [PAYLOAD_FORMAT_JSON, PAYLOAD_FORMAT_RAW], (
        "docs/EXPLOITATION.md doit documenter exactement les deux cas : "
        "cas A (payload_format json) puis cas B (payload_format raw), "
        "tous deux chargeables par le démon"
    )


def test_exploitation_doc_pins_sm1_measurement_procedure():
    """Matrice I/O (latence mesurée) : la procédure SM-1 de
    docs/EXPLOITATION.md cite les lignes journald exactes de la mesure et
    fournit le relevé à remplir — le terrain reste manuel, la consigne est
    épinglée."""
    doc = EXPLOITATION_DOC.read_text(encoding="utf-8")
    for ancre in (
        "mqtt message topic=",  # réception (main.py)
        "fcm sent topic=",  # envoi (notifier.py)
        "journalctl -u mqtt-notif",
        "Relevé SM-1",
        "Verdict (< 2 000 ms)",
    ):
        assert ancre in doc, (
            f"docs/EXPLOITATION.md doit citer « {ancre} » (procédure de "
            "mesure SM-1 ou relevé à remplir)"
        )
