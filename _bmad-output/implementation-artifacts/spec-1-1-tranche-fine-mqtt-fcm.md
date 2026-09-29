---
title: 'Story 1.1 — Tranche fine : du message MQTT à la notification FCM'
type: 'feature'
created: '2026-09-28'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 4b825dc642cb6eb9a060e54bf8d69288fbee4904 # arbre vide git (aucun commit initial)
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Aucun code n'existe : un message MQTT publié sur le réseau domotique n'aboutit à rien sur le téléphone.

**Approach:** Livrer la tranche fine de bout en bout — connexion broker, souscription QoS 0 à un topic unique, envoi FCM *data-only* à message fixe (`title`, `short_message`) — dans un paquet `mqtt_notif` résilient : reconnexion automatique, erreurs d'envoi non fatales, arrêt propre sur SIGTERM, échec rapide au démarrage sur YAML invalide.

## Boundaries & Constraints

**Always:**
- Boucle d'événements asyncio unique ; appels bloquants firebase-admin exclusivement via `asyncio.to_thread` ; aucun thread applicatif.
- Chaque envoi FCM s'exécute comme tâche asyncio indépendante — le traitement des messages n'attend jamais la fin d'un envoi.
- Souscription QoS 0 ; une ligne de journal par connexion, déconnexion, reconnexion (timestamps ISO 8601 UTC, sortie stdout).
- Erreurs fatales uniquement avant le démarrage ; après le démarrage, aucune erreur ne peut arrêter le service.
- Chemins fournis par variables d'environnement : `MQTT_NOTIF_CONFIG` (YAML), `MQTT_NOTIF_FIREBASE_CREDENTIALS` (service-account, optionnel en 1.1).

**Never:**
- Pas de moteur de règles, pas de motifs `+`/`#`, pas de gabarits, pas de debounce (stories 1.2–2.2).
- Pas de TLS ni d'authentification broker ; pas de thread, pas de persistance, pas de rejeu.
- Aucune autre clé que `title` et `short_message` dans le message FCM (consolidation en 1.4).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Démarrage valide | YAML valide, broker joignable | Connexion, souscription QoS 0, log de connexion | N/A |
| Message sur le topic | Publication MQTT sur le topic souscrit | Tâche d'envoi FCM data-only (`title`, `short_message` fixes) créée | Échec d'envoi journalisé, service continue |
| Broker injoignable | Broker coupé puis revenu | Reconnexion auto avec backoff croissant, logs de déconnexion/reconnexion | Boucle de reconnexion, jamais d'arrêt |
| Token absent/refusé | `fcm_unregistered` ou token vide | Erreur explicite journalisée | Messages suivants traités normalement |
| Envoi lent | Mock FCM qui traîne | Messages suivants traités sans attendre | Aucun blocage de la boucle |
| SIGTERM | Démon en marche | Déconnexion propre, exit code 0 | N/A |
| YAML absent/corrompu/clé manquante | Démarrage | Erreur explicite, exit code non nul | Jamais de démarrage à moitié configuré |

</frozen-after-approval>

## Code Map

- `pyproject.toml` -- à créer : Python 3.13, aiomqtt 2.5.1, firebase-admin 7.7.0, PyYAML 6.0.3, pytest en dev.
- `mqtt_notif/__init__.py` -- à créer : paquet `mqtt_notif` (snake_case).
- `mqtt_notif/main.py` -- à créer : assemblage, boucle asyncio unique, gestion des signals (SIGTERM/SIGINT → exit 0).
- `mqtt_notif/config.py` -- à créer : lecture YAML minimale (`broker.host`, `broker.port`, `device_token`, `topic`), validation au démarrage, échec rapide.
- `mqtt_notif/mqtt.py` -- à créer : connexion, souscription QoS 0, reconnexion avec backoff croissant, itération asynchrone des messages.
- `mqtt_notif/notifier.py` -- à créer : frontière FCM — initialisation firebase-admin, envoi data-only à message fixe via `to_thread`, erreurs journalisées sans retry.
- `tests/` -- à créer : tests pytest avec broker MQTT et FCM mockés (aiomqtt mocké ou broker de test ; firebase-admin mocké).

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- créer le projet (Python 3.13, dépendances épinglées, pytest) -- socle exécutable.
- [x] `mqtt_notif/config.py` -- lire et valider le YAML minimal, échouer vite avec message explicite -- AC-1.1.7.
- [x] `mqtt_notif/notifier.py` -- envoyer le message FCM fixe dans `to_thread`, journaliser les erreurs sans crash -- AC-1.1.2, AC-1.1.4.
- [x] `mqtt_notif/mqtt.py` -- connexion, souscription QoS 0, reconnexion à backoff croissant, logs de cycle de vie -- AC-1.1.1, AC-1.1.3.
- [x] `mqtt_notif/main.py` -- assembler le pipeline, tâche d'envoi indépendante par message, SIGTERM → arrêt propre exit 0 -- AC-1.1.5, AC-1.1.6.
- [x] `tests/` -- couvrir la matrice I/O avec broker et FCM mockés -- chaque AC automatisable.

