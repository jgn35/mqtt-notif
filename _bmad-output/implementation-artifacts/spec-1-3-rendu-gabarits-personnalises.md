---
title: 'Story 1.3 — Rendu des gabarits personnalisés'
type: 'feature'
created: '2026-09-28'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 5a7f3eb
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Toutes les notifications portent le même message fixe — le YAML ne pilote pas le contenu, chaque événement affiche la même chose.

**Approach:** Rendre `title`, `short_message` et `detailed_message` (optionnel) depuis des gabarits par règle qui référencent les champs du payload MQTT : substitution `{champ}` et `{chemin.pointé}` en mode `json` (défaut), `{payload}` seul en mode `raw`, `{{` → accolade littérale, champ absent → `[absent]` — jamais d'exception, jamais d'envoi avorté.

## Boundaries & Constraints

**Always:**
- Validation de la syntaxe des gabarits au chargement : gabarit malformé (placeholder avec espace, accolade non fermée, placeholder interdit en mode `raw`) → démarrage refusé, erreur citant la règle et le champ fautifs.
- Payload parsé à la sélection : dict pour une règle `json`, str pour une règle `raw` ; payload JSON non parsable → rendu comme si tous les champs étaient absents + avertissement journalisé, le message n'est jamais ignoré en bloc.
- Champ ou chemin absent → texte `[absent]`, notification envoyée quand même ; aucune exception de rendu ne peut atteindre la boucle.
- `{{` produit un `{` littéral, sans tenter la substitution.
- `detailed_message` absent de la règle → la clé n'est pas envoyée dans le message FCM ; présent → clé `detailed_message` ajoutée.
- Le notifier reste l'unique rédacteur FCM ; il reçoit désormais l'événement et construit le message depuis le rendu.
- Conventions héritées : échec rapide avant le démarrage, événement immuable, logs plats ISO 8601 UTC, QoS 0, première-gagnante.

**Never:**
- Aucune fonction, filtre ni logique scriptée dans les gabarits (pas de Jinja2 ni évaluateur embarqué).
- Pas de validation des limites de taille (titre/messages) — c'est la story 1.4 ; pas de debounce, pas de rechargement à chaud.
- Pas de mutation de l'événement en traversée ; pas de re-parsing du payload après la sélection.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Substitution json | payload `{"action": "simple"}`, gabarit `Bouton appuye ({action})` | `Bouton appuye (simple)` | N/A |
| Chemin pointé imbriqué | `{"capteur": {"lieu": "salon"}}`, gabarit `{capteur.lieu}` | `salon` ; chemin inexistant → `[absent]` | N/A |
| Champ absent | gabarit référençant un champ absent | `[absent]` substitué, notification envoyée | Aucune exception |
| Payload json non parsable | règle `json`, payload non-JSON | rendu tout `[absent]` + avertissement journalisé, notification envoyée | Jamais ignoré en bloc |
| Mode raw | `payload_format: raw`, gabarit `{payload}` | payload brut décodé UTF-8 substitué ; tout autre placeholder → rejet au chargement | Rejet démarrage |
| Accolade littérale | gabarit avec `{{` | `{` littéral, pas de substitution | N/A |
| Gabarit malformé au chargement | placeholder avec espace, accolade non fermée | démarrage refusé, erreur citant règle + champ | Exit code non nul |
| Message détaillé optionnel | règle sans `detailed_message` | seules `title` et `short_message` dans le message FCM | N/A |
| Deux événements, deux règles | gabarits différents | deux notifications au contenu distinct, fidèles aux gabarits | N/A |

</frozen-after-approval>

## Code Map

- `mqtt_notif/render.py` -- à créer : moteur de substitution unique (`{{` → `{`, `{chemin}` → valeur, absent → `[absent]`), validation de gabarit pour le chargement, aucune dépendance à un moteur de template externe.
- `mqtt_notif/config.py` -- à modifier : la règle gagne `payload_format` (`json` défaut, `raw`), `title`, `short_message`, `detailed_message` (optionnel) ; validation : gabarits non vides pour `title`/`short_message`, syntaxe validée, mode `raw` n'accepte que `{payload}`. Clés autorisées mises à jour.
- `mqtt_notif/rules.py` -- à modifier : `Rule` gagne les champs de gabarit et `payload_format` (frozen dataclass étendue).
- `mqtt_notif/main.py` -- à modifier : à la sélection, le payload est parsé selon `payload_format` (dict/str) et figé dans l'événement ; non-parsable → payload vide + avertissement ; rendu effectué avant l'émetteur.
- `mqtt_notif/notifier.py` -- à modifier : `notify(event)` construit le message FCM depuis le rendu (`title`, `short_message`, `detailed_message` si présent) ; les constantes de message fixe de la 1.1 disparaissent ; le notifier reste le seul point d'appel FCM.
- `tests/` -- à étendre : rendu (matrice complète), validation au chargement, pipeline bout-en-bout avec contenus distincts, notifier recevant l'événement.

