"""AC-3.1.1 à AC-3.1.7 : validations statiques de l'unité systemd et du
script d'installation (unité, chemins par variables : AC-3.1.3 ; échec
visible borné : AC-3.1.4 ; installation reproductible : AC-3.1.7).

Tout ce qui exige un vrai systemd (boot, kill -9, journalctl) reste une
vérification terrain documentée dans deploy/README.md — ces tests ne
vérifient que ce qui est automatisable par relecture : sémantique de
l'unité, limites de démarrage actives, chemins par variables
d'environnement, utilisateur non-root cross-pinné avec le script,
cohérence ExecStart/Documentation/APP_DIR/entrée pyproject, syntaxe du
script (`sh -n`), étapes et gate d'activation du script, aucun chemin de
déploiement codé en dur dans le paquet."""

from __future__ import annotations

import importlib
import os
import re
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT_PATH = os.path.join(PROJECT_ROOT, "deploy", "mqtt-notif.service")
INSTALL_PATH = os.path.join(PROJECT_ROOT, "deploy", "install.sh")
PYPROJECT_PATH = os.path.join(PROJECT_ROOT, "pyproject.toml")
PACKAGE_DIR = os.path.join(PROJECT_ROOT, "mqtt_notif")

CONFIG_ENV = "MQTT_NOTIF_CONFIG"
CREDENTIALS_ENV = "MQTT_NOTIF_FIREBASE_CREDENTIALS"
# Répertoires d'exemple de la machine dédiée : les seuls chemins de
# configuration tolérés dans l'unité, uniquement sur des lignes Environment=
# (marqués à adapter).
EXAMPLE_DIRS = ("/etc/mqtt-notif", "/var/lib/mqtt-notif")
# Limites de démarrage actives et calibrées avec RestartSec (Implementation
# Notes du spec) : 5 démarrages espacés de RestartSec=5 tiennent dans la
# fenêtre d'une minute.
START_LIMIT_BURST = "5"
START_LIMIT_INTERVAL = "1min"


def read(path: str) -> str:
    with open(path, encoding="utf-8") as file:
        return file.read()


def active_lines(text: str) -> list[str]:
    """Lignes actives d'un fichier ini/shell : ni vides ni commentées."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def unit_assignments(lines: list[str]) -> dict[str, str]:
    """Paires Clé=Valeur des lignes actives de l'unité."""
    pairs = {}
    for line in lines:
        key, _, value = line.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def unit() -> dict[str, str]:
    return unit_assignments(active_lines(read(UNIT_PATH)))


def pyproject_console_script() -> tuple[str, str, str]:
    """(nom, module, fonction) de l'entrée [project.scripts] du pyproject."""
    match = re.search(
        r"\[project\.scripts\]\s*\n\s*([\w.-]+)\s*=\s*\"([\w.]+):([\w.]+)\"",
        read(PYPROJECT_PATH),
    )
    assert match is not None, "entrée [project.scripts] absente du pyproject"
    return match.group(1), match.group(2), match.group(3)


def install_app_dir() -> str:
    """APP_DIR de install.sh, par regex stricte : `APP_DIR=` ancré aux deux
    bouts — `APP_DIRECTORY=...` ne doit PAS matcher (dérive silencieuse sinon)."""
    match = re.search(r"^APP_DIR=(\S+)$", read(INSTALL_PATH), re.MULTILINE)
    assert match is not None, "APP_DIR absent de install.sh"
    # La regex est stricte : un préfixe comme APP_DIRECTORY ne capture pas.
    assert re.search(r"^APP_DIR=(\S+)$", "APP_DIRECTORY=/x\n", re.MULTILINE) is None
    return match.group(1)


def install_service_user() -> str:
    """SERVICE_USER de install.sh, même regex stricte que APP_DIR."""
    match = re.search(r"^SERVICE_USER=(\S+)$", read(INSTALL_PATH), re.MULTILINE)
    assert match is not None, "SERVICE_USER absent de install.sh"
    return match.group(1)


