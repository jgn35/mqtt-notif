"""AC-3.1.3 / AC-3.1.4 / AC-3.1.7 : validations statiques de l'unité systemd
et du script d'installation. Tout ce qui exige un vrai systemd (boot, kill -9,
journalctl) reste une vérification terrain documentée dans deploy/README.md —
ces tests ne vérifient que ce qui est automatisable par relecture : sémantique
de l'unité, chemins par variables d'environnement, utilisateur non-root,
cohérence ExecStart/entrée pyproject, étapes du script d'installation."""

from __future__ import annotations

import importlib
import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT_PATH = os.path.join(PROJECT_ROOT, "deploy", "mqtt-notif.service")
INSTALL_PATH = os.path.join(PROJECT_ROOT, "deploy", "install.sh")
PYPROJECT_PATH = os.path.join(PROJECT_ROOT, "pyproject.toml")
PACKAGE_DIR = os.path.join(PROJECT_ROOT, "mqtt_notif")

CONFIG_ENV = "MQTT_NOTIF_CONFIG"
CREDENTIALS_ENV = "MQTT_NOTIF_FIREBASE_CREDENTIALS"
# Répertoires d'exemple de la machine dédiée : les seuls chemins tolérés dans
# l'unité, uniquement sur des lignes Environment= (marqués à adapter).
EXAMPLE_DIRS = ("/etc/mqtt-notif", "/var/lib/mqtt-notif")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as file:
        return file.read()


def active_lines(text: str) -> list[str]:
    """Lignes actives d'un fichier ini/shell : ni vides ni commentées."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def unit_assignments(lines: list[str]) -> dict[str, str]:
    """Paires Clé=Valeur des lignes actives de l'unité."""
    pairs = {}
    for line in lines:
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def pyproject_console_script() -> tuple[str, str, str]:
    """(nom, module, fonction) de l'entrée [project.scripts] du pyproject."""
    match = re.search(
        r"\[project\.scripts\]\s*\n\s*([\w.-]+)\s*=\s*\"([\w.]+):([\w.]+)\"",
        read(PYPROJECT_PATH),
    )
    assert match is not None, "entrée [project.scripts] absente du pyproject"
    return match.group(1), match.group(2), match.group(3)


def test_unit_restart_on_failure():
    """Restart=on-failure actif : kill -9 -> relance par systemd (AC-3.1.1)."""
    assignments = unit_assignments(active_lines(read(UNIT_PATH)))
    assert assignments.get("Restart") == "on-failure"


def test_unit_restart_sec_bounded():
    """RestartSec actif : la boucle de redémarrage est bornée dans le temps."""
    assignments = unit_assignments(active_lines(read(UNIT_PATH)))
    assert "RestartSec" in assignments


def test_unit_start_limit_documented():
    """StartLimitBurst documenté comme réglage du déploiement (AC-3.1.4) :
    présent dans l'unité (actif ou commenté) avec StartLimitIntervalSec."""
    text = read(UNIT_PATH)
    assert "StartLimitBurst" in text
    assert "StartLimitIntervalSec" in text


def test_unit_type_simple_stdout_to_journald():
    """Type=simple : premier plan, stdout/stderr -> journald par défaut,
    et aucun fichier de log propre n'est ouvert par l'unité."""
    assignments = unit_assignments(active_lines(read(UNIT_PATH)))
    assert assignments.get("Type") == "simple"
    assert "StandardOutput" not in assignments  # défaut systemd : journal
    assert "StandardError" not in assignments


def test_unit_non_root_dedicated_user():
    """Utilisateur dédié non-root défini dans l'unité."""
    assignments = unit_assignments(active_lines(read(UNIT_PATH)))
    assert "User" in assignments
    assert assignments["User"] != "root"
    assert assignments.get("Group", assignments["User"]) != "root"


def test_unit_environment_variables():
    """Les deux chemins passent par les variables d'environnement (AC-3.1.3)."""
    env_lines = [
        line
        for line in active_lines(read(UNIT_PATH))
        if line.startswith("Environment=")
    ]
    joined = "\n".join(env_lines)
    assert f"{CONFIG_ENV}=" in joined, f"{CONFIG_ENV} absente des Environment="
    assert f"{CREDENTIALS_ENV}=" in joined, f"{CREDENTIALS_ENV} absente des Environment="


