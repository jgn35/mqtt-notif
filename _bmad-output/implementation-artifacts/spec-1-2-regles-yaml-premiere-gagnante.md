---
title: 'Story 1.2 — Règles YAML et sélection première-gagnante'
type: 'feature'
created: '2026-09-28'
status: 'done'
route: 'dispatch'
review_loop_iteration: 1
baseline_commit: cc64d29
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Le YAML ne pilote rien : un seul topic codé en dur, aucun filtrage, et tout message déclenche la même notification fixe sans identité.

**Approach:** Introduire la section `rules` du YAML (nom unique + topic exact ou motif `+`/`#`, validés au chargement), la sélection première-gagnante dans un nouveau `rules.py`, la souscription à l'ensemble des topics des règles, et l'objet événement immuable (topic + payload brut + règle figée) qui traverse le pipeline sans être relu.

## Boundaries & Constraints

**Always:**
- Première règle gagnante : évaluation dans l'ordre de déclaration, première correspondance traite le message ; plusieurs correspondances → avertissement journalisé ; aucune → message ignoré, journalisé.
- Un message reçu déclenche au plus une notification, y compris quand les souscriptions des règles se chevauchent (l'ensemble de souscriptions est réduit à une couverture minimale).
- Événement immuable : topic (str), payload brut (str), règle figée au moment de la sélection — jamais relu depuis le ConfigStore en traversée.
- Validation au chargement : nom unique non vide, topic non vide et syntaxiquement valide (`+` niveau entier, `#` dernier niveau uniquement, aucun espace) ; règle fautive nommée dans l'erreur, démarrage refusé.
- Contenu de notification inchangé (message fixe de 1.1) — le rendu arrive en story 1.3.
- Conventions héritées : QoS 0, boucle asyncio unique, logs plats ISO 8601 UTC, échec rapide avant le démarrage uniquement.

**Never:**
- Pas de rendu/gabarits, pas de `payload_format` (le payload reste brut dans l'événement), pas de debounce, pas de rechargement à chaud (config statique jusqu'à la story 2.1).
- Pas de relecture du YAML en cours d'exécution ; pas de mutation de l'événement ni de la règle en traversée.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Démarrage avec règles valides | YAML : `rules` bien formée | Souscription QoS 0 à l'ensemble des topics des règles | N/A |
| Règle fautive (topic absent, nom vide, nom dupliqué, motif invalide) | YAML invalide | Démarrage refusé, erreur explicite citant la règle fautive | Exit code non nul |
| Message couvert par deux règles | Publication sur topic couvert par 2 motifs | Première règle traite, avertissement journalisé, une seule notification | N/A |
| Motif `+` / `#` | `home/+/bouton` ou `home/#` | Topic couvert correspond ; topic non couvert ne correspond pas | N/A |
| Topic hors périmètre | Publication sans règle correspondante | Aucune notification, ligne de journal dédiée | N/A |
| Événement en traversée | Message en cours de traitement | Événement immuable, règle figée à la sélection — toute mutation lève | N/A |

</frozen-after-approval>

## Code Map

- `mqtt_notif/rules.py` -- à créer : `Rule` (frozen dataclass), `find_rule(rules, topic)` première-gagnante (renvoie la règle + le nombre de correspondances), `topic_matches(pattern, topic)` (motifs `+`/`#`, `+` accepte le niveau vide, sémantique MQTT 4.7.1), et `subscription_topics(rules)` : ensemble de souscriptions **deux à deux disjoints** couvrant l'union des topics des règles — fusion en plus petit sur-ensemble des filtres qui se chevauchent, itérée jusqu'au point fixe (la couverture par contenance seule est insuffisante : le broker livre un message une fois par souscription correspondante) ; doublons exacts abonnés une seule fois.
- `mqtt_notif/config.py` -- à modifier : remplacer la clé `topic` unique par la section `rules` (liste ordonnée : `name`, `topic`), validation stricte au chargement (la validation 1.1 qui rejetait `+`/`#` disparaît — les motifs sont désormais légaux). Conserver broker/device_token. Rejeter les clés inconnues dans une entrée de règle et les caractères de contrôle dans les topics.
- `mqtt_notif/mqtt.py` -- à modifier : souscrire à l'ensemble de souscriptions disjoint des topics des règles à chaque connexion, y compris après reconnexion ; deux souscriptions retenues ne peuvent pas correspondre au même topic — un message chevauchant plusieurs règles n'est livré qu'une fois.
- `mqtt_notif/main.py` -- à modifier : introduire l'étape de sélection entre source et émetteur — construire l'événement immuable, avertir si plusieurs règles correspondent, journaliser les non-correspondances, passer la règle au notifier (contenu encore fixe).
- `mqtt_notif/notifier.py` -- inchangé (message fixe jusqu'à 1.3) ; l'événement n'y entre pas encore.
- `tests/` -- à modifier/étendre : tests config (schéma règles, clés inconnues, caractères de contrôle), règles (correspondance, première-gagnante, motifs, disjonction des souscriptions), source (souscriptions multiples, re-souscription après reconnexion multi-règles), pipeline (au plus une notification par message — avec un faux broker qui livre chaque message une fois par souscription correspondante —, événement figé).

## Tasks & Acceptance

**Execution:**
- [x] `mqtt_notif/rules.py` -- créer la sélection première-gagnante et la correspondance de motifs `+`/`#` (`+` accepte le niveau vide) -- AD-4.
- [x] `mqtt_notif/rules.py` -- `subscription_topics(rules)` : fusion des filtres chevauchants en leur plus petit sur-ensemble MQTT, jusqu'à disjonction deux à deux -- AC-1.2.2.
- [x] `mqtt_notif/config.py` -- section `rules` + validation stricte (nom unique non vide, topic valide, motif légal : `#` dernier niveau uniquement, aucun caractère de contrôle, clés inconnues rejetées) -- AC-1.2.1.
- [x] `mqtt_notif/mqtt.py` -- souscrire à l'ensemble disjoint des topics des règles à chaque connexion -- AC-1.2.2.
- [x] `mqtt_notif/main.py` -- événement immuable topic+payload brut+règle figée, avertissement multi-correspondances, journal des non-correspondances -- AC-1.2.3, AC-1.2.5, AC-1.2.6.
- [x] `tests/` -- couvrir la matrice I/O et les AC automatisables, dont : le faux broker livre chaque message une fois par souscription correspondante ; un pipeline avec filtres en chevauchement partiel (ex. `home/+/capteur` + `home/salon/+`) produit une seule notification par message ; la reconnexion re-souscrit l'ensemble complet (multi-règles) -- chaque AC.

**Acceptance Criteria:**
- Given une règle sans topic, un nom vide ou dupliqué, when le démon démarre, then démarrage refusé avec erreur citant la règle fautive.
- Given deux règles dont les motifs couvrent le même message, when il arrive, then seule la première traite, un avertissement est journalisé, une seule notification part.
- Given une règle à motif `+`/`#`, when un topic couvert (resp. non couvert) arrive, then elle correspond (resp. ne correspond pas).
- Given un message pendant que la config change en mémoire de test, when il traverse le pipeline, then il est traité avec la règle figée à la sélection, sans relecture.

## Implementation Notes

Décisions internes (non perceptibles pour l'utilisateur) :
- Souscriptions disjointes : MQTT livre un message une fois par souscription correspondante ; des filtres chevauchants provoqueraient des doublons. Deux filtres dont les langages s'intersectent sont fusionnés en leur plus petit sur-ensemble MQTT (position par position : exacts égaux → exact, sinon `+` ; épuisement d'un filtre ou `#` → `#` en fin), itéré jusqu'au point fixe ; l'évaluation première-gagnante reste sur la liste complète des règles. Les topics supplémentaires couverts par un sur-ensemble mais hors règles sont ignorés par la sélection (log dédié).
- La correspondance de motifs convertit le filtre MQTT en expression régulière (`+` → `[^/+]*` — niveau vide accepté, sémantique MQTT 4.7.1 —, `#` → fin de hiérarchie incluant le parent, segments ancrés) — mise en cache par filtre.

Décisions prises pendant l'implémentation (tentative 1, révertée par le bouclage d'itération 1 — cf. Spec Change Log ; les décisions non contredites restent valables) :
- `rules: []` (liste vide) rejetée au démarrage — un démon sans règle ne ferait rien silencieusement ; cohérent avec la doctrine d'échec rapide.
- `home/#` couvre aussi le niveau parent `home` (sémantique MQTT stricte), appliquée de façon cohérente en correspondance et en couverture.
- L'événement n'entre pas encore dans le notifier (message fixe inchangé) : `_dispatch` le reçoit, `Notifier.notify()` garde sa signature — résolution du conflit interne de la spec en faveur de « notifier inchangé ».
- Implémenté par sous-agent, commit `c34255d`.

## Review Triage Log

- `rules.py/minimal_subscription_topics` + tests — chevauchement partiel : deux filtres non emboîtés restent tous deux abonnés, le broker livre le message une fois par souscription correspondante, deux notifications partent (AC-1.2.2/NFR-3 violés) ; aucune trace en test car le faux broker ne modélise pas la livraison par souscription — high — réel, vérifié par exécution (`home/+/capteur` + `home/salon/+` retenus, `find_rule` = 2 correspondances) — blind#1/#2, edge claim, vgap#1 : bad_spec (la spec prescrivait la réduction par contenance seule, insuffisante).
- `rules.py/topic_matches` — `+` n'accepte pas le niveau vide (`[^/+]+`) alors que le broker MQTT livre `home//x` à un filtre `home/+/x` : message livré puis ignoré par la sélection — medium — réel (sémantique MQTT 4.7.1) — blind#7, edge#1/#6 : patch.
- `config.py/_validate_topic_pattern` — caractères de contrôle (NUL, etc.) acceptés dans un topic ; le broker rejetterait SUBSCRIBE → boucle de reconnexion infinie — medium — réel — edge#4 : patch.
- `config.py/_validate_rule` — clés inconnues dans une entrée de règle ignorées en silence (typos comme `topick` non détectées) — low — réel, contraire à la doctrine d'échec rapide — blind#5 : patch.
- `mqtt.py` — la re-souscription de l'ensemble complet des topics après reconnexion n'est affirmée par aucun test multi-règles — medium — réel (régression silencieuse possible) — vgap#2 : patch (test).
- `config.py` docstring — « occéder » (faute) — low — cosmétique — blind#8 : patch.
- `rules.py/_covers` — branche `return level == LEVEL_HASH` inatteignable après le `return True` amont — low — cosmétique — blind#6 : patch.
- `main.py` — `str(message.topic)` sur un topic bytes produirait `b'...'` — false — aiomqtt livre toujours le topic décodé en str/Topic — rejet.
- `main.py/build_event` — avertissement multi-correspondances émis à chaque message quand deux règles partagent le même topic — false — comportement exigé par AD-4 (avertissement journalisé) ; des topics dupliqués entre règles sont une config légitime (première-gagnante) — rejet.
- `mqtt.py` — Config construit directement avec `rules=()` contournerait la validation — low — injoignable depuis la production (Config n'est construit que par load_config validé) — rejet.
- `notifier.py` — `Message.token` déprécié — false — décision AD-6 délibérée (jamais `fid`), déjà triée en story 1.1 — rejet.
- Itération 2 — `config.py:80` : `sorted()` sur clés de types mixtes lève TypeError brut au lieu de ConfigError (entrée avec `42:` et `topick:`) — medium — réel, vérifié par exécution par deux couches — patch.
- Itération 2 — `main.py/_dispatch` : la sélection de règle n'a aucun effet observable sur la notification (message fixe) — false — périmètre figé de la spec : contenu de notification inchangé, le rendu arrive en story 1.3 — rejet.
- Itération 2 — `main.py/build_event` : avertissement multi-correspondances émis à chaque message — carried (itération 1) — false — comportement exigé par AD-4 — rejet.
- Itération 2 — `main.py` : log « message ignored » amplifié par le sur-ensemble de souscriptions fusionné — low — volume domestique faible, la ligne est utile (AC-3.2.4) ; niveaux de log ou rate-limit hors périmètre — rejet.
- Itération 2 — `tests/test_mqtt.py` : test tautologique sur la livraison unique (break à la première livraison) — medium — réel — patch.
- Itération 2 — `rules.py`/`conftest.py` docstrings : « le broker livre une fois par souscription correspondante » présenté comme garanti ; MQTT 3.1.1 §3.3.5 permet la livraison unique — low — réel (doc) — patch (« peut livrer »).
- Itération 2 — `tests/test_config.py` : tests Cc dépendants de l'échappement double-quoted YAML sans commentaire protecteur — low — réel (fragilité de test) — patch.
- Itération 2 — `tests/test_main.py` : `RULES_YAML` mort ; test no-match avec capsys inutilisé et sans assertion négative — low — réel — patch.
- Itération 2 — `config.py` : clés racine inconnues ignorées en silence (incohérent avec le rejet des clés inconnues des règles ; l'ancienne clé `topic` de la 1.1 passe sans indice) — medium — réel — patch (rejet racine + indice de migration pour `topic`).
- Itération 2 — `tests/test_rules.py` : pas de test `re.escape` (méta-caractères dans un topic exact) ni de test bout-en-bout d'une règle `#` seule — medium — réel — patch.
- Itération 2 — `config.py` : caractères de format (Cf : U+200B, U+FEFF) passent le contrôle Cc-only — low — réel — patch.
- Itération 2 — `mqtt.py` : un broker qui refuse un filtre (ACL) est indistinguable d'un broker à terre — false en environnement cible — NFR-4 : réseau domestique fermé, pas d'ACL ni d'auth broker en v1 — rejet.
- Itération 2 — `mqtt.py:46` : topic légal contenant une virgule rend la ligne `topics=` ambiguë — low — cosmétique — rejet.
- Itération 2 — `mqtt.py` : ligne de log multi-règles (connexion/reconnexion) non affirmée en test multi-règles — medium — réel (lacune de vérification) — patch.

## Spec Change Log

- Itération 1 (2026-09-28) — Déclencheur : finding high des trois couches de revue (exécuté en preuve) — la réduction des souscriptions par contenance seule laisse des filtres en chevauchement partiel tous deux abonnés ; le broker MQTT livre un message une fois par souscription correspondante, donc deux notifications partent pour un seul événement (AC-1.2.2/NFR-3 violés). Amendé : la stratégie de couverture (Code Map `rules.py`/`mqtt.py`, tâches, notes) exige désormais un ensemble de souscriptions **deux à deux disjoints** couvrant l'union des topics des règles — deux filtres qui se chevauchent sont fusionnés en leur plus petit sur-ensemble MQTT (position par position : exacts égaux → exact, sinon `+` ; épuisement ou `#` → `#`), itéré jusqu'au point fixe ; l'évaluation des règles reste sur la liste complète. Ajouté : `+` doit accepter le niveau vide (sémantique MQTT 4.7.1, alignement avec la livraison broker), rejet des caractères de contrôle dans les topics, rejet des clés inconnues dans les entrées de règle, test de re-souscription après reconnexion (multi-règles), test pipeline avec faux broker livrant une fois par souscription correspondante. Known-bad évité : tout ensemble de souscriptions contenant deux filtres dont les langages s'intersectent. KEEP (doit survivre à la re-dérivation) : `Rule` frozen dataclass ; `find_rule` première-gagnante avec compte de correspondances ; `Event` immuable + `build_event` (avertissement multi-correspondances, log dédié pour les non-correspondances) ; validation config avec règle fautive citée (nom unique non vide, motif légal `#` dernier niveau, valeurs strippées, liste vide rejetée) ; conversion filtre → regex avec cache ; table de tests `topic_matches` ; tests config/matrix existants ; style (docstrings FR, `log_line`, constantes module) ; `home/#` couvre le parent `home` (cohérent partout).

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, chaque AC automatisable couverte.