# --- Unité : sémantique de démarrage ------------------------------------------


def test_unit_restart_on_failure():
    """Restart=on-failure actif : kill -9 -> relance par systemd (AC-3.1.2)."""
    assert unit().get("Restart") == "on-failure"


def test_unit_restart_sec():
    """RestartSec actif : relance bornée dans le temps après une défaillance."""
    assert unit().get("RestartSec") == "5"


def test_unit_start_limits_active_and_calibrated():
    """Limites de démarrage ACTIVES (AC-3.1.4) : avec RestartSec=5, les
    défauts systemd (5 / 10 s) ne déclencheraient jamais — boucle infinie,
    jamais d'état failed. Ici 5 démarrages espacés de 5 s tiennent dans la
    fenêtre d'une minute : la limite déclenche réellement."""
    assignments = unit()
    assert assignments.get("StartLimitBurst") == START_LIMIT_BURST, (
        "StartLimitBurst doit être actif (non commenté) dans l'unité"
    )
    assert assignments.get("StartLimitIntervalSec") == START_LIMIT_INTERVAL, (
        "StartLimitIntervalSec doit être actif (non commenté) dans l'unité"
    )
    restart_sec = int(assignments["RestartSec"])
    burst = int(assignments["StartLimitBurst"])
    # Calibration : les `burst` démarrages espacés de `restart_sec` tiennent
    # dans la fenêtre, sinon la limite ne déclencherait jamais.
    assert restart_sec * burst <= 60


def test_unit_type_simple_stdout_to_journald():
    """Type=simple : premier plan, stdout/stderr -> journald par défaut,
    et aucun fichier de log propre n'est ouvert par l'unité (AC-3.1.5)."""
    assignments = unit()
    assert assignments.get("Type") == "simple"
    assert "StandardOutput" not in assignments  # défaut systemd : journal
    assert "StandardError" not in assignments


def test_unit_wanted_by_multi_user_target():
    """WantedBy=multi-user.target : la seule section qui porte le démarrage
    au boot (AC-3.1.1) — sans elle, enable échoue ou le boot ne démarre pas."""
    assert unit().get("WantedBy") == "multi-user.target"


def test_unit_no_duplicate_semantic_keys():
    """Clés sémantiques uniques dans l'unité (hors Environment=) :
    unit_assignments écrase les doublons — un doublon passerait inaperçu
    et l'assertion sur la clé garderait l'ancienne valeur silencieusement."""
    keys = [
        line.partition("=")[0].strip()
        for line in active_lines(read(UNIT_PATH))
        if not line.startswith("Environment=")
    ]
    assert keys, "aucune clé sémantique dans l'unité"
    assert len(keys) == len(set(keys)), f"clés dupliquées dans l'unité : {keys}"


# --- Unité : chemins par variables, utilisateur non-root ----------------------


def test_unit_non_root_dedicated_user():
    """Utilisateur dédié non-root défini dans l'unité."""
    assignments = unit()
    assert assignments["User"] != "root"
    assert assignments.get("Group", assignments["User"]) != "root"


def test_unit_user_group_match_install_service_user():
    """Cross-pin User/Group de l'unité ↔ SERVICE_USER de install.sh : une
    dérive silencieuse casserait le chown du script au déploiement."""
    service_user = install_service_user()
    assignments = unit()
    assert assignments.get("User") == service_user
    assert assignments.get("Group") == service_user


def test_unit_environment_variables():
    """Les deux chemins passent par les variables d'environnement (AC-3.1.3)."""
    env_lines = [line for line in active_lines(read(UNIT_PATH)) if line.startswith("Environment=")]
    joined = "\n".join(env_lines)
    assert f"{CONFIG_ENV}=" in joined, f"{CONFIG_ENV} absente des Environment="
    assert f"{CREDENTIALS_ENV}=" in joined, (
        f"{CREDENTIALS_ENV} absente des Environment="
    )