def test_unit_example_paths_only_on_environment_lines():
    """Aucun chemin codé en dur hors exemples : les répertoires d'exemple
    n'apparaissent que sur des lignes Environment=, jamais ailleurs dans
    la sémantique de l'unité."""
    for line in active_lines(read(UNIT_PATH)):
        if any(directory in line for directory in EXAMPLE_DIRS):
            assert line.startswith("Environment="), f"chemin codé en dur : {line}"


def test_daemon_has_no_hardcoded_deploy_paths():
    """Le démon ne code aucun chemin de déploiement : les répertoires
    d'exemple de la machine dédiée n'apparaissent dans aucun module, et les
    deux variables d'environnement sont bien celles lues au démarrage."""
    sources = [
        os.path.join(PACKAGE_DIR, name)
        for name in os.listdir(PACKAGE_DIR)
        if name.endswith(".py")
    ]
    assert sources, "sources du paquet introuvables"
    for path in sources:
        text = read(path)
        for directory in EXAMPLE_DIRS:
            assert directory not in text, f"{directory} codé en dur dans {path}"
    all_sources = "\n".join(read(path) for path in sources)
    assert CONFIG_ENV in all_sources
    assert CREDENTIALS_ENV in all_sources


def test_execstart_matches_pyproject_entry_point():
    """Cohérence ExecStart/entrée pyproject : le binaire de l'unité est le
    console script déclaré, et son point d'entrée est importable."""
    assignments = unit_assignments(active_lines(read(UNIT_PATH)))
    assert "ExecStart" in assignments
    script_name, module_name, func_name = pyproject_console_script()
    exec_path = assignments["ExecStart"]
    assert os.path.basename(exec_path) == script_name
    module = importlib.import_module(module_name)
    assert callable(getattr(module, func_name))


def test_execstart_matches_install_venv():
    """ExecStart pointe le venv créé par install.sh : même répertoire
    d'application, même nom de console script. VENV_DIR est défini dans
    install.sh comme « $APP_DIR/.venv » : on résout la variable APP_DIR
    pour comparer avec le chemin littéral de l'unité."""
    install_text = read(INSTALL_PATH)
    match = re.search(r"^APP_DIR=?\"?(\S+?)\"?$", install_text, re.MULTILINE)
    assert match is not None, "APP_DIR absent de install.sh"
    app_dir = match.group(1)
    assert re.search(
        rf"^VENV_DIR=\"\$\{{?APP_DIR\}}?/\.venv\"|^VENV_DIR=\"{re.escape(app_dir)}/\.venv\"",
        install_text,
        re.MULTILINE,
    ), "VENV_DIR incohérent avec APP_DIR dans install.sh"
    script_name, _module, _func = pyproject_console_script()
    assignments = unit_assignments(active_lines(read(UNIT_PATH)))
    assert assignments["ExecStart"] == f"{app_dir}/.venv/bin/{script_name}"


def test_install_script_creates_dedicated_user_and_venv():
    """Le script référence l'utilisateur dédié et le venv Python 3.13."""
    install_text = read(INSTALL_PATH)
    assert "SERVICE_USER=mqtt-notif" in install_text
    assert "useradd" in install_text
    assert "python3.13" in install_text
    assert re.search(r"uv venv|python3\.13 -m venv", install_text), (
        "ni uv ni venv ne créent l'environnement virtuel"
    )


def test_install_script_systemd_steps():
    """Le script copie l'unité, fait daemon-reload puis enable --now (AC-3.1.7)."""
    install_text = read(INSTALL_PATH)
    assert "mqtt-notif.service" in install_text
    assert "systemctl daemon-reload" in install_text
    assert re.search(r"systemctl enable --now ", install_text)


def test_install_script_fails_explicitly_on_missing_prerequisites():
    """Le script échoue explicitement sur prérequis manquant (set -eu,
    vérifications préalables) plutôt que de deviner."""
    install_text = read(INSTALL_PATH)
    assert re.search(r"^set -e", install_text, re.MULTILINE)
    assert "python3.13 introuvable" in install_text
    assert "systemd introuvable" in install_text


def test_install_script_reports_expected_paths():
    """Le script rappelle les chemins attendus par l'unité (variables
    d'environnement, exemples à adapter), alignés sur l'unité."""
    install_text = read(INSTALL_PATH)
    assert f"{CONFIG_ENV}=/etc/mqtt-notif/mqtt-notif.yaml" in install_text
    assert f"{CREDENTIALS_ENV}=/var/lib/mqtt-notif/service-account.json" in install_text
