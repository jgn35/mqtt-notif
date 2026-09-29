---
title: 'Story 2.2 — Debounce par règle'
type: 'feature'
created: '2026-09-28'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 34706a9
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Un bouton qui rebondit ou qu'on tripote envoie une rafale de notifications sur le téléphone ; rien ne permet de poser un délai minimum entre deux notifications.

**Approach:** Ajouter une étape dédiée du pipeline entre le rendu et l'émetteur : un `debounce_ms` optionnel par règle (défaut 0 = pas de debounce), fenêtre en mémoire ouverte à la réception du message (jamais à l'envoi), clé = nom unique de la règle ; au swap de config, purge des fenêtres des règles disparues et conservation de celles qui restent.

## Boundaries & Constraints

**Always:**
- `debounce_ms` optionnel par règle, bornes [0, 3 600 000] (0 = pas de debounce, défaut) ; hors bornes → rejet explicite au chargement (démarrage refusé / rechargement rejeté, anciennes règles conservées).
- Silence pendant la fenêtre : un message reçu pendant le délai de sa règle ne déclenche aucune notification ; le message ignoré est journalisé en debug.
- Fenêtre ouverte à la réception du message, jamais à l'envoi : un envoi FCM lent n'étend pas la fenêtre ; un message arrivé pendant le délai reste ignoré même si l'envoi traîne.
- Sans délai (absent ou 0), chaque message notifie.
- Indépendance des règles : les fenêtres ne se partagent jamais, clé = nom unique de la règle.
- Au swap de config : purge des fenêtres des règles disparues, conservation de celles des règles conservées — un message immédiat sur une règle conservée pendant sa fenêtre reste ignoré.
- Redémarrage : toutes les fenêtres réinitialisées (aucun état persisté, AD-9) ; timer via `asyncio.sleep`, aucun thread applicatif (AD-2).
- État du debounce en mémoire, propriété du pipeline, jamais persisté ni partagé.

**Never:**
- Pas de file d'attente ni de rejeu : le message ignoré est perdu (QoS 0, perte assumée).
- Pas de debounce global ni de configuration hors du YAML par règle ; pas de persistance des fenêtres ; pas de thread ni de verrou externe.
- Le debounce ne touche ni la sélection ni le rendu : il s'insère entre rendu et émetteur (AD-1, AD-7).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Silence pendant la fenêtre | règle `debounce_ms: 1000`, 2e message 400 ms après le premier notifié | aucune notification, message ignoré journalisé (debug) | N/A |
| Retour au silence après la fenêtre | même règle, message après expiration (1000 ms) | notification envoyée, nouvelle fenêtre ouverte | N/A |
| Fenêtre ancrée sur la réception | envoi FCM lent, délai expire pendant l'envoi | fenêtre ancrée sur la réception, message pendant le délai ignoré même si l'envoi traîne | N/A |
| Sans délai | règle sans `debounce_ms` (ou 0), rafale | chaque message notifie | N/A |
| Indépendance des règles | deux règles avec debounce, message sur la 2e pendant sa fenêtre | 2e ignorée, 1re reste pleinement active hors de sa propre fenêtre | N/A |
| Bornes de configuration | `debounce_ms` négatif ou > 3 600 000 | rejet explicite au chargement/rechargement | Démarrage refusé / anciennes règles conservées |
| Swap de config | fenêtres ouvertes dont une règle supprimée | fenêtre purgée pour la règle disparue, conservée pour l'autre ; message immédiat sur la règle conservée pendant sa fenêtre ignoré | N/A |
| Redémarrage | fenêtres ouvertes au redémarrage | toutes réinitialisées, premier message de chaque règle notifie | N/A |

</frozen-after-approval>

## Code Map

