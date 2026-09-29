---
title: 'Story 3.1 — Service systemd résident'
type: 'feature'
created: '2026-09-28'
status: 'done'
route: 'dispatch'
review_loop_iteration: 1
baseline_commit: a74dac6
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Le démon ne tourne que lancé à la main : il ne démarre pas au boot, ne survit pas à une mort brutale, et rien ne documente son installation sur la machine dédiée.

**Approach:** Livrer l'enveloppe opérationnelle : unité systemd (`Restart=on-failure`, utilisateur dédié non-root, venv Python 3.13, chemins passés par `MQTT_NOTIF_CONFIG`/`MQTT_NOTIF_FIREBASE_CREDENTIALS`), procédure d'installation reproductible documentée (venv, dépendances, unité copiée, `enable --now`), échec de démarrage visible dans journald, arrêt propre SIGTERM → code 0.

## Boundaries & Constraints

**Always:**
- `Restart=on-failure` : un `kill -9` du processus entraîne son redémarrage automatique par systemd ; les fenêtres de debounce repartent à zéro (AC-2.2.8).
- Les chemins du YAML des règles et du service-account FCM passent par les variables d'environnement `MQTT_NOTIF_CONFIG` et `MQTT_NOTIF_FIREBASE_CREDENTIALS` (lecture au démarrage, AD-8) — aucun chemin codé en dur dans le démon ni dans l'unité.
- Utilisateur dédié non-root pour le service ; logs plats sur stdout confiés à journald (timestamps ISO 8601 UTC) — le démon n'écrit jamais dans un fichier de log propre.
- Échec de démarrage (YAML absent, service-account manquant) : le démon échoue vite, systemd le marque failed, l'erreur est lisible dans journald — pas de boucle de redémarrage silencieuse indéfinie ; `StartLimitBurst` est documenté comme réglage du déploiement.
- Arrêt propre : `systemctl stop` → SIGTERM → déconnexion propre du broker, code 0 — l'état systemd passe à inactive, pas failed.
- Installation reproductible : venv Python 3.13, dépendances installées, unité copiée, service activé — sans étape manuelle non documentée.

**Never:**
- Pas de durcissement avancé de l'unité ni de réglage fin des permissions du service-account (reportés au déploiement réel par la spine, section Deferred).
- Pas de conteneur, pas de base de données, pas d'API de gestion (NFR-4).
- Le démon n'acquiert aucune capacité système nouvelle : il reste le même processus que celui testé hors systemd.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Démarrage au boot | machine dédiée, unité installée et activée | service démarré sans intervention, connexion au broker journalisée | Vérification manuelle sur la Pi |
| Redémarrage sur défaillance | `kill -9` du processus | systemd le relance (`Restart=on-failure`), fenêtres de debounce réinitialisées | Vérification manuelle sur la Pi |
| Chemins par variables | inspection de l'unité et du démon | `MQTT_NOTIF_CONFIG`/`MQTT_NOTIF_FIREBASE_CREDENTIALS` ; aucun chemin codé en dur | Test statique automatisable |
| Échec de démarrage visible | YAML absent ou service-account manquant au boot | échec vite, état failed, erreur lisible dans journald | Vérification manuelle ; politique testée par les stories 1.x |
| Logs dans journald | `journalctl -u mqtt-notif` | lignes du démon (connexions, rechargements, envois, erreurs) visibles | Test du comportement stdout déjà couvert |
| Arrêt propre | `systemctl stop mqtt-notif` | SIGTERM → déconnexion propre, code 0, état inactive (pas failed) | Test SIGTERM → exit 0 déjà automatisé |
| Installation reproductible | machine dédiée vierge | venv créé, dépendances installées, unité copiée, service activé | Procédure documentée, vérifiable par relecture |

</frozen-after-approval>

## Code Map