## Tasks & Acceptance

**Execution:**
- [x] `mqtt_notif/render.py` -- moteur de substitution + validation de gabarits (malformé → erreur nommant règle et champ) -- AC-1.3.1, AC-1.3.2, AC-1.3.3, AC-1.3.6, AC-1.3.7.
- [x] `mqtt_notif/config.py` -- `payload_format`, `title`, `short_message`, `detailed_message` par règle, validation au chargement (raw : `{payload}` seul) -- AC-1.3.5, AC-1.3.7.
- [x] `mqtt_notif/rules.py` + `mqtt_notif/main.py` -- payload parsé à la sélection, figé dans l'événement ; non-parsable → rendu `[absent]` + avertissement -- AC-1.3.4.
- [x] `mqtt_notif/notifier.py` -- message FCM construit depuis le rendu de l'événement, `detailed_message` seulement si présent -- AC-1.3.8, AC-1.3.9.
- [x] `tests/` -- couvrir la matrice I/O et les AC automatisables, dont un test pipeline deux règles/gabarits distincts -- chaque AC.

**Acceptance Criteria:**
- Given un gabarit malformé, when le démon démarre, then démarrage refusé avec erreur citant la règle et le champ.
- Given un payload non parsable en mode json, when le message est traité, then notification rendue tout `[absent]` + avertissement, jamais ignorée.
- Given deux règles aux gabarits différents déclenchées, then deux notifications au contenu distinct (FR-4).
- Given une règle sans `detailed_message`, then seules `title` et `short_message` sont envoyées.

## Implementation Notes

Décisions internes (non perceptibles pour l'utilisateur) :
- Moteur maison minimal : scan du gabarit, `{{` → `{` littéral, `{chemin}` → résolution par segments dans le dict (chemins pointés) ; aucune bibliothèque de template (AD-5 : ni filtre ni logique scriptée).
- Substitution des valeurs non-chaîne : scalaires rendus via `str()`, `None` → `null`, booléens → `true`/`false` ; dict/list sur le chemin final → JSON compact (choix stable, documenté ici).
- Le rendu s'exécute dans la tâche de dispatch (hors boucle d'écoute), jamais dans la source ; aucune erreur de rendu ne remonte au pipeline.

## Review Triage Log

- `main.py:_parse_payload` — payload json valide mais non-dict journalisé comme « non parsable » (deux conditions confondues) — low — réel — patch (message distinct).
- `notifier.py` — les lignes d'échec d'envoi ne portent ni topic ni règle (impossible de corréler un échec à son message) ; une exception de rendu serait étiquetée « fcm send failed » — low — réel — patch (corrélation + log distinct).
- `render.py:validate_template` — placeholders à segments vides (`{a.}`, `{.a}`) acceptés au chargement mais résolvent `[absent]` à jamais — low — réel, contraire à l'échec rapide des gabarits — patch (rejet au chargement) ; note docstring : les clés contenant un point ne sont pas adressables (syntaxe `{chemin.pointé}`, AD-5).
- `main.py:_parse_payload` — json.loads lève RecursionError (imbrication extrême) qui échappe au except ValueError → message ignoré + restart pipeline — low — réel mais improbable à l'échelle domestique ; correctif direct — patch.
- `tests/test_config.py` — le cas `None` paramétré écrit le littéral « None » (pas de null YAML réel) ; pas de test template non-chaîne ; tests templates avec clés YAML dupliquées — medium — réel, lacunes démontrées par mutation — patch.
- `tests/` — aucune règle `raw` ni payload dégradé ne traverse `run_pipeline` de bout en bout (« jamais ignoré en bloc » pinné seulement en unitaire) — medium — réel, démontré par mutation (`if not event.payload: continue` reste vert) — patch (tests pipeline).
- `tests/test_render.py` — assertion avec opérande mort (`or payload == …`) — low — réel (test trompeur) — patch.
- Limites de taille FCM (4 Ko data) non gérées — false comme défaut de 1.3 — périmètre figé : « pas de validation des limites de taille — c'est la story 1.4 » — rejet (story 1.4, immédiatement suivante).
- Clés pointées vs clés contenant un point (`{"a.b": …}` inadressable) — false — AD-5 définit le point comme séparateur de chemin, choix de syntaxe de l'intent — rejet (limitation documentée).
- Re-tokenisation des gabarits à chaque rendu — low — micro-optimisation sans impact perceptible à l'échelle domestique — rejet.
- `Event` dans main.py avec import TYPE_CHECKING — low — pattern standard, le déplacement vers un module feuille serait un refactor hors périmètre — rejet.
- Longueurs de lignes > 90 non uniformes — low — cosmétique, pas de lint configuré — rejet.
- Pas de README/exemple YAML pour les nouvelles clés de règle — false comme défaut de 1.3 — la story 3.2 (AC-3.2.5) couvre explicitement l'exemple de YAML réel commenté — rejet avec renvoi au plan.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, chaque AC automatisable couverte.