def test_unit_example_paths_only_on_environment_lines():
    """Aucun chemin de configuration codé en dur : les répertoires d'exemple
    n'apparaissent que sur des lignes Environment=, jamais ailleurs dans
    la sémantique de l'unité (AC-3.1.3)."""
    for line in active_lines(read(UNIT_PATH)):
        if any(directory in line for directory in EXAMPLE_DIRS):
            assert line.startswith("Environment="), f"chemin codé en dur : {line}"


def test_daemon_has_no_hardcoded_deploy_paths():
    """Le démon ne code aucun chemin de déploiement : les répertoires
    d'exemple n'apparaissent dans aucun module du paquet (parcours os.walk
    complet, sous-répertoires compris), et les deux variables
    d'environnement sont bien celles lues au démarrage."""
    sources = []
    for root, _dirs, names in os.walk(PACKAGE_DIR):
        if "__pycache__" in root:
            continue
        sources.extend(os.path.join(root, name) for name in names if name.endswith(".py"))
    assert sources, "sources du paquet introuvables"
    for path in sources:
        text = read(path)
        for directory in EXAMPLE_DIRS:
            assert directory not in text, f"{directory} codé en dur dans {path}"
    all_sources = "\n".join(read(path) for path in sources)
    assert CONFIG_ENV in all_sources
    assert CREDENTIALS_ENV in all_sources


# --- Unité : cohérence ExecStart/Documentation/pyproject/install.sh -----------


def test_execstart_matches_pyproject_entry_point():
    """Cohérence ExecStart/entrée pyproject : le binaire de l'unité est le
    console script déclaré, et son point d'entrée est importable."""
    assignments = unit()
    assert "ExecStart" in assignments
    script_name, module_name, func_name = pyproject_console_script()
    assert os.path.basename(assignments["ExecStart"]) == script_name
    module = importlib.import_module(module_name)
    assert callable(getattr(module, func_name))


def test_execstart_and_documentation_match_app_dir():
    """ExecStart ET Documentation= pointent le répertoire d'application
    installé (APP_DIR de install.sh) : pas de dérive silencieuse si APP_DIR
    change — Documentation= est cross-pinné au même titre qu'ExecStart."""
    app_dir = install_app_dir()
    script_name, _module, _func = pyproject_console_script()
    install_text = read(INSTALL_PATH)
    assert re.search(
        rf'^VENV_DIR="\$\{{?APP_DIR\}}?/\.venv"$',
        install_text,
        re.MULTILINE,
    ), "VENV_DIR incohérent avec APP_DIR dans install.sh"
    assignments = unit()
    assert assignments["ExecStart"] == f"{app_dir}/.venv/bin/{script_name}"
    assert assignments["Documentation"] == f"file://{app_dir}/deploy/README.md"


# --- Script d'installation : syntaxe et prérequis ------------------------------


def test_install_script_posix_syntax():
    """`sh -n` exécuté sur install.sh : une régression de flux de contrôle
    (fi/then, quotes) ne doit pas passer verte."""
    result = subprocess.run(["sh", "-n", INSTALL_PATH], capture_output=True, text=True)
    assert result.returncode == 0, f"sh -n échoue sur install.sh : {result.stderr}"


def test_install_script_fails_explicitly_on_missing_prerequisites():
    """Le script échoue explicitement sur prérequis manquant (set -eu,
    vérifications préalables) plutôt que de deviner : root, systemd,
    python3.13, outils utilisés, ensurepip pour la branche pip (paquet
    python3.13-venv)."""
    install_text = read(INSTALL_PATH)
    assert re.search(r"^set -eu$", install_text, re.MULTILINE)
    assert "lancer en root" in install_text
    assert "systemd introuvable" in install_text
    assert "python3.13 introuvable" in install_text
    # Chaque outil utilisé plus bas est vérifié (tar, sed, cmp, install,
    # mktemp, useradd, su, getent) — pas seulement mentionné en commentaire.
    tool_loop = re.search(r"^for tool in ([\w ]+); do$", install_text, re.MULTILINE)
    assert tool_loop is not None, "boucle de vérification des outils absente"
    declared = tool_loop.group(1).split()
    for tool in ("tar", "sed", "cmp", "install", "mktemp", "useradd", "su", "getent"):
        assert tool in declared, f"prérequis non vérifié : {tool}"
    # Branche pip : ensurepip vérifié par exécution (pas seulement évoqué).
    assert "python3.13 -m ensurepip --version" in install_text
    assert "python3.13-venv" in install_text


