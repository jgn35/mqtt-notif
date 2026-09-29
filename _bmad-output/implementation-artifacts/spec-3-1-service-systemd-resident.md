---
title: 'Story 3.1 — Service systemd résident'
type: 'feature'
created: '2026-09-28'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
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

- `deploy/mqtt-notif.service` -- à créer : unité systemd — `Restart=on-failure`, utilisateur dédié non-root, `Environment=` pour les deux variables (chemins d'exemple documentés), `ExecStart` pointant le venv, `RestartSec` raisonnable, `StartLimitBurst` commenté comme réglage du déploiement, logs stdout → journald par défaut.
- `deploy/install.sh` -- à créer : procédure d'installation reproductible scriptée (création utilisateur dédié si absent, venv Python 3.13, `uv sync`/`pip install`, copie de l'unité, `systemctl daemon-reload`, `enable --now`) — chaque étape vérifiable, aucune étape manuelle non documentée.
- `deploy/README.md` -- à créer : procédure d'installation documentée pas à pas (ce que fait le script, réglages de déploiement laissés : `StartLimitBurst`, permissions du service-account, firewall), reprise après échec visible dans journald.
- `mqtt_notif/main.py` -- inchangé attendu (échec rapide déjà en place : config absente → exit 1 ; SIGTERM → exit 0) ; l'unité doit refléter les variables existantes.
- `tests/test_deploy.py` -- à créer : validations statiques de l'unité (Restart=on-failure présent, Environment= avec les deux variables, aucun chemin codé en dur hors exemples commentés, utilisateur non-root défini, ExecStart cohérent avec l'entrée pyproject) et du script d'installation (référence au venv, daemon-reload, enable --now).

## Tasks & Acceptance

**Execution:**
- [ ] `deploy/mqtt-notif.service` -- unité systemd conforme -- AC-3.1.1, AC-3.1.2, AC-3.1.3, AC-3.1.5.
- [ ] `deploy/install.sh` -- installation reproductible scriptée -- AC-3.1.7.
- [ ] `deploy/README.md` -- procédure documentée, réglages de déploiement explicités (`StartLimitBurst`, permissions) -- AC-3.1.4, AC-3.1.7.
- [ ] `tests/test_deploy.py` -- validations statiques automatisables de l'unité et du script -- AC-3.1.3, cohérence pyproject/ExecStart.

**Acceptance Criteria:**
- Given l'unité installée et activée, when la machine démarre ou le processus meurt, then le service démarre/redémarre sans intervention (vérification terrain).
- Given l'inspection de l'unité et du code, then les deux chemins passent par variables d'environnement, aucun chemin codé en dur.
- Given un YAML absent au boot, then échec vite, état failed, erreur lisible dans journald (politique d'échec rapide héritée des stories 1.x, vérification terrain).
- Given une machine dédiée vierge, when on suit la procédure documentée, then venv, dépendances, unité, activation — sans étape manuelle non documentée.

## Implementation Notes

Décisions internes (non perceptibles pour l'utilisateur) :
- L'unité utilise `Type=simple` (le démon tourne au premier plan, stdout/stderr → journald par défaut) ; `RestartSec=5` borne la boucle de redémarrage ; `StartLimitBurst`/`StartLimitIntervalSec` présents mais commentés comme réglages de déploiement (l'AC exige la visibilité, pas une valeur figée).
- Les chemins dans `Environment=` sont des exemples de la machine dédiée (/etc/mqtt-notif/, /var/lib/mqtt-notif/) clairement marqués à adapter ; l'absence de chemin codé en dur concerne le démon et la sémantique de l'unité, pas les valeurs d'exemple commentées.
- Le script d'installation cible Raspberry Pi OS (systemd présent, python3.13, uv ou venv) ; il échoue explicitement sur prérequis manquant plutôt que de deviner.
- Tout ce qui exige un vrai systemd (boot, kill -9, journalctl) reste une vérification terrain documentée dans le README — la story ne prétend pas les automatiser.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, y compris les validations statiques de l'unité.
- `systemd-analyze verify deploy/mqtt-notif.service` (si disponible sur la machine) -- expected: aucune erreur de syntaxe d'unité.

**Manual checks (if no CLI):**
- AC-3.1.1, AC-3.1.2, AC-3.1.4, AC-3.1.5, AC-3.1.6 sur la machine dédiée : installation suivant le README, `enable --now`, observation journald, `kill -9` + redémarrage, `systemctl stop` → inactive.
