# Déploiement de mqtt-notif — service systemd résident

mqtt-notif tourne en service systemd résident sur la machine dédiée
(Raspberry Pi OS Trixie, Debian 13) : démarrage au boot, relance sur
défaillance, logs dans journald. Le démon n'acquiert aucune capacité
nouvelle — c'est le même processus que celui testé hors systemd.

## Prérequis

- Raspberry Pi OS avec systemd (journald actif).
- `python3.13` sur PATH (livré par Trixie ; sinon `requires-python >= 3.13`
  de `pyproject.toml` n'est pas satisfait).
- Accès root (`sudo`).
- `uv` si disponible (accélère l'installation), sinon `pip` du venv.
- Avant l'activation : le YAML de règles et le service-account FCM doivent
  exister, sinon le démon échoue vite et l'unité passe en état `failed`
  (voir ci-dessous).

## Installation (scriptée)

Depuis la racine du dépôt copié sur la machine :

```sh
sudo ./deploy/install.sh
```

Le script fait, dans l'ordre, de façon vérifiable à chaque étape :

1. Vérifie les prérequis (`systemctl`, `python3.13`, dépôt complet) et
   échoue explicitement plutôt que de deviner.
2. Crée l'utilisateur dédié non-root `mqtt-notif` (système, sans login)
   s'il n'existe pas déjà.
3. Copie le code dans `/opt/mqtt-notif` (hors `.venv`, `.git`, artefacts).
4. Crée le venv Python 3.13 `/opt/mqtt-notif/.venv` et installe les
   dépendances (`uv` s'il est présent, sinon `pip`), puis vérifie que le
   console script `mqtt-notif` (entrée pyproject `mqtt_notif.main:main`)
   est installé.
5. Crée `/etc/mqtt-notif` et `/var/lib/mqtt-notif`, attribués au service.
6. Copie `deploy/mqtt-notif.service` dans `/etc/systemd/system/`, fait
   `systemctl daemon-reload`, puis `systemctl enable --now mqtt-notif`.

## Chemins passés par variables (aucun chemin codé en dur)

L'unité définit :

```ini
Environment=MQTT_NOTIF_CONFIG=/etc/mqtt-notif/mqtt-notif.yaml
Environment=MQTT_NOTIF_FIREBASE_CREDENTIALS=/var/lib/mqtt-notif/service-account.json
```

Ce sont des **exemples** pour la machine dédiée : adaptez l'unité (copiée
dans `/etc/systemd/system/`) à vos chemins réels, puis
`systemctl daemon-reload && systemctl restart mqtt-notif`. Le démon lit ces
variables au démarrage ; il ne connaît aucun chemin par lui-même. La
rotation du device token ou l'ajout d'une règle passe par l'édition du YAML
(rechargement à chaud) ; un changement de `broker.host`/`broker.port` exige
un redémarrage du service.

## Vérifications après installation

```sh
systemctl status mqtt-notif          # active (running), utilisateur mqtt-notif
journalctl -u mqtt-notif -f          # connexion au broker journalisée
```

Vérifications terrain (à faire une fois sur la machine dédiée) :

- **Démarrage au boot** : `sudo reboot` → le service démarre seul.
- **Redémarrage sur défaillance** : `sudo kill -9 $(systemctl show -p MainPID --value mqtt-notif)` →
  systemd relance le processus (`Restart=on-failure`, `RestartSec=5`) ; les
  fenêtres de debounce repartent à zéro (QoS 0, aucun rejeu — comportement
  assumé).
- **Arrêt propre** : `systemctl stop mqtt-notif` → SIGTERM → déconnexion
  propre du broker, code 0, état `inactive` (pas `failed`).

## Échec de démarrage visible dans journald

Si le YAML est absent/invalide ou le service-account manquant au démarrage,
le démon échoue vite (exit 1) : systemd marque l'unité `failed`, l'erreur
est lisible dans `journalctl -u mqtt-notif`. Il n'y a pas de boucle de
redémarrage silencieuse indéfinie : systemd borne les tentatives et laisse
l'unité en échec. Reprise après correction du fichier :

```sh
systemctl reset-failed mqtt-notif   # seulement si l'unité est restée failed
systemctl start mqtt-notif
```

## Réglages laissés au déploiement

- **`StartLimitBurst` / `StartLimitIntervalSec`** (commentés dans l'unité) :
  plafond de redémarrages avant que systemd ne marque l'unité `failed`.
  Décommentez et ajustez selon la politique voulue (défauts systemd :
  5 démarrages / 10 s).
- **Permissions du service-account FCM** : le script se limite à rendre
  `/var/lib/mqtt-notif` accessible au service ; le durcissement fin
  (propriétaire strict, `chmod 600`, éventuel `ProtectSystem=`) est à faire
  au déploiement réel, hors périmètre de cette story.
- **Pare-feu / réseau** : le broker Mosquitto du LAN doit être joignable
  depuis la machine ; tout réglage firewall lui est propre.
- Pas de durcissement avancé de l'unité (conteneurs, sandboxing…) :
  reporté au déploiement réel.

## Désinstallation

```sh
sudo systemctl disable --now mqtt-notif
sudo rm /etc/systemd/system/mqtt-notif.service && sudo systemctl daemon-reload
sudo userdel mqtt-notif && sudo rm -rf /opt/mqtt-notif
```