**Acceptance Criteria:**
- Given un YAML valide, when le démon démarre, then il se connecte, souscrit en QoS 0 et journalise sa connexion (ISO 8601 UTC).
- Given le démon connecté, when un message arrive sur le topic, then un envoi FCM data-only `title`/`short_message` est déclenché.
- Given le broker qui tombe puis revient, then reconnexion automatique à backoff croissant, jamais d'arrêt spontané.
- Given un SIGTERM, then déconnexion propre et exit code 0.

## Implementation Notes

Message fixe choisi (décision non perceptible pour l'utilisateur, à remplacer en 1.3) : `title: "mqtt-notif"`, `short_message: "Événement reçu"`. `MQTT_NOTIF_FIREBASE_CREDENTIALS` absent à l'envoi → erreur journalisée, service continue (identifiants réels en story 3.2).
- Correction post-diff : `Notifier._get_app` protégé par `threading.Lock` — deux envois concurrents via `to_thread` pouvaient appeler `firebase_admin.initialize_app` en double (ValueError en production, notification perdue).
- `DeprecationWarning` firebase-admin `Message.token` : adressage par device token maintenu (décision AD-6, jamais `fid`).
- Implémentation livrée par sous-agent, commit `5fbaffa` ; correction committée ensuite.

## Review Triage Log

- `main.py:run_pipeline` — envois en vol abandonnés après expiration du drain (non annulés, non journalisés) — medium — réel : perte silencieuse + warnings à la fermeture de boucle — patch.
- `main.py:serve` — exception du pipeline non relevée si `stopper` et `pipeline` finissent ensemble — low — réel (warning « Task exception was never retrieved ») — patch.
- `main.py:serve` — `RESTART_DELAY_S` contournable si le pipeline se termine sans stop — low — chemin inatteignable en 1.1 (`iter_messages` ne retourne que sur stop) — defer (à surveiller en story 2.1).
- `mqtt.py:message_payload` — code mort en production (payload non consommé, message fixe) — low — réel mais volontairement conservé : la story 1.2 construit l'objet événement topic+payload — defer.
- Pipeline — aucune ligne de journal à la réception d'un message — medium — réel, requis pour le relevé de latence SM-1 (AC-3.2.3) et le journald lisible (AC-3.2.4) — patch.
- Pipeline — concurrence d'envoi non bornée — low — AD-11 impose une tâche indépendante par envoi, échelle domestique, rafales traitées par debounce en story 2.2 — rejet (fix = complexité contraire au design d'intent).
- `notifier.py:_get_app` — ré-initialisation tentée à chaque envoi si identifiants invalides — low — comportement voulu : identifiants optionnels en 1.1, erreurs journalisées, service continue, branchement à chaud possible — rejet.
- `config.py:load_config` — `IsADirectoryError`/`PermissionError`/`UnicodeDecodeError` échappent comme traceback brut — medium — réel, contredit AC-1.1.7 (« YAML illisible → erreur explicite, exit non nul ») — patch (blind + edge-case).
- `config.py` — valeurs validées par `.strip()` mais stockées brutes — low — réel (host/token/topic avec espaces parasites) — patch (blind + edge-case).
- `config.py` — aucun contrôle de validité du topic → topic invalide = boucle backoff infinie silencieuse ; `+`/`#` interdits en 1.1 — medium — réel — patch (blind + edge-case).
- `notifier.py:_send` — branche token vide injoignable depuis la production — low — défense volontaire en frontière, documentée — rejet.
- `pyproject.toml`/`__init__.py` — version dupliquée, métadonnées manquantes — low — cosmétique, hors intent — rejet.
- Absence de README/exemple YAML/`__main__.py`/notes systemd — false comme défaut de 1.1 — les stories 3.1 et 3.2 du sprint en ont la charge explicite — rejet avec renvoi au plan.
- Chemins non testés : branche restart de `serve`, drain borné `SHUTDOWN_TIMEOUT_S`, stop-during-backoff, config OSError, port bool/65536 — medium — réel, une régression passerait la suite — patch (verification-gap, pré-vérifié).
- `tests:test_sigterm_subprocess_exit_code_zero` — polling select+readline fragile — low — fonctionne sur la cible Linux, garde-fou par deadline — rejet.
- Logs — langue mixte FR/EN — low — cosmétique, journald insensible — rejet.
- `log_line` via print plutôt que logging — low — AD-8 impose des logs plats stdout/journald, une ligne par événement — rejet.
- `main.py` — `add_signal_handler` NotImplementedError (Windows/thread non principal) — false en environnement cible (Linux, thread principal via `asyncio.run`, NFR-4 Raspberry Pi OS) — rejet.
- `main.py` — stop pendant le sommeil de restart → « stopped cleanly » manquant + stopper pending — low — réel, correctif direct — patch (edge-case).
- `notifier.py` — collision du nom d'app par défaut firebase-admin — false — aucun autre code n'initialise d'app dans ce processus mono-usage — rejet.
- `log_line` — BrokenPipeError si stdout fermé — maybe-false — stdout appartient à journald sous systemd, scénario non démontré ; si vrai, l'impact serait low (le journal est de toute façon cassé) — rejet.

## Verification

**Commands:**
- `uv sync --extra dev` -- expected: environnement créé sans erreur.
- `uv run pytest -q` -- expected: tous les tests passent, chaque AC automatisable couverte.

**Manual checks (if no CLI):**
- AC-1.1.2 (affichage réel sur le téléphone) : vérification manuelle une fois les identifiants réels fournis (story 3.2).