def test_install_script_creates_dedicated_user_and_venv():
    """Le script référence l'utilisateur dédié (compte système sans login,
    propriétés épinglées sur la ligne useradd) et le venv Python 3.13."""
    install_text = read(INSTALL_PATH)
    useradd_match = re.search(r"^[ \t]*useradd .*$", install_text, re.MULTILINE)
    assert useradd_match is not None, "useradd absent de install.sh"
    useradd_line = useradd_match.group(0)
    assert "--system" in useradd_line
    assert "--shell /usr/sbin/nologin" in useradd_line
    assert re.search(r'command -v python3\.13 >/dev/null', install_text)
    assert re.search(r"uv venv|python3\.13 -m venv", install_text), (
        "ni uv ni venv ne créent l'environnement virtuel"
    )


# --- Script d'installation : copie du code et de l'unité ----------------------


def test_install_script_copy_without_hidden_status():
    """Copie du dépôt sans statut masqué : archive temporaire (mktemp),
    statut de CHAQUE tar vérifié, jamais de pipeline `| tar` — sh POSIX
    n'a pas pipefail, un pipeline masquerait l'échec du premier tar."""
    install_text = read(INSTALL_PATH)
    assert re.search(r"^ARCHIVE=\$\(mktemp\)", install_text, re.MULTILINE)
    assert re.search(r'tar -cf "\$ARCHIVE"', install_text)
    assert re.search(r'tar -xf "\$ARCHIVE" -C "\$APP_DIR"', install_text)
    assert 'tar -cf "$ARCHIVE"' in install_text and "|| fail" in install_text
    for match in re.finditer(r"^[ \t]*tar -[cx]f", install_text, re.MULTILINE):
        # Découpage tolérant : la ligne tar peut être la dernière du fichier.
        line = install_text[match.start() :].split("\n", 1)[0]
        assert "|| fail" in line or line.rstrip().endswith("\\"), (
            f"tar sans vérification de statut : {line.strip()}"
        )
    assert not re.search(r"tar -cf -[^|]*\|\s*tar", install_text), (
        "pipeline tar détecté : seul le statut du dernier tar déciderait"
    )


def test_install_script_excludes_local_artifacts():
    """Excludes du tar étendus : venv, git, caches et artefacts de build ne
    sont jamais copiés sur la machine dédiée."""
    install_text = read(INSTALL_PATH)
    for exclude in (
        ".venv",
        ".git",
        ".pytest_cache",
        "__pycache__",
        "*.egg-info",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "_bmad",
        "_bmad-output",
        ".agents",
    ):
        # Le motif est quoté dans le script (globbing du shell sinon).
        assert f"--exclude={exclude}" in install_text or (
            f"--exclude='{exclude}'" in install_text
        ), f"exclude manquant : {exclude}"


