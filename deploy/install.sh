#!/bin/sh
# Installation de mqtt-notif sur la machine dédiée (Raspberry Pi OS, systemd).
# Procédure reproductible de bout en bout : prérequis vérifiés, utilisateur
# dédié non-root, venv Python 3.13, dépendances, unité systemd copiée avec
# mode fixé, gate d'activation, daemon-reload, enable --now. Aucune étape
# manuelle non documentée (le détail de chaque étape est dans deploy/README.md).
#
# Usage (depuis la racine du dépôt, en root) : sudo ./deploy/install.sh
set -eu

SERVICE_NAME=mqtt-notif
SERVICE_USER=mqtt-notif
APP_DIR=/opt/mqtt-notif
VENV_DIR="$APP_DIR/.venv"
UNIT_SRC=deploy/mqtt-notif.service
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
[ -f "$SRC_DIR/$UNIT_SRC" ] || fail "$UNIT_SRC absent — lancer depuis la racine du dépôt"
# Outils utilisés plus bas : leur absence doit échouer ici, explicitement.
for tool in tar sed cmp install mktemp useradd su getent; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool introuvable (prérequis)"
done

# uv peut vivre dans ~/.local/bin de l'appelant, invisible sous sudo
# (secure_path) : sans cette recherche, la branche pip serait prise
# silencieusement et échouerait sur ensurepip alors qu'uv était disponible.
UV_BIN=$(command -v uv 2>/dev/null || true)
if [ -z "$UV_BIN" ] && [ -n "${SUDO_USER:-}" ]; then
    SUDO_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    if [ -n "$SUDO_HOME" ] && [ -x "$SUDO_HOME/.local/bin/uv" ]; then
        UV_BIN="$SUDO_HOME/.local/bin/uv"
    fi
fi

# Branche pip (sans uv) : `python3.13 -m venv` exige ensurepip — paquet
# python3.13-venv sur Debian/Trixie (voir deploy/README.md, prérequis).
if [ -z "$UV_BIN" ]; then
    python3.13 -m ensurepip --version >/dev/null 2>&1 \
        || fail "ensurepip indisponible : python3.13 -m venv échouera — installez le paquet python3.13-venv (ou uv)"
fi

# --- 1. Utilisateur dédié non-root (sans login interactif) ------------------

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/mqtt-notif --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "utilisateur $SERVICE_USER créé"
else
    echo "utilisateur $SERVICE_USER déjà présent"
fi

# --- 2. Arrêt du service actif avant remplacement du code (réinstallation) --
#
# Sans arrêt, le processus actif continuerait de tourner sur des inodes
# supprimés, et tout échec ultérieur (venv, pip, gate) laisserait la machine
# sans code fonctionnel. Le service est relancé à l'activation (étape 8).

was_active=0
if systemctl is-active --quiet "$SERVICE_NAME"; then
    was_active=1
    systemctl stop "$SERVICE_NAME"
fi

# --- 3. Code applicatif dans /opt/mqtt-notif --------------------------------
#
# Copie par archive temporaire (tar natif, pas de dépendance rsync) : le
# statut de chaque tar est vérifié explicitement — jamais un pipeline dont
# seul le dernier élément déciderait (sh POSIX n'a pas pipefail : un
# pipeline masquerait l'échec du premier tar et installerait une copie
# partielle silencieuse).
# Réinstallation propre : APP_DIR est vidé avant extraction — aucun reliquat
# de fichier obsolète ne survit à une réinstallation.

ARCHIVE=$(mktemp) || fail "création de l'archive temporaire impossible"
trap 'rm -f "$ARCHIVE"' EXIT HUP INT TERM

tar -cf "$ARCHIVE" -C "$SRC_DIR" \
    --exclude=.venv --exclude=.git --exclude=.pytest_cache \
    --exclude=__pycache__ --exclude='*.egg-info' \
    --exclude=.mypy_cache --exclude=.ruff_cache --exclude=dist \
    --exclude=_bmad --exclude=_bmad-output --exclude=.agents \
    . || fail "archivage du dépôt impossible"

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
tar -xf "$ARCHIVE" -C "$APP_DIR" || fail "extraction du dépôt dans $APP_DIR impossible"
rm -f "$ARCHIVE"
trap - EXIT HUP INT TERM

# --- 4. Venv Python 3.13 + dépendances ---------------------------------------

if [ -n "$UV_BIN" ]; then
    "$UV_BIN" venv --python python3.13 "$VENV_DIR"
    "$UV_BIN" pip install --python "$VENV_DIR/bin/python" "$APP_DIR"
else
    python3.13 -m venv "$VENV_DIR" \
        || fail "création du venv impossible (ensurepip manquant ? paquet python3.13-venv)"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet "$APP_DIR"
fi
# Vérification : le console script pyproject (mqtt-notif) est bien installé.
[ -x "$VENV_DIR/bin/$SERVICE_NAME" ] || fail "console script $SERVICE_NAME absent du venv"

# --- 5. Unité systemd ---------------------------------------------------------
#
# Copie avec mode fixé (install -m 644). L'unité installée n'est jamais
# écrasée silencieusement : si l'opérateur l'a adaptée (chemins réels du
# déploiement), elle est conservée et la nouvelle version du dépôt est posée
# à côté (.new) pour relecture — jamais écrasée sans avertissement.