- `deploy/mqtt-notif.service` -- unité systemd : `Restart=on-failure`, `RestartSec=5`, `StartLimitBurst=5` et `StartLimitIntervalSec=1min` **actifs** (calibrés avec `RestartSec` pour que la limite déclenche réellement — voir Implementation Notes), documentés comme ajustables au déploiement ; utilisateur dédié non-root (`User=`/`Group=`) ; `Environment=` pour les deux variables (exemples de la machine dédiée, marqués à adapter) ; `ExecStart` = console script du venv ; `Type=simple`, stdout/stderr → journald ; `[Install] WantedBy=multi-user.target`.
- `deploy/install.sh` -- installation reproductible : prérequis à échec explicite (root, systemctl, python3.13, **ensurepip pour `python3.13 -m venv` si la branche pip est prise**) ; utilisateur dédié si absent ; copie du dépôt **sans statut masqué (pas de pipeline dont seul le dernier tar décide)** avec excludes étendus (`__pycache__`, `*.egg-info`, `.mypy_cache`, `.ruff_cache`, `dist`) ; **réinstallation propre : `APP_DIR` vidé avant extraction, service redémarré s'il tourne déjà, l'unité installée adaptée par l'opérateur n'est pas écrasée sans avertissement** ; venv Python 3.13 + dépendances ; **copie de l'unité avec mode fixé (`install -m 644`)** ; **gate avant activation : YAML et service-account présents aux chemins attendus, sinon échec explicite** ; `daemon-reload`, `enable --now`.
- `deploy/README.md` -- procédure documentée : prérequis (dont le paquet `python3.13-venv` pour la branche pip), étapes du script, chemins par variables (**la promesse « aucun chemin codé en dur » est bornée aux deux chemins de configuration lus par le démon**), **mécanisme du rechargement à chaud explicité (polling du YAML)**, vérifications terrain (**`kill -9` précédé d'un garde sur MainPID**), échec visible + reprise, réglages de déploiement (`StartLimitBurst`/`StartLimitIntervalSec` **actifs dans l'unité, ajustables**), **désinstallation complète incluant `/etc/mqtt-notif` et `/var/lib/mqtt-notif` (secrets)**.
- `mqtt_notif/main.py` -- inchangé attendu (échec rapide déjà en place : config absente → exit 1 ; SIGTERM → exit 0) ; l'unité doit refléter les variables existantes.
- `tests/test_deploy.py` -- validations statiques étendues : `Restart=on-failure`, `RestartSec`, `StartLimitBurst`/`StartLimitIntervalSec` **actifs**, les deux `Environment=`, exemples de chemins uniquement sur lignes `Environment=`, `WantedBy=multi-user.target`, **cross-pin `User`/`Group` de l'unité ↔ `SERVICE_USER` de install.sh**, cohérence `ExecStart` **et `Documentation=`** avec `APP_DIR` et l'entrée pyproject, **`sh -n` exécuté sur install.sh**, **parcours `os.walk` de tout le paquet** pour les chemins codés en dur, **regex `APP_DIR` stricte**, étapes du script (venv, `daemon-reload`, `enable --now`, gate d'activation, échecs explicites).

## Tasks & Acceptance

**Execution:**
- [x] `deploy/mqtt-notif.service` -- unité conforme, limites de démarrage actives et calibrées -- AC-3.1.1, AC-3.1.2, AC-3.1.3, AC-3.1.4, AC-3.1.5.
- [x] `deploy/install.sh` -- installation reproductible scriptée, gate d'activation, réinstallation propre -- AC-3.1.7.
- [x] `deploy/README.md` -- procédure documentée, réglages explicités, désinstallation complète -- AC-3.1.4, AC-3.1.7.
- [x] `tests/test_deploy.py` -- validations statiques étendues (cross-pins, `sh -n`, `os.walk`, `WantedBy`) -- AC-3.1.3, cohérence pyproject/ExecStart.

**Acceptance Criteria:**
- Given l'unité installée et activée, when la machine démarre ou le processus meurt, then le service démarre/redémarre sans intervention (vérification terrain).
- Given l'inspection de l'unité et du code, then les deux chemins passent par variables d'environnement, aucun chemin codé en dur.
- Given un YAML absent au démarrage persistant, then le démon échoue vite et, après le nombre de démarrages borné par les limites actives, l'unité atteint l'état failed avec l'erreur lisible dans journald — pas de boucle infinie.
- Given une machine dédiée vierge, when on suit la procédure documentée, then venv, dépendances, unité, activation — sans étape manuelle non documentée ; l'installation échoue explicitement si le YAML ou le service-account manque au moment de l'activation.
- Given une réinstallation (re-run du script) sur une installation existante, then le code est remplacé sans reliquat, le service tourant déjà est redémarré, et une unité adaptée par l'opérateur n'est pas écrasée silencieusement.

## Implementation Notes

Décisions internes (non perceptibles pour l'utilisateur) :
- L'unité utilise `Type=simple` (le démon tourne au premier plan, stdout/stderr → journald par défaut) ; `RestartSec=5`.
- **`StartLimitBurst=5` / `StartLimitIntervalSec=1min` sont actifs** : avec `RestartSec=5`, cinq démarrages tiennent dans la fenêtre d'une minute, la start limit déclenche et systemd marque l'unité `failed`. État connu-évité : les défauts systemd (5 démarrages / 10 s) ne déclenchent jamais avec `RestartSec=5` — boucle de redémarrage infinie, l'unité n'atteint jamais `failed`. Les valeurs restent documentées comme ajustables (l'AC exige la visibilité, pas une valeur figée).
- Les chemins dans `Environment=` sont des exemples de la machine dédiée (/etc/mqtt-notif/, /var/lib/mqtt-notif/) clairement marqués à adapter ; l'absence de chemin codé en dur concerne les deux chemins de configuration lus par le démon ; `ExecStart` (et `Documentation=`) pointent le répertoire d'application installé, cross-pinés par les tests.
- Le script d'installation cible Raspberry Pi OS (systemd présent, python3.13, uv ou venv) ; il échoue explicitement sur prérequis manquant plutôt que de deviner. Branche pip : `python3.13 -m venv` exige ensurepip (paquet `python3.13-venv` sur Debian/Trixie) — prérequis vérifié et documenté ; branche `uv` non concernée.
- Copie du code : tar natif (pas de dépendance rsync), **archive temporaire ou vérification explicite du statut de chaque tar** — jamais un pipeline dont seul le dernier élément décide (sh POSIX n'a pas `pipefail`).
- Tout ce qui exige un vrai systemd (boot, kill -9, journalctl) reste une vérification terrain documentée dans le README — la story ne prétend pas les automatiser.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, y compris `sh -n` sur install.sh, les cross-pins et les limites actives.
- `systemd-analyze verify deploy/mqtt-notif.service` (disponible sur la machine de développement) -- expected: aucune erreur de syntaxe d'unité.

**Manual checks (if no CLI):**
- AC-3.1.1, AC-3.1.2, AC-3.1.4, AC-3.1.5, AC-3.1.6 sur la machine dédiée : installation suivant le README, `enable --now`, observation journald, `kill -9` + redémarrage, `systemctl stop` → inactive.

## Spec Change Log

### 2026-09-29 — boucle de revue 1 (bad_spec), triage de la première implémentation
- **Finding déclencheur (high)** : `RestartSec=5` avec `StartLimitBurst`/`StartLimitIntervalSec` commentés (défauts systemd 5/10 s) → la start limit ne déclenche jamais ; boucle de redémarrage infinie, l'unité n'atteint jamais `failed` — contredit la contrainte gelée « pas de boucle de redémarrage silencieuse indéfinie » et la ligne « état failed » de la matrice I/O. Les Implementation Notes précédentes (« présents mais commentés, l'AC exige la visibilité, pas une valeur figée ») portaient la racine du défaut.
- **Autres findings vérifiés regroupés dans le même amendement** : activation sans gate (exit 0 et « installation terminée » alors que le service crash-loop, README annonçant un « prérequis avant l'activation » non implémenté) ; réinstallation (reliquat de fichiers obsolètes, pas de redémarrage du service actif, unité adaptée écrasée par `cp`) ; pipeline tar au statut masqué (copie partielle silencieuse — reproduit empiriquement, exit 0 du pipeline avec fichier illisible) ; désinstallation laissant `/etc/mqtt-notif` et `/var/lib/mqtt-notif` (service-account FCM, YAML) sur disque ; prérequis `python3.13-venv`/ensurepip absent de la gate et du README ; vérification textuelle incomplète (pas de cross-pin `User`/`Group` ↔ `SERVICE_USER`, pas d'assertion `WantedBy`, pas de `sh -n` exécuté, `os.listdir` au lieu d'`os.walk`, regex `APP_DIR` tolérant un préfixe, `Documentation=` non vérifié) ; imprécisions README (portée de « aucun chemin codé en dur », mécanisme du rechargement à chaud non explicité, `kill -9 $(MainPID)` sans garde — MainPID=0 signale le groupe de processus, « status montre l'utilisateur » inexact, références AC incohérentes, `cp` sans mode fixé, excludes tar incomplets).
- **Amendé (sections non gelées)** : Code Map, Tasks & Acceptance, Implementation Notes, Verification — limites de démarrage actives et calibrées, gate d'activation, sémantique de réinstallation, copie robuste sans statut masqué, désinstallation complète, prérequis ensurepip, tests étendus (cross-pins, `sh -n`, `os.walk`, `WantedBy`, regex stricte).
- **État connu-évité** : limites commentées → boucle infinie sans état failed ; activation inconditionnelle → succès affiché sur service crash-loop ; pipeline tar → copie partielle silencieuse avec exit 0.
- **KEEP (à préserver à la re-dérivation)** : `Environment=` par variables avec exemples marqués à adapter ; `Type=simple` et stdout/stderr → journald, aucun fichier de log propre ; `Restart=on-failure` + `RestartSec=5` ; style `fail()` du script (échec explicite plutôt que deviner) ; approche tar native sans dépendance rsync ; motif de cross-check `ExecStart`/pyproject/`APP_DIR` de test_deploy.py ; helpers `read`/`active_lines`/`unit_assignments` ; structure du README (prérequis → installation scriptée → variables → vérifications terrain → échec visible → réglages différés → désinstallation) ; docstring des tests rappelant que le vrai systemd reste une vérification terrain.

## Review Triage Log

| # | Couche | Constat | Verdict | Preuve / routage |
|---|--------|---------|---------|------------------|
| 1 | blind | Activation sans gate : `enable --now` inconditionnel, « installation terminée » même si le démon crash-loop | medium | Vérifié : `Type=simple` rend la main avant l'échec du démon ; le README annonce un « prérequis avant l'activation » que le script n'implémente pas. Groupe E-B (bad_spec). |
| 2 | blind | `chown -R` du service user sur `/etc/mqtt-notif` et `/var/lib/mqtt-notif` | low, hors périmètre — rejet | Réel, mais le Never gelé reporte explicitement le réglage fin des permissions au déploiement réel. |
| 3 | blind | ensurepip/`python3.13-venv` non vérifié | low | Branche pip échoue sur OS vierge sans le paquet (branche uv épargnée) ; échec visible mais remède non documenté. Groupe E-G (bad_spec). |
| 4 | blind | Installation hors-ligne non gérée | false | L'installation exige le réseau par construction (pip/uv depuis PyPI) ; rien ne promet l'offline. |
| 5 | blind | Réinstallation : fichiers obsolètes persistants, pas de redémarrage | medium | Vérifié : le tar fusionne sans nettoyer `APP_DIR` ; `enable --now` ne redémarre pas un service déjà actif. Groupe E-C (bad_spec). |
| 6 | blind | README « aucun chemin codé en dur » alors que l'unité fige `/opt/mqtt-notif` dans `ExecStart`/`Documentation=` | low | La promesse ne vaut que pour les deux chemins de configuration (AC : « les deux chemins ») ; `ExecStart` est déjà cross-piné au script. Reformulation directe du README. Groupe E-G. |
| 7 | blind | Mécanisme du rechargement à chaud non documenté | low | Rechargement par polling (config.py:308) ; le README ne dit ni poll ni délai ; `ExecReload` inutile en polling. Groupe E-G. |
| 8 | blind | `cp` nu pour l'unité ; excludes tar incomplets (`__pycache__`, `*.egg-info`, `.mypy_cache`, `dist`) | low | Vérifié : seuls `.venv/.git/.pytest_cache/_bmad*/.agents` sont exclus ; le mode de l'unité est hérité du checkout. Groupe E-G. |
| 9 | blind | `os.listdir` ignore les sous-paquets dans le test des chemins codés en dur | low | Paquet plat aujourd'hui, gap latent ; correction directe (`os.walk`). Groupe E-F. |
| 10 | blind | Aucune exécution de install.sh, pas même `sh -n` | medium | Seule la formulation du script est testée ; `sh -n` passe aujourd'hui mais rien ne l'épingle ; une régression de flux de contrôle passerait verte. Groupe E-F (bad_spec). |
| 11 | blind | Durcissement absent (`NoNewPrivileges`, `ProtectSystem`, `WatchdogSec`, `WorkingDirectory`) | low, hors périmètre — rejet | Never gelé : « pas de durcissement avancé de l'unité », « aucune capacité système nouvelle » (Watchdog exige sd_notify). |
| 12 | blind | Références AC incohérentes dans les docstrings ; « status montre l'utilisateur » inexact | low | `systemctl status` n'affiche pas `User=` par défaut (`systemctl show -p User` si) ; alignement direct. Groupe E-G. |
| 13 | edge | Start limit jamais atteinte : `RestartSec=5` vs défauts 5 démarrages/10 s | high | Vérifié : les redémarrages comptent dans la start limit ; espacés de 5 s, jamais 5 sur 10 s → boucle infinie, l'unité n'atteint jamais `failed` ; contredit l'Always gelé et la matrice « état failed ». Groupe E-A (bad_spec). |
| 14 | edge | Pipeline tar : statut du pipeline = statut du dernier tar | medium | Reproduit empiriquement : fichier illisible dans la source → premier tar échoue, pipeline exit 0, copie partielle installée (sh POSIX sans pipefail). Groupe E-E (bad_spec). |
| 15 | edge | Utilisateur `mqtt-notif` existant sans groupe → `chown` aborte | low — rejet | État non démontré (l'AC cible une machine vierge) ; l'échec est de toute façon bruyant sous `set -e` ; le fix ajouterait une garde pour un état non atteint. |
| 16 | edge | Réinstallation : `cp` écrase l'unité adaptée par l'opérateur | medium | Vérifié : le README dit d'adapter la copie installée dans `/etc/systemd/system/`, le script la recouvre au re-run. Groupe E-C (bad_spec). |
| 17 | edge | Pas de `TimeoutStopSec` | low — rejet | SIGTERM → déconnexion propre → exit 0 est testé (tests/test_main.py:663) ; le hang sur SIGTERM n'est pas démontré. |
| 18 | edge | `kill -9 $(systemctl show -p MainPID …)` avec MainPID=0 | low | Vérifié : service inactif → `kill -9 0` signale tout le groupe de processus du shell sous sudo ; garde direct dans le README. Groupe E-G. |
| 19 | edge | Désinstallation laisse `/etc/mqtt-notif` et `/var/lib/mqtt-notif` | medium | Vérifié : la section désinstallation retire unité, utilisateur et `/opt`, jamais le YAML ni le service-account (clé privée FCM). Groupe E-D (bad_spec). |
| 20 | edge | Regex `^APP_DIR=?…` tolère un préfixe (`APP_DIRECTORY=`) | low | Vérifié : `APP_DIRECTORY=/x` matche et capturerait un mauvais chemin ; resserrement direct de la regex. Groupe E-F. |
| 21 | edge | Claim « échec vite → état failed » jamais atteint | carried | Même constat et même verdict que #13 (dupliquat). |
| 22 | verif | `User`/`Group` de l'unité ↔ `SERVICE_USER` du script sans cross-check | medium (pré-vérifié) | `User=notif` passe les 14 tests ; au déploiement les permissions du chown ne colleraient plus. Groupe E-F (bad_spec). |
| 23 | verif | `WantedBy=multi-user.target` non asserté | medium (pré-vérifié) | La seule section qui porte le démarrage au boot, zéro couverture ; `enable` échouerait ou le boot ne démarre pas. Groupe E-F (bad_spec). |
| 24 | verif | Script jamais exécuté, pas de `sh -n` | carried | Même constat que #10 (dupliquat, pré-vérifié). |
| 25 | verif | `Documentation=` duplique `APP_DIR` sans vérification | low (pré-vérifié) | Dérive silencieuse possible si `APP_DIR` change ; cross-check direct dans le test existant. Groupe E-F. |
| 26 | verif | `enable --now` inconditionnel vs « prérequis avant l'activation » du README | carried | Même constat que #1 (dupliquat). |

**Groupes et routage** (boucle de revue 1) : E-A (high, #13/#21), E-B (medium, #1/#26), E-C (medium, #5/#16), E-D (medium, #19), E-E (medium, #14), E-F (medium, #10/#22/#23/#24 + low #9/#20/#25), E-G (low, #3/#6/#7/#8/#12/#18) — tous bad_spec, racine dans les sections non gelées (Implementation Notes, Code Map, README) → loopback : KEEP extrait, code révoqué, spec amendée ci-dessus, re-dérivation via step-03. Rejets : #2, #4, #11, #15, #17. Aucun report (defer) : le seul constat maybe-false potentiel (#14) a été tranché empiriquement.

### Boucle de revue 2 (après re-dérivation)

| # | Couche | Constat | Verdict | Preuve / routage |
|---|--------|---------|---------|-------------------|
| 27 | blind | `rm -rf "$APP_DIR"` pendant que le service tourne : inodes supprimés, aucun retour arrière si une étape ultérieure échoue, fenêtre où un restart automatique brûle la start limit | medium | Vérifié : le script ne stoppe pas le service avant le remplacement du code et n'atteint `restart` qu'à la fin ; tout exit anormal du démon dans la fenêtre relance un binaire disparu. Groupe R2-P1 (patch). |
| 28 | blind | `mkdir`/`chown` codent en dur les répertoires d'exemple alors que la gate lit l'unité installée | medium | Vérifié : flux documenté « adaptez l'unité » → le script prépare/chowne les mauvais répertoires et le service ne peut pas lire sa config réelle. Groupe R2-P3 (patch). |
| 29 | blind | Gate existence-only : un YAML invalide passe et « installation terminée » s'affiche sur un crash-loop — le README promet que jamais | medium | Vérifié : la gate ne teste que `[ -f ]` ; la promesse README (« n'affiche jamais… sur un service qui crash-loop ») n'est tenue que pour l'absence de fichier. Groupe R2-P2 (patch). |
| 30 | blind | sed fragile : `Environment="…"` quotée (syntaxe systemd valide) casse la gate ; `EnvironmentFile=` rejeté ; duplicates silencieux | low | Vérifié : la valeur garderait un `"` final. Forme quotée acceptée (patch R2-P2) ; `EnvironmentFile=` hors flux documenté. |
| 31 | blind | Assertions satisfaites par les commentaires, pas le comportement ; aucune exécution au-delà de `sh -n` | low | Serrer les assertions citées vers des lignes comportementales (patch R2-P6) ; un harnais d'exécution complet (shims PATH) dépasse le périmètre tests statiques + `sh -n`, l'exécution réelle reste terrain — rejet pour cette part. |
| 32 | blind | Pas de `PYTHONUNBUFFERED=1` → logs retardés dans le tampon | false | `log_line` (mqtt_notif/__init__.py:10) imprime avec `flush=True` sur chaque ligne — jamais de blocage en tampon. |
| 33 | blind | Pas de pinage des dépendances (`uv.lock` inutilisé) | low — rejet | L'AC « installation reproductible » vise la procédure, pas le pinage de versions ; le fix (migration `uv sync` + stratégie pip) dépasse la correction directe ; nuisance marginale pour la Pi dédiée. |
| 34 | blind | `chown -R` du répertoire de règles par le daemon (auto-réécriture possible avec le hot-reload) | carried | Identique à la ligne 2 : le code y lit encore comme la ligne le décrit ; verdict maintenu low hors-périmètre (Never gelé, permissions reportées au déploiement) ; jamais re-patché ni reporté. |
| 35 | blind | Désinstallation : `.new` jamais retiré ; pas de `reset-failed` avant `userdel` ; pas de `systemd-analyze` à l'install | low | `.new` : correction directe README (R2-P5) ; `userdel` ne dépend pas de l'état failed — faux pour cette part ; `systemd-analyze verify` est une vérification de développement (spec Verification), pas d'installation — rejet. |
| 36 | blind | Assert « leurre » constante-vraie sur un littéral | low — rejet | L'assertion épingle volontairement l'ancrage de la regex elle-même (anti-`APP_DIRECTORY`) ; aucun mal, aucune correction requise. |
| 37 | blind | `unit_assignments` écrase les clés dupliquées | low | Latent : un second `ExecStart=`/`Restart=` serait masqué par `.get()`. Assert d'unicité des clés sémantiques — patch R2-P6. |
| 38 | blind | Prérequis sans `command -v` pour tar/sed/cmp/install/mktemp/useradd | low | Vérifié : échec opaque en cours de route (après le `rm -rf`) au lieu de l'échec explicite que le script promet. Petite boucle de vérifs — patch R2-P7. |
| 39 | edge | YAML/service-account présents mais illisibles par le service : gate verte quand même | medium | Vérifié : `[ -f ]` s'exécute en root ; un fichier root:600 passe la gate puis le démon crash-loop (YAML) ou perd les envois (credentials). Groupe R2-P2 (patch). |
| 40 | edge | `Environment=` quotée → gate échoue à tort | carried | Dupliquat de #30, même racine. |
| 41 | edge | uv dans `~/.local/bin` invisible sous sudo (secure_path) → branche pip silencieuse | low | Plausible sur une Pi (installeur curl uv). Chercher aussi dans le HOME de `SUDO_USER` — patch R2-P4. |
| 42 | edge | Aucun retour arrière après `rm -rf` | carried | Dupliquat de #27, même racine. |
| 43 | edge | `enable --now`/`restart` sur unité failed → « start request repeated too quickly », installation avortée sans indice | medium | Scénario réel de réinstallation après échec (gate, crash pendant l'installation). `systemctl reset-failed` avant activation — patch R2-P1. |
| 44 | edge | YAML invalide passe la gate | carried | Dupliquat de #29, même racine. |
| 45 | edge | Claim : « service-account manquant → exit 1 vite, état failed » | medium | Vérifié : init firebase paresseuse au premier envoi (notifier.py:263-280), `main()` sort en erreur uniquement sur `ConfigError` (main.py:223) — le service reste actif, les erreurs sont journalisées par envoi. Le README et le commentaire de gate affirment à tort le fail-fast : correction documentaire (R2-P5). Le fail-fast démon lui-même est préexistant (story 1.x) → report (defer). |
| 46 | edge | `str.index("\n")` lève ValueError si la ligne tar est la dernière sans saut de ligne | low | Latent dans `test_install_script_copy_without_hidden_status` ; `find` avec repli — patch R2-P6. |
| 47 | verif | Propriétés du compte (`--system`, `--shell /usr/sbin/nologin`) non épinglées | medium (pré-vérifié) | Supprimer les flags : les 22 tests restent verts — la posture « sans login » régresse sans détection. Assertion d'épinglage — patch R2-P6. |
| 48 | verif | `chown`/`mkdir` des répertoires de config non épinglés — leur omission défait la gate silencieusement | medium (pré-vérifié) | Supprimer le chown : gate verte (fichiers existants), démon illisible → crash-loop après « installation terminée ». Assertion d'épinglage — patch R2-P6. |
| 49 | verif | README/gate : « exit 1 si service-account manquant » inexact | carried | Dupliquat de #45 (pré-vérifié par verification-gap). |
| 50 | verif | Fenêtre `rm -rf` : le restart automatique brûle la start limit et marque l'unité failed avant la fin du script | carried | Dupliquat de #27, même racine. |

**Groupes et routage** (boucle 2) : R2-P1 (medium, #27/#42/#43/#50 — réinstallation robuste : stop avant remplacement de code, `reset-failed` avant activation), R2-P2 (medium, #29/#30/#39/#44 — gate durcie : lisibilité par le service, validation du YAML, forme quotée acceptée), R2-P3 (medium, #28 — répertoires dérivés de l'unité installée), R2-P4 (low, #41 — uv sous sudo), R2-P5 (medium, #45/#49 + #35 — exactitude documentaire du mode d'échec credentials, `.new` retiré à la désinstallation), R2-P6 (medium, #47/#48 + low #31/#37/#46 — épinglages et hygiène des tests), R2-P7 (low, #38 — prérequis `command -v`). Tous **patch** — re-engagement du subagent d'implémentation. Rejets : #32, #33, #36, #31 (harnais d'exécution), #35 (`userdel`, `systemd-analyze` install). Carried : #34 (= ligne 2). Report (defer) : fail-fast du démon sur service-account absent/invalide au démarrage — comportement préexistant (story 1.x, init paresseuse) que la contrainte Always de cette spec suppose ; voir deferred-work.md.