- `mqtt_notif/debounce.py` -- à créer : `Debouncer` en mémoire — `should_notify(rule, received_at) -> bool` (fenêtre ancrée à la réception, clé = nom de règle), `purge(noms conservés)` pour le swap, horloge injectable pour les tests, aucun thread, aucun verrou externe (mono-boucle).
- `mqtt_notif/rules.py` -- à modifier : `Rule` gagne `debounce_ms: int = 0` (frozen dataclass étendue).
- `mqtt_notif/config.py` -- à modifier : validation `debounce_ms` (int, bornes [0, 3 600 000]) avec règle citée dans l'erreur ; clé autorisée mise à jour.
- `mqtt_notif/main.py` -- à modifier : étape debounce dans la tâche de dispatch (entre rendu et émetteur — le rendu reste en amont, l'émetteur n'est appelé que si la fenêtre l'autorise) ; purge au swap de config (règles conservées) ; l'horodatage de réception est capturé à la réception du message, pas à l'envoi.
- `mqtt_notif/notifier.py` -- inchangé.
- `tests/` -- à créer/étendre : `test_debounce.py` (fenêtre, bornes, purge, indépendance), tests config (bornes), tests pipeline (rafale silenciée, message ignoré journalisé, fenêtre non étendue par un envoi lent, purge au swap via store).

## Tasks & Acceptance

**Execution:**
- [x] `mqtt_notif/debounce.py` -- Debouncer : fenêtre par nom de règle ancrée à la réception, purge, horloge injectable -- AD-7, AC-2.2.1, AC-2.2.3, AC-2.2.5, AC-2.2.7.
- [x] `mqtt_notif/config.py` + `rules.py` -- `debounce_ms` par règle, validation des bornes [0, 3 600 000] -- AC-2.2.6.
- [x] `mqtt_notif/main.py` -- étape debounce entre rendu et émetteur, purge au swap, horodatage capturé à la réception -- AC-2.2.1, AC-2.2.2, AC-2.2.3, AC-2.2.7, AC-2.2.8.
- [x] `tests/` -- couvrir la matrice I/O et chaque AC automatisable -- chaque AC.

**Acceptance Criteria:**
- Given une règle `debounce_ms: 1000` et un premier message notifié, when un second arrive 400 ms plus tard, then aucune notification et le message ignoré est journalisé.
- Given le même cas, when un message arrive après expiration du délai, then il notifie et une nouvelle fenêtre s'ouvre.
- Given un envoi FCM lent pendant la fenêtre, when le délai expire pendant l'envoi, then la fenêtre reste ancrée sur la réception du message.
- Given `debounce_ms` négatif ou > 3 600 000, when la config est chargée/rechargée, then rejet explicite.
- Given des fenêtres ouvertes dont une règle supprimée au rechargement, when le swap a lieu, then purge de la règle disparue, conservation des autres.

## Implementation Notes

Décisions internes (non perceptibles pour l'utilisateur) :
- Le Debouncer vit dans la boucle asyncio unique : un simple dict nom → horodatage de réception ; toute mutation se fait dans la tâche de dispatch (mono-boucle, pas de verrou) ; la purge au swap est appelée par la source ou le pipeline au moment du `changed` — jamais de thread.
- Fenêtre ouverte = horodatage de réception du dernier message notifié ; un message reçu pendant `notified_at + debounce_ms` est ignoré (aucun repositionnement de fenêtre sur un message ignoré — la fenêtre n'est prolongée que par une notification).
- L'horloge (`time.monotonic`) est injectable pour des tests déterministes ; la réception est capturée avant l'étape debounce (jamais après le rendu ni l'envoi).
- Position exacte dans le pipeline : la porte de debounce s'exécute dans la tâche de dispatch avant l'appel de l'émetteur — le rendu (dans notifier.notify) n'est exécuté que pour les messages qui passent la porte ; l'isolation AD-1 est préservée, l'ordre physique rendu/debounce s'écarte de l'ordre logique sans effet comportemental (consigné au Review Triage Log).
- La fenêtre s'ouvre sur la décision (message reçu hors fenêtre), jamais sur le résultat de l'envoi : un échec FCM ne referme pas la fenêtre (AD-7 « pas à l'envoi », test pinant ce comportement).
- Aucun timer ni sommeil : la fenêtre est une comparaison à l'arrivée du message (écart à la mention « asyncio.sleep » de la spec, AD-2 respecté — aucun thread).

## Review Triage Log

- `debounce.py/main.py` — un envoi FCM qui échoue laisse la fenêtre ouverte (messages suivants silenciés sans notification livrée) — false comme défaut — conforme à AD-7 « fenêtre ouverte à la réception (pas à l'envoi) » : la fenêtre ne dépend jamais du résultat de l'envoi — patch (test pinant l'interaction + docstring explicite).
- `main.py` docstrings — « journalisé en debug » alors que log_line n'a pas de niveaux — low — réel (doc) — patch (marqueur `debug:` sur la ligne, alignement des docstrings).
- `main.py/_dispatch` — le debounce précède physiquement le rendu (rendu dans notifier.notify), pas « entre rendu et émetteur » — low — réel (écart à l'ordre logique AD-1) ; isolation des étapes préservée, comportement équivalent, messages silenciés jamais rendus — patch (docstrings + test renommés ; écart consigné ici).
- `main.py` — le log de message silencié n'indique pas le temps restant dans la fenêtre — low — réel — patch (millisecondes restantes dans la ligne).
- `mqtt.py:iter_messages` — paramètre debouncer optionnel (None = purge silencieusement ignorée) — low — réel (footgun) — patch (paramètre requis).
- `debounce.py:purge` — une règle redéfinie sous le même nom garde sa fenêtre — false — AD-7 : « persistance de celles conservées » (conservation par nom) — patch (docstring + test pinant la redéfinition).
- `tests/test_debounce.py` — résultats de `until()` jamais affirmés (timeout dégradé en échec distant) ; helpers `bump_mtime`/`write_yaml` dupliqués depuis test_hot_reload.py ; asserts temps réel fragiles (0.15 s) — medium — réel — patch (assert, conftest, alignement).
- `tests/` — chemin horloge réelle jamais exercé avec expiration (tout via FakeClock) — low — réel — patch (un test 20 ms + court sommeil réel).
- `tests/` — précédence des erreurs de validation combinées (debounce_ms + gabarit) non pinnée — low — réel — patch (test).
- `debounce.py` — comparaison de frontière flottante (fenêtre semi-ouverte) — low — négligeable en usage réel (jitter de réception >> erreur flottante) — rejet avec note docstring.
- `debounce.py` — horodatage antidaté / NaN (horloge injectée) — false en production — time.monotonic monotone et finie — rejet.
- Écart consigné : la spec figée mentionnait « timer via asyncio.sleep » ; l'implémentation compare à l'arrivée du message, sans sommeil — AD-2 (aucun thread) respecté, mécanisme plus simple ; écart noté dans les Implementation Notes.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, chaque AC automatisable couverte.