if [ -f "$UNIT_DST" ] && ! cmp -s "$SRC_DIR/$UNIT_SRC" "$UNIT_DST"; then
    install -m 644 "$SRC_DIR/$UNIT_SRC" "$UNIT_DST.new" \
        || fail "copie de la nouvelle unité impossible"
    echo "attention : $UNIT_DST diffère du dépôt (adaptée par l'opérateur ?)" >&2
    echo "  -> unité installée conservée, nouvelle version posée dans $UNIT_DST.new" >&2
    echo "  -> comparez, fusionnez, puis déplacez-la vous-même si voulu" >&2
else
    install -m 644 "$SRC_DIR/$UNIT_SRC" "$UNIT_DST" || fail "copie de l'unité impossible"
fi

# --- 6. Chemins et répertoires de configuration ------------------------------
#
# Chemins lus dans l'unité installée (celle qui tournera réellement, y
# compris adaptée par l'opérateur), forme quotée ou nue acceptée. Les
# répertoires sont préparés d'après CES chemins, jamais d'après des
# répertoires d'exemple codés en dur.

CONFIG_PATH=$(sed -n 's/^Environment="\{0,1\}MQTT_NOTIF_CONFIG=\([^"]*\)"\{0,1\}$/\1/p' "$UNIT_DST" | tail -n 1)
CREDENTIALS_PATH=$(sed -n 's/^Environment="\{0,1\}MQTT_NOTIF_FIREBASE_CREDENTIALS=\([^"]*\)"\{0,1\}$/\1/p' "$UNIT_DST" | tail -n 1)
[ -n "$CONFIG_PATH" ] || fail "MQTT_NOTIF_CONFIG absente de l'unité installée"
[ -n "$CREDENTIALS_PATH" ] || fail "MQTT_NOTIF_FIREBASE_CREDENTIALS absente de l'unité installée"

CONFIG_DIR=$(dirname "$CONFIG_PATH")
CREDENTIALS_DIR=$(dirname "$CREDENTIALS_PATH")
mkdir -p "$CONFIG_DIR" "$CREDENTIALS_DIR"
# Le durcissement fin (permissions exactes du service-account) est reporté
# au déploiement réel (voir README) ; ici : lecture possible par le service.
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$CREDENTIALS_DIR"

# --- 7. Gate d'activation ----------------------------------------------------
#
# Le démon échoue vite (exit 1) au démarrage uniquement si le YAML est
# absent ou invalide ; un service-account absent ne fait pas échouer le
# démarrage (initialisation FCM paresseuse au premier envoi : le service
# reste actif, l'erreur est journalisée à chaque envoi). La gate couvre les
# deux à l'activation, pour ne jamais afficher « installation terminée »
# sur un service défectueux : existence, lisibilité PAR LE SERVICE (la gate
# tourne en root : un test root passerait un fichier root-only) et YAML
# validé par le chargeur du démon.

[ -f "$CONFIG_PATH" ] \
    || fail "gate d'activation : YAML de règles absent ($CONFIG_PATH) — posez-le puis relancez"
[ -f "$CREDENTIALS_PATH" ] \
    || fail "gate d'activation : service-account FCM absent ($CREDENTIALS_PATH) — posez-le puis relancez"
if ! su -s /bin/sh -c 'test -r "$1"' "$SERVICE_USER" sh "$CONFIG_PATH"; then
    fail "gate d'activation : YAML de règles illisible par $SERVICE_USER ($CONFIG_PATH)"
fi
if ! su -s /bin/sh -c 'test -r "$1"' "$SERVICE_USER" sh "$CREDENTIALS_PATH"; then
    fail "gate d'activation : service-account FCM illisible par $SERVICE_USER ($CREDENTIALS_PATH)"
fi
MQTT_NOTIF_CONFIG="$CONFIG_PATH" "$VENV_DIR/bin/python" -c \
    "from mqtt_notif.config import load_store_from_env; load_store_from_env()" \
    || fail "gate d'activation : YAML de règles invalide ($CONFIG_PATH) — le chargeur du démon le rejette"

# --- 8. Activation -------------------------------------------------------------

systemctl daemon-reload
# reset-failed tolérant (l'unité peut ne pas exister encore) : une unité
# restée failed d'une tentative précédente ferait échouer le start avec
# « start request repeated too quickly ».
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
if [ "$was_active" -eq 1 ]; then
    # Réinstallation : le service (arrêté avant remplacement du code) est
    # relancé sur le nouveau code ; enable est idempotent.
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"
else
    systemctl enable --now "$SERVICE_NAME"
fi

echo "installation terminée : systemctl status $SERVICE_NAME"
echo "chemins lus dans l'unité installée (adaptez l'unité si besoin) :"
echo "  MQTT_NOTIF_CONFIG=$CONFIG_PATH"
echo "  MQTT_NOTIF_FIREBASE_CREDENTIALS=$CREDENTIALS_PATH"
echo "logs : journalctl -u $SERVICE_NAME"
