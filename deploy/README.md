# Déploiement de mqtt-notif — service systemd résident

mqtt-notif tourne en service systemd résident sur la machine dédiée
(Raspberry Pi OS Trixie, Debian 13) : démarrage au boot, relance sur
défaillance, logs dans journald. Le démon n'acquiert aucune capacité
nouvelle — c'est le même processus que celui testé hors systemd.

## Prérequis

- Raspberry Pi OS avec systemd (journald actif).
- `python3.13` sur PATH (livré par Trixie ; sinon `requires-python >= 3.13`
  de `pyproject.toml` n'est pas satisfait).
- Sans `uv` : le paquet **`python3.13-venv`** (ensurepip) — sans lui,
  `python3.13 -m venv` échoue sur une machine vierge. Le script le vérifie
  et échoue explicitement. Avec `uv`, ce paquet n'est pas requis.
- Accès root (`sudo`).
- Le YAML de règles et le service-account FCM **posés avant l'activation**
  aux chemins de l'unité (exemples ci-dessous), ou l'unité du dépôt adaptée
  à vos chemins réels avant de lancer le script : la gate d'activation
  vérifie leur existence, leur lisibilité par le service et la validité du
  YAML, et échoue explicitement sinon — elle n'affiche jamais
  « installation terminée » sur un service défectueux.

## Installation (scriptée)

Depuis la racine du dépôt copié sur la machine :

```sh
sudo ./deploy/install.sh
```

Le script fait, dans l'ordre, de façon vérifiable à chaque étape :

1. Vérifie les prérequis (`systemctl`, `python3.13`, outils utilisés,
   ensurepip pour la branche `pip`, dépôt complet) et échoue explicitement
   plutôt que de deviner.
2. Détecte si le service tourne déjà (réinstallation) et **l'arrête avant
   de remplacer le code** — sinon le processus actif continuerait de tourner
   sur des inodes supprimés, et un échec ultérieur (venv, pip, gate)
   laisserait la machine sans code fonctionnel ; il est relancé à
   l'activation.
3. Crée l'utilisateur dédié non-root `mqtt-notif` (système, sans login)
   s'il n'existe pas déjà.
4. Archive le dépôt (**tar natif, archive temporaire, statut de chaque tar
   vérifié — jamais un pipeline dont seul le dernier élément décide**),
   **vide `/opt/mqtt-notif` avant extraction** (réinstallation sans reliquat
   de fichiers obsolètes), hors `.venv`, `.git`, `.pytest_cache`,
   `__pycache__`, `*.egg-info`, `.mypy_cache`, `.ruff_cache`, `dist`,
   `_bmad`, `_bmad-output`, `.agents`.
5. Crée le venv Python 3.13 `/opt/mqtt-notif/.venv` et installe les
   dépendances (`uv` s'il est présent — y compris dans le `~/.local/bin` de
   l'appelant, invisible sous sudo autrement — sinon `pip`), puis vérifie
   que le console script `mqtt-notif` (entrée pyproject
   `mqtt_notif.main:main`) est installé.
6. Copie `deploy/mqtt-notif.service` dans `/etc/systemd/system/` avec mode
   fixé (`install -m 644`). **Réinstallation** : une unité installée qui
   diffère du dépôt (adaptée par l'opérateur) n'est **pas écrasée** — la
   nouvelle version est posée en `mqtt-notif.service.new` à côté, avec un
   avertissement ; à vous de comparer et fusionner.
7. Lit les deux chemins dans l'unité installée et prépare **ces**
   répertoires-là (`mkdir -p`, attribués au service) — jamais des
   répertoires d'exemple codés en dur : si l'unité a été adaptée, ce sont
   vos répertoires qui sont préparés.
8. **Gate d'activation** : le YAML de règles et le service-account FCM
   doivent exister, être lisibles par l'utilisateur du service (la gate
   tourne en root : un fichier root-only ne passerait pas), et le YAML doit
   passer le chargeur du démon — sinon échec explicite.
9. `systemctl daemon-reload`, `systemctl reset-failed` (tolérant : une
   unité restée `failed` d'une tentative précédente ferait échouer le start
   avec « start request repeated too quickly »), puis
   `systemctl enable --now mqtt-notif` (réinstallation : le service arrêté
   à l'étape 2 est relancé sur le nouveau code).

## Chemins passés par variables

L'unité définit :

```ini
Environment=MQTT_NOTIF_CONFIG=/etc/mqtt-notif/mqtt-notif.yaml
Environment=MQTT_NOTIF_FIREBASE_CREDENTIALS=/var/lib/mqtt-notif/service-account.json
```

Ce sont des **exemples** pour la machine dédiée : adaptez l'unité (copiée
dans `/etc/systemd/system/`) à vos chemins réels, puis
`systemctl daemon-reload && systemctl restart mqtt-notif`.

La promesse « aucun chemin codé en dur » vaut pour ces **deux chemins de
configuration** (AC-3.1.3) : ni le démon ni l'unité ne les codent en dur.
En revanche, `ExecStart` et `Documentation=` pointent le répertoire
d'application installé (`/opt/mqtt-notif`, posé par `install.sh`) — leur
cohérence avec le script et l'entrée pyproject est épinglée par
`tests/test_deploy.py`.

Le démon lit ces variables au démarrage ; il ne connaît aucun chemin par
lui-même.

### Rechargement à chaud (polling du YAML)

Le démon **sonde le mtime du YAML toutes les secondes** (polling, pas
d'inotify ni de signal — aucun `ExecReload` n'est nécessaire). Un YAML
modifié est revalidé en entier puis remplacé en une opération atomique ;
le changement prend effet à l'événement MQTT suivant. Un YAML invalide est
rejeté (erreur journalisée) et l'ancienne configuration reste active.
Ajouter une règle ou tourner le device token passe donc par la simple
édition du YAML, sans redémarrage. Un changement de
`broker.host`/`broker.port` est refusé à chaud : il exige un redémarrage
du service.

## Vérifications après installation

```sh
systemctl status mqtt-notif                  # active (running)
systemctl show -p User --value mqtt-notif    # utilisateur dédié : mqtt-notif
journalctl -u mqtt-notif -f                  # connexion au broker journalisée
```

Vérifications terrain (à faire une fois sur la machine dédiée — la story
ne les automatise pas) :

- **Démarrage au boot** (AC-3.1.1) : `sudo reboot` → le service démarre
  seul, sans intervention.
- **Redémarrage sur défaillance** (AC-3.1.2) : tuer le processus —
  **avec un garde sur MainPID** (un service inactif expose `MainPID=0`, et
  `kill -9 0` signalerait tout le groupe de processus du shell) :

  ```sh
  pid=$(systemctl show -p MainPID --value mqtt-notif)
  if [ "$pid" != "0" ] && [ -n "$pid" ]; then sudo kill -9 "$pid"; else echo "service inactif"; fi
  ```

  systemd relance le processus (`Restart=on-failure`, `RestartSec=5`) ; les
  fenêtres de debounce repartent à zéro (QoS 0, aucun rejeu — comportement
  assumé).
- **Arrêt propre** (AC-3.1.6) : `systemctl stop mqtt-notif` → SIGTERM →
  déconnexion propre du broker, code 0, état `inactive` (pas `failed`).

## Échec de démarrage visible dans journald

Si le YAML est absent/invalide au démarrage, le démon échoue vite (exit 1).
Les limites de démarrage étant **actives** (voir ci-dessous), l'unité
atteint l'état `failed` après le nombre de démarrages borné, l'erreur est
lisible dans `journalctl -u mqtt-notif` — pas de boucle de redémarrage
silencieuse indéfinie (AC-3.1.4).

Un service-account FCM manquant, en revanche, **ne fait pas échouer le
démarrage** : l'initialisation FCM est paresseuse (premier envoi) — le
service reste actif et l'erreur est journalisée à chaque envoi. C'est la
gate d'activation de `install.sh` qui couvre ce cas à l'installation, pas
le démarrage.

Reprise après correction du fichier :

```sh
systemctl reset-failed mqtt-notif   # seulement si l'unité est restée failed
systemctl start mqtt-notif
```

Si c'est la **gate d'activation** du script qui a échoué (fichiers
absents), le service n'a pas été activé : posez le YAML et le
service-account (ou adaptez les chemins `Environment=` de l'unité dans
`/etc/systemd/system/mqtt-notif.service`), puis :

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now mqtt-notif
```

## Réglages de déploiement

- **`StartLimitBurst=5` / `StartLimitIntervalSec=1min`** : **actifs dans
  l'unité**, calibrés avec `RestartSec=5` — cinq démarrages espacés de 5 s
  tiennent dans la fenêtre d'une minute, la limite déclenche réellement et
  systemd marque l'unité `failed`. Les défauts systemd (5 démarrages /
  10 s) ne déclencheraient jamais avec `RestartSec=5` : boucle de
  redémarrage infinie, jamais d'état `failed`. Ajustables au déploiement :
  après modification, `systemctl daemon-reload` puis
  `systemctl reset-failed mqtt-notif` pour repartir de zéro.
- **Permissions du service-account FCM** : le script se limite à rendre
  `/var/lib/mqtt-notif` accessible au service ; le durcissement fin
  (propriétaire strict, `chmod 600`) est à faire au déploiement réel, hors
  périmètre de cette story.
- **Pare-feu / réseau** : le broker Mosquitto du LAN doit être joignable
  depuis la machine ; tout réglage firewall lui est propre.
- Pas de durcissement avancé de l'unité (conteneurs, sandboxing…) :
  reporté au déploiement réel.

## Désinstallation complète

```sh
sudo systemctl disable --now mqtt-notif
sudo rm -f /etc/systemd/system/mqtt-notif.service \
  /etc/systemd/system/mqtt-notif.service.new && sudo systemctl daemon-reload
sudo userdel mqtt-notif && sudo rm -rf /opt/mqtt-notif
# Secrets : le YAML (device token) et le service-account FCM (clé privée)
sudo rm -rf /etc/mqtt-notif /var/lib/mqtt-notif
```
