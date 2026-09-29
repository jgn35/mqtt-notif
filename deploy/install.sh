#!/bin/sh
# Installation de mqtt-notif sur la machine dédiée (Raspberry Pi OS, systemd).
# Procédure reproductible de bout en bout : utilisateur dédié, venv Python 3.13,
# dépendances, unité systemd, activation. Aucune étape manuelle non documentée
# (le détail de chaque étape est dans deploy/README.md).
#
# Usage (depuis la racine du dépôt, en root) : sudo ./deploy/install.sh
set -eu

SERVICE_NAME=mqtt-notif
SERVICE_USER=mqtt-notif
APP_DIR=/opt/mqtt-notif
VENV_DIR="$APP_DIR/.venv"
UNIT_DST=/etc/systemd/system/"$SERVICE_NAME".service

# Racine du dépôt : parent du répertoire contenant ce script.
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

fail() {
    echo "erreur : $1" >&2
    exit 1
}

# --- Prérequis : échec explicite plutôt que de deviner ----------------------

[ "$(id -u)" -eq 0 ] || fail "lancer en root (sudo ./deploy/install.sh)"
command -v systemctl >/dev/null 2>&1 || fail "systemd introuvable"
systemctl --version >/dev/null 2>&1 || fail "systemctl inutilisable"
command -v python3.13 >/dev/null 2>&1 || fail "python3.13 introuvable (requis par pyproject)"
[ -f "$SRC_DIR/pyproject.toml" ] || fail "pyproject.toml absent — lancer depuis la racine du dépôt"
[ -f "$SRC_DIR/deploy/mqtt-notif.service" ] || fail "deploy/mqtt-notif.service absent"

# --- 1. Utilisateur dédié non-root (sans login interactif) ------------------

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/mqtt-notif --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "utilisateur $SERVICE_USER créé"
else
    echo "utilisateur $SERVICE_USER déjà présent"
fi

# --- 2. Code applicatif dans /opt/mqtt-notif --------------------------------

mkdir -p "$APP_DIR" /etc/mqtt-notif /var/lib/mqtt-notif
# Copie du dépôt hors venv/git/artefacts locaux (tar natif, pas de dépendance rsync).
tar -cf - -C "$SRC_DIR" \
    --exclude=.venv --exclude=.git --exclude=.pytest_cache \
    --exclude=_bmad --exclude=_bmad-output --exclude=.agents \
    . | tar -xf - -C "$APP_DIR"

# --- 3. Venv Python 3.13 + dépendances ---------------------------------------

if command -v uv >/dev/null 2>&1; then
    uv venv --python python3.13 "$VENV_DIR"
    uv pip install --python "$VENV_DIR/bin/python" "$APP_DIR"
else
    python3.13 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet "$APP_DIR"
fi
# Vérification : le console script pyproject (mqtt-notif) est bien installé.
[ -x "$VENV_DIR/bin/$SERVICE_NAME" ] || fail "console script $SERVICE_NAME absent du venv"

# --- 4. Droits minimaux ------------------------------------------------------

# Le durcissement fin (permissions exactes du service-account) est reporté
# au déploiement réel (voir README) ; ici : lecture possible par le service.
chown -R "$SERVICE_USER:$SERVICE_USER" /etc/mqtt-notif /var/lib/mqtt-notif

# --- 5. Unité systemd + activation -------------------------------------------

cp "$SRC_DIR/deploy/mqtt-notif.service" "$UNIT_DST"
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME.service"

echo "installation terminée : systemctl status $SERVICE_NAME"
echo "chemins attendus par l'unité (adaptez l'unité si différents) :"
echo "  MQTT_NOTIF_CONFIG=/etc/mqtt-notif/mqtt-notif.yaml"
echo "  MQTT_NOTIF_FIREBASE_CREDENTIALS=/var/lib/mqtt-notif/service-account.json"
echo "logs : journalctl -u $SERVICE_NAME"
