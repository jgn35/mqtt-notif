---
title: 'Story 2.1 — Rechargement à chaud avec swap atomique'
type: 'feature'
created: '2026-09-28'
status: 'in-progress'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: db294c4
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Toute modification du YAML (libellés, nouvelles règles, token) exige un redémarrage du démon ; un YAML cassé entre deux éditions casserait le service en marche.

**Approach:** Faire de la configuration un ConfigStore à lecteur unique : surveillance du fichier par sondage mtime en tâche asyncio, validation intégrale avant tout remplacement, swap atomique en une opération — le changement prend effet à l'événement suivant ; un YAML invalide est rejeté avec erreur explicite et les règles précédentes restent actives ; la source MQTT resynchronise ses souscriptions au swap sans déconnexion.

## Boundaries & Constraints

**Always:**
- Validation intégrale avant swap : schéma + motifs + limites + syntaxe des gabarits (le chargement existant) ; YAML invalide → erreur explicite journalisée, ancienne config conservée, service continue.
- Swap atomique : toute étape du pipeline lit intégralement l'ancienne ou la nouvelle configuration — jamais un mélange, jamais une config à moitié chargée.
- La modification prend effet à partir de l'événement suivant, sans redémarrage ni intervention (SM-2, FR-7).
- Resynchronisation des souscriptions au swap : abonnement des nouveaux topics, désabonnement des disparus, sans déconnexion du broker ; un message sur un topic retiré ne déclenche plus rien.
- Rotation du device token à chaud : le prochain envoi utilise le nouveau token sans redémarrage.
- Une ligne de journal par rechargement (réussi ou rejeté), timestamps ISO 8601 UTC.
- Seul le ConfigStore lit le fichier YAML (lecteur unique, contrôlable par grep).
- Changement de `broker.host`/`broker.port` : rechargement rejeté avec erreur explicite « redémarrage requis », ancienne config conservée.
- L'événement en traversée reste figé : la règle est capturée à la sélection dans la config courante, jamais relue (AC-1.2.6 inchangé).

**Never:**
- Pas de purge ni de gestion des fenêtres de debounce (story 2.2) ; pas de rejeu des messages ; pas de nouvelle souscription au broker via reconnexion (jamais de déconnexion).
- Pas de watchers externes (inotify, watchdog) : sondage mtime en tâche asyncio uniquement.
- Aucune écriture du fichier YAML par le démon.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Prise d'effet sans redémarrage | libellé modifié dans le YAML | l'événement suivant utilise le nouveau libellé | N/A |
| Swap atomique | rechargement pendant une traversée | chaque étape voit intégralement l'ancienne ou la nouvelle | Jamais de mélange |
| YAML invalide rejeté | schéma/gabarit/limites cassés | erreur explicite, anciennes règles actives, événement suivant traité avec l'ancienne config | Service continue |
| Resynchronisation | règles ajoutées et retirées | nouveaux topics abonnés, disparus désabonnés, sans déconnexion ; topic retiré → plus rien | N/A |
| Token tourné | `device_token` modifié | prochain envoi avec le nouveau token, sans redémarrage | N/A |
| Host/port modifiés | `broker.host` ou `port` changé | rechargement rejeté, « redémarrage requis », ancienne config conservée | Service continue |
| Rechargements journalisés | chaque rechargement | une ligne (réussi / rejeté) ISO 8601 UTC | N/A |

</frozen-after-approval>

## Code Map

- `mqtt_notif/config.py` -- à transformer en ConfigStore : chargement initial + `get()` (snapshot figé), tâche de sondage mtime (constante d'intervalle), rechargement validé avant swap, swap en une opération, journal des rechargements. Reste l'unique lecteur du YAML.
- `mqtt_notif/mqtt.py` -- à modifier : souscriptions pilotées par la config courante du store ; au changement, delta abonnement/désabonnement sans déconnexion (aiomqtt `subscribe`/`unsubscribe`).
- `mqtt_notif/main.py` -- à modifier : la sélection lit `store.get()` à chaque message (règle figée dans l'événement) ; le store et son watcher sont créés à l'assemblage ; arrêt propre du watcher au SIGTERM.
- `mqtt_notif/notifier.py` -- à modifier : le device token est lu dans le snapshot du store au moment de l'envoi (jamais mis en cache dans le Notifier) — la rotation prend effet au prochain envoi.
- `tests/` -- à étendre : rechargement et effet à l'événement suivant, YAML invalide conservé, resynchronisation sans déconnexion, swap atomique (aucune vue mixte), rotation token, host/port rejeté, lecteur unique (grep), journalisation.

## Tasks & Acceptance

**Execution:**
- [ ] `mqtt_notif/config.py` -- ConfigStore : sondage mtime, validation avant swap, swap atomique, journal -- AC-2.1.1, AC-2.1.2, AC-2.1.3, AC-2.1.5.
- [ ] `mqtt_notif/mqtt.py` -- resynchronisation des souscriptions au swap (delta abonner/désabonner, sans déconnexion) -- AC-2.1.4.
- [ ] `mqtt_notif/notifier.py` -- token lu au moment de l'envoi depuis le snapshot du store -- AC-2.1.7.
- [ ] `mqtt_notif/main.py` -- assemblage : store + watcher, sélection depuis `store.get()`, arrêt propre du watcher.
- [ ] `tests/` -- couvrir la matrice I/O, dont un test de lecteur unique (échoue si un autre module lit le YAML) -- AC-2.1.6 et chaque AC.

**Acceptance Criteria:**
- Given le démon en marche et un libellé modifié, when l'événement suivant arrive, then il utilise le nouveau libellé sans redémarrage.
- Given un YAML invalide, when le rechargement a lieu, then erreur explicite, anciennes règles actives, service continue.
- Given des règles ajoutées et retirées, when le swap a lieu, then souscriptions resynchronisées sans déconnexion ; topic retiré → aucune notification.
- Given le token modifié, when le prochain envoi part, then il utilise le nouveau token.
- Given host/port modifiés, then rechargement rejeté avec « redémarrage requis », ancienne config conservée.

## Implementation Notes

Décisions internes (non perceptibles pour l'utilisateur) :
- Le store expose un `get()` renvoyant la `Config` figée (dataclass immuable, règles en tuple) : le swap est une simple réaffectation d'attribut — atomique pour tout lecteur ; aucune vue mixte possible par construction.
- Notification du changement : le watcher arme un `asyncio.Event` après un swap réussi ; la source MQTT attend (FIRST_COMPLETED) le prochain message ou l'événement de changement, puis applique le delta de souscriptions — jamais de déconnexion, jamais de reconnexion.
- Le rendu et la sélection restent sur l'événement figé ; seul le token (hors de l'événement, AD-10) et les futures règles (événements suivants) suivent le store.
- Interval de sondage : constante de l'ordre de la seconde (le « prochain événement » masque la latence du sondage ; la précision n'a pas d'exigence).
- Changement host/port : comparaison avec la config courante au rechargement → rejet explicite plutôt que swap silencieux inopérant.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, chaque AC automatisable couverte.