def test_install_script_clean_reinstall():
    """Réinstallation propre : APP_DIR vidé avant extraction (pas de reliquat
    de fichiers obsolètes) ; le service actif est ARRÊTÉ avant le remplacement
    du code (sinon il tournerait sur des inodes supprimés, et un échec
    ultérieur laisserait la machine sans code fonctionnel) puis relancé ;
    reset-failed (tolérant) précède l'activation, sinon le start échouerait
    avec « start request repeated too quickly » sur une unité restée failed ;
    l'unité adaptée par l'opérateur est conservée avec avertissement (copie
    en .new, jamais écrasée sans avertissement)."""
    install_text = read(INSTALL_PATH)
    # APP_DIR vidé avant extraction, jamais fusion.
    assert re.search(r'^rm -rf "\$APP_DIR"$', install_text, re.MULTILINE)
    assert re.search(
        r'^rm -rf "\$APP_DIR"$\n^mkdir -p "\$APP_DIR"$', install_text, re.MULTILINE
    )
    # Détection du service actif, puis arrêt AVANT le remplacement du code.
    assert re.search(r'systemctl is-active --quiet "\$SERVICE_NAME"', install_text)
    assert re.search(r'^\s*systemctl stop "\$SERVICE_NAME"$', install_text, re.MULTILINE)
    assert install_text.index('systemctl stop "$SERVICE_NAME"') < install_text.index(
        'rm -rf "$APP_DIR"'
    )
    # reset-failed tolérant avant l'activation (unité possiblement failed).
    assert re.search(
        r'systemctl reset-failed "\$SERVICE_NAME" 2>/dev/null \|\| true', install_text
    )
    # Relance du service arrêté après activation (enable seul ne suffit pas).
    assert re.search(r'^\s*systemctl start "\$SERVICE_NAME"$', install_text, re.MULTILINE)
    assert install_text.index('systemctl start "$SERVICE_NAME"') > install_text.index(
        "systemctl enable"
    )
    # Unité adaptée : comparaison avec le dépôt, copie en .new, jamais d'écrasement.
    assert 'cmp -s "$SRC_DIR/$UNIT_SRC" "$UNIT_DST"' in install_text
    assert '"$UNIT_DST.new"' in install_text


def test_install_script_unit_mode_fixed():
    """L'unité est copiée avec un mode fixé (install -m 644) : le mode de
    l'unité ne doit pas dépendre du checkout du dépôt."""
    install_text = read(INSTALL_PATH)
    assert re.search(r'install -m 644 "\$SRC_DIR/\$UNIT_SRC" "\$UNIT_DST"', install_text)


# --- Script d'installation : gate et activation --------------------------------


def test_install_script_activation_gate():
    """Gate avant activation : chemins lus dans l'unité installée (forme
    quotée ou nue acceptée), existence ET lisibilité par le service (la gate
    tourne en root : un fichier root-only passerait un test root), YAML
    validé par le chargeur du démon — sinon échec explicite, jamais
    « installation terminée » sur un service défectueux (AC-3.1.4, AC-3.1.7)."""
    install_text = read(INSTALL_PATH)
    # Chemins lus dans l'unité installée (celle qui tournera), guillemets
    # optionnels acceptés (Environment="VAR=path" est une forme légale).
    for var, target in (
        (CONFIG_ENV, "CONFIG_PATH"),
        (CREDENTIALS_ENV, "CREDENTIALS_PATH"),
    ):
        sed_match = re.search(
            rf'^{target}=\$\(sed -n \'([^\']+)\' "\$UNIT_DST"',
            install_text,
            re.MULTILINE,
        )
        assert sed_match is not None, f"extraction sed de {var} absente de install.sh"
        assert var in sed_match.group(1), f"{var} absente du sed de {target}"
        assert '"\\{0,1\\}' in sed_match.group(1), (
            f"le sed de {target} n'accepte pas la forme quotée"
        )
    # Existence vérifiée avec échec explicite.
    assert '[ -f "$CONFIG_PATH" ]' in install_text
    assert '[ -f "$CREDENTIALS_PATH" ]' in install_text
    # Lisibilité testée EN TANT QUE le service user (test exécuté via su,
    # pas par root) — comparaison textuelle exacte de la ligne.
    for path_var in ("$CONFIG_PATH", "$CREDENTIALS_PATH"):
        readability_line = (
            f'su -s /bin/sh -c \'test -r "$1"\' "$SERVICE_USER" sh "{path_var}"'
        )
        assert readability_line in install_text, (
            f"lisibilité par le service non testée pour {path_var}"
        )
    # YAML validé par le chargeur du démon (python du venv, MQTT_NOTIF_CONFIG posée).
    assert re.search(
        r'MQTT_NOTIF_CONFIG="\$CONFIG_PATH" "\$VENV_DIR/bin/python" -c',
        install_text,
    )
    assert "load_store_from_env" in install_text
    # Échecs explicites de la gate.
    assert "gate d'activation : YAML de règles absent" in install_text
    assert "gate d'activation : service-account FCM absent" in install_text
    assert "gate d'activation : YAML de règles illisible par" in install_text
    assert "gate d'activation : YAML de règles invalide" in install_text
    # La gate précède l'activation (daemon-reload/enable).
    gate_pos = install_text.index("gate d'activation : YAML")
    assert gate_pos < install_text.index("systemctl daemon-reload")
    assert gate_pos < install_text.index("systemctl enable --now")


def test_install_script_prepares_config_dirs_from_unit_paths():
    """mkdir/chown portent sur les dirname des chemins lus dans l'unité
    installée — jamais sur des répertoires d'exemple codés en dur : si
    l'opérateur a adapté l'unité (flux documenté), ce sont SES répertoires
    qui sont préparés et attribués ; l'omission du chown déférait la gate
    silencieusement (répertoires créés root-only)."""
    install_text = read(INSTALL_PATH)
    assert 'CONFIG_DIR=$(dirname "$CONFIG_PATH")' in install_text
    assert 'CREDENTIALS_DIR=$(dirname "$CREDENTIALS_PATH")' in install_text
    assert 'mkdir -p "$CONFIG_DIR" "$CREDENTIALS_DIR"' in install_text
    assert (
        'chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$CREDENTIALS_DIR"'
        in install_text
    )
    # La préparation suit l'extraction des chemins (sinon elle préparerait
    # les mauvais répertoires sur une unité adaptée).
    assert install_text.index('mkdir -p "$CONFIG_DIR"') > install_text.index(
        "CREDENTIALS_PATH=$(sed"
    )


def test_install_script_finds_uv_of_sudo_user():
    """uv installé dans ~/.local/bin de l'appelant est invisible sous sudo
    (secure_path) : la branche pip serait prise silencieusement et
    échouerait sur ensurepip alors qu'uv était disponible. Le choix de branche
    passe par UV_BIN, cherché aussi via le HOME de SUDO_USER."""
    install_text = read(INSTALL_PATH)
    assert "${SUDO_USER:-}" in install_text
    assert '.local/bin/uv' in install_text
    assert re.search(r"^UV_BIN=\$\(command -v uv 2>/dev/null \|\| true\)$", install_text, re.MULTILINE)
    # La branche et les commandes uv utilisent UV_BIN, pas un bare `uv`.
    assert re.search(r'^if \[ -n "\$UV_BIN" \]; then$', install_text, re.MULTILINE)
    assert '"$UV_BIN" venv --python python3.13 "$VENV_DIR"' in install_text
    # Le prérequis ensurepip n'est exigé que sans uv.
    assert re.search(r'^if \[ -z "\$UV_BIN" \]; then$', install_text, re.MULTILINE)


def test_install_script_systemd_steps():
    """Le script copie l'unité, fait daemon-reload puis enable --now
    (AC-3.1.7)."""
    install_text = read(INSTALL_PATH)
    assert "systemctl daemon-reload" in install_text
    assert re.search(r'systemctl enable --now "\$SERVICE_NAME"', install_text)
    assert install_text.index("systemctl daemon-reload") < install_text.index(
        "systemctl enable --now"
    )


def test_install_script_reports_expected_paths():
    """Le script rappelle les chemins lus dans l'unité installée (variables
    d'environnement), alignés sur l'unité."""
    install_text = read(INSTALL_PATH)
    assert f"MQTT_NOTIF_CONFIG=$CONFIG_PATH" in install_text
    assert f"MQTT_NOTIF_FIREBASE_CREDENTIALS=$CREDENTIALS_PATH" in install_text
