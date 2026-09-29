---
title: 'Story 3.2 — Règle réelle du bouton et validation terrain de SM-1'
type: 'feature'
created: '2026-09-29'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: bb9925c65f526fbc2b66fe7cf9269f9e4d34446c
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-3-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Le projet n'a encore prouvé SM-1 (notification en moins de 2 secondes) qu'avec des mocks : aucun YAML réel de production n'existe, la question ouverte « le bouton émet-il du JSON ou du brut » n'est pas tranchée, et aucune documentation ne permet d'opérer (ajouter une règle, mesurer la latence, tourner le token).

**Approach:** Livrer les artefacts d'exploitation réelle : exemple de YAML commenté avec la règle du bouton (topic réel et `payload_format` à confirmer par observation du broker réel), documentation d'exploitation complète (procédures, méthode de mesure de latence journald + relevé manuel, rotation du token), puis validation terrain sur la machine dédiée suivant ces procédures.

## Boundaries & Constraints

**Always:**
- Le `payload_format` de la règle du bouton est confirmé par observation des messages réels du broker (mosquitto_sub), pas décrété — la question ouverte de la spine est tranchée sur le terrain et le résultat consigné.
- La mesure SM-1 couvre la chaîne complète appui → affichage : timestamps journald (`mqtt message topic=` → `fcm sent topic=`, main.py:143 et notifier.py:214) pour réception MQTT → envoi FCM, complétés d'un relevé manuel appui → affichage ; la méthode est consignée avec le relevé.
- Aucune valeur réelle sensible (device token, service-account, topic réel si considéré privé) n'est versionnée dans le dépôt : l'exemple du dépôt ne contient que des placeholders clairement marqués, les valeurs réelles vivent dans le YAML de production sur la Pi (/etc/mqtt-notif/).
- La validation terrain (AC-3.2.4) : appuis, au moins un rechargement de YAML, broker qui ondule — aucune erreur fatale attendue.

**Never:**
- Aucune modification du démon : les logs existants suffisent à la mesure (réception main.py:143, envoi notifier.py:214, format ISO 8601 UTC `__init__.py:11-13`).
- Pas de durcissement systemd ni de permissions du service-account (reportés au déploiement, spec 3.1).
- Pas d'authentification broker/TLS, pas de multi-appareil.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Exemple YAML chargé | `config/mqtt-notif.yaml` lu par le loader | charge sans `ConfigError`, schéma conforme (clés exactes, payload_format json/raw) | Test automatisé |
| Placeholder secret oublié | device_token réel tapé dans l'exemple du dépôt | le test échoue (placeholder attendu, pas une valeur réelle) | Test automatisé |
| Payload du bouton = JSON | observation broker : JSON dict | `payload_format: json`, gabarits à placeholders pointés (`{action}`, …) | Doc : cas A de la procédure |
| Payload du bouton = brut | observation broker : texte non JSON | `payload_format: raw`, gabarits limités à `{payload}` (render.py:88-99) | Doc : cas B de la procédure |
| Latence mesurée | appuis en conditions calmes | réception→envoi lu dans journalctl, appui→affichage chronométré, total < 2 s, relevé consigné | Procédure documentée, exécution terrain |
| Doc périmée | règle ajoutée / schéma évolué | l'exemple reste validé par le test de chargement | Anti-rot par test |

</frozen-after-approval>

## Code Map

- `config/mqtt-notif.yaml` -- à créer (n'existe pas) : exemple réel commenté — `broker.host/port` (LAN, marqués à adapter), `device_token` (placeholder), une règle du bouton complète (`name`, `topic`, `payload_format` à confirmer sur le terrain, `title`, `short_message`, `detailed_message`, `debounce_ms`) ; schéma strict : clés admises `broker`, `device_token`, `rules` (config.py:285-293), clés de règle exactes `RULE_KEYS` (config.py:115-123).
- `docs/EXPLOITATION.md` -- à créer : procédures d'exploitation (voir tâches). Sources des faits à citer : rechargement à chaud polling 1 s (config.py:36-38, 341-393), token relu à chaque envoi — rotation sans redémarrage (notifier.py:269), broker.host/port interdits à chaud (config.py:361-372), logs exacts pour la mesure (`__init__.py:11-13`, `main.py:143`, `notifier.py:214`).
- `deploy/README.md` -- pointer une ligne vers `docs/EXPLOITATION.md` ; le fichier est cross-pinné par `Documentation=` (test_deploy.py:236-240) : ne pas le déplacer/renommer, édition de contenu neutre.
- `tests/test_config_example.py` -- à créer : charge l'exemple via le loader réel (pas une reparse YAML), assert placeholders secrets, assert charge sans `ConfigError`.
- Ne pas toucher : `mqtt_notif/` (aucun changement de code), `tests/test_deploy.py` (le nouveau `.py` de test n'est pas dans le paquet — la règle des chemins interdits ne s'applique qu'à `mqtt_notif/`, test_deploy.py:257-271).

## Tasks & Acceptance

**Execution:**
- [x] `config/mqtt-notif.yaml` -- créer l'exemple réel commenté (règle du bouton, placeholders secrets, commentaires des deux cas JSON/brut) -- AC-3.2.1 (support), AC-3.2.5.
- [x] `docs/EXPLOITATION.md` -- rédiger : (1) confirmer un topic/payload sur le broker réel (mosquitto_sub, décision json/brut), (2) ajouter/modifier une règle (éditer le YAML, rechargement 1 s, erreurs visibles), (3) mesurer la latence SM-1 (extraire les timestamps journald, chronométrage manuel, modèle de relevé à remplir), (4) tourner le device token (éditer le YAML, effet au prochain envoi), (5) checklist journée type (AC-3.2.4) -- AC-3.2.3, AC-3.2.4, AC-3.2.5.
- [x] `deploy/README.md` -- ajouter le pointeur vers `docs/EXPLOITATION.md` -- découverte, AC-3.2.5.
- [x] `tests/test_config_example.py` -- test de chargement de l'exemple + garde placeholders -- anti-rot de l'exemple et de la doc.

**Acceptance Criteria:**
- Given le dépôt, when Brfw9455 veut ajouter une règle, mesurer la latence ou tourner le token, then la procédure complète est dans `docs/EXPLOITATION.md` (exemple YAML réel commenté inclus), sans relire le code (AC-3.2.5).
- Given `config/mqtt-notif.yaml`, when chargé par le loader du démon, then il est valide et ne contient aucun secret réel (AC-3.2.1 support).
- Given le service installé et la règle du bouton configurée sur le YAML de production, when Brfw9455 appuie sur le bouton du salon, then la notification personnalisée s'affiche, écran verrouillé inclus (AC-3.2.2 — terrain).
- Given les timestamps journald réception→envoi et le relevé manuel appui→affichage, when la mesure est consignée avec sa méthode, then la chaîne complète tient sous 2 secondes en conditions calmes (AC-3.2.3 — terrain).
- Given une journée type d'exploitation (appuis, un rechargement au moins, broker qui ondule), when le jour s'écoule, then aucune erreur fatale et le silence hors événement réel (AC-3.2.4 — terrain).

## Implementation Notes

Décisions et surprises de l'implémentation (2026-09-29) :
- `payload_format: raw` livré dans l'exemple comme **hypothèse explicitement marquée** — le tranchage json/brut est terrain (section (1) de la doc) ; les deux cas sont documentés et testés.
- Corrections de références du Code Map vérifiées dans le code : relecture du token à `notifier.py:253` (pas 269), `POLL_INTERVAL_S` à `config.py:41` (pas 36-38). La doc cite les lignes vérifiées ; le démon n'a pas été modifié.
- Faits épinglés par vérification directe : logs `debug: message ignored` (main.py:110), `config file disappeared` (config.py:345), label `unregistered` mappé sur `UnregisteredError` (notifier.py:64), `pipeline error (restarting)` (main.py:205).
- Audit matrice (step-03) : lignes 3, 4 et 5 n'étaient couvertes par aucun test après le premier passage — ajout de deux tests dans `tests/test_config_example.py` : chargement des snippets cas A/B extraits de la doc via le loader réel, et épinglage de la procédure SM-1 (lignes journald exactes + relevé). Toutes les lignes de la matrice ont désormais un test couvrant, passé.
- Un échec unique et non reproduit (`1 failed, 331 passed`) sur un passage de la suite complète pendant la vérification ; huit passages complets consécutifs verts ensuite (332 passed). Le `lastfailed` cache mentionne des tests de `test_render.py`, non touchés par cette story.
- Terrain restant (exécution par Brfw9455 sur la Pi, cf. Verification) : observation broker → tranchage json/brut + topic réel, AC-3.2.2 (appui → téléphone, écran verrouillé), relevé SM-1 < 2 s, checklist journée type.
- Boucle de revue 1 : huit groupes patch appliqués (P1 alignment sprint-status fait par l'orchestrateur ; P2-P8 par le subagent d'implémentation). Le tour 2 du subagent s'est terminé sur une erreur de connexion (message incomplet) mais tout le travail était posé sur le disque — vérifié par audit direct de chaque fichier cible, 332 tests verts après patch. Rejets et détails : cf. Review Triage Log.

## Spec Change Log

## Review Triage Log

| # | Couche | Constat | Verdict | Preuve / routage |
|---|--------|---------|---------|------------------|
| 1 | blind | Références de ligne contradictoires dans la spec (frozen `notifier.py:269`, `config.py:36-38` vs valeurs corrigées) non signalées | false | Les corrections sont explicitement consignées dans Implementation Notes (« Corrections de références du Code Map vérifiées ») ; le bloc gelé est verrouillé par règle, la doc cite les lignes vérifiées. |
| 2 | blind | Statuts divergents : sprint-status `in-progress` vs spec `in-review` | low | Réel : les deux trackers désaccordent ; alignement direct (sprint-status → review). Patch. |
| 3 | blind | Assertions creuses dans `test_config_example_loads_through_real_loader` : `payload_format in (json, raw)` garanti par le loader, `assert config.rules` trivial ; l'hypothèse livrée (raw), le topic et les gabarits ne sont pas épinglés | low | Vérifié : lecture du test — une réécriture du corps de la règle passerait inaperçue ; renforcement direct. Patch. |
| 4 | blind | Pas de garde sur le topic de l'exemple : un topic réel tapé dans le dépôt passe toute la suite | low | Vérifié : aucun test n'épingle le topic d'exemple ; assertion directe. Patch (même racine que #3). |
| 5 | blind | Pas de procédure de suppression/retrait d'une règle dans la doc | low | Vérifié : §(2) couvre ajout/modification seulement ; le comportement existe et est sûr — resync au rechargement avec désabonnement des disparus et ligne `mqtt resync` (mqtt.py:46-55) ; paragraphe direct. Patch. |
| 6 | blind | Biais amont de la mesure SM-1 : temps de réaction humain dans les segments chronométrés, ligne « marge » du relevé sans guidance | low | Réel : la méthode manuelle est prescrite par l'AC gelée ; guidance de marge et mention de l'alternative à moindre biais = ajout direct. Patch (même racine que #7/#8/#15/#16). |
| 7 | blind | Pas de chemin d'échec pour le verdict SM-1 (> 2 s) : ni re-mesure, ni isolation du segment, ni consignation de l'échec | low | Réel : §(3) ne définit que le cas heureux ; ajout direct. Patch. |
| 8 | blind | Le relevé rempli n'a pas de lieu de consignation désigné — AC-3.2.3 invérifiable a posteriori | low | Réel : « consigné » sans emplacement ; décision : sur la Pi à côté du YAML de production (cohérent avec l'Always : les valeurs réelles vivent sur la Pi, pas dans le dépôt). Patch. |
| 9 | blind | Le schéma en prose (§(2) de la doc + bloc de commentaires de l'exemple) peut pourrir sans détection | low — rejet | Toute clé inconnue est rejetée au rechargement avec erreur explicite citant la clé (échec bruyant au moment de l'usage) ; le fix (cross-pin prose ↔ `RULE_KEYS`) dépasse la correction directe et rend le test cassant pour un cas non quotidien. |
| 10 | blind | Extraction de snippets de la doc fragile : regex sur toute la doc + paire ordonnée exacte `[json, raw]` — tout futur bloc yaml ailleurs casse le test pour une raison sans rapport | low | Vérifié : `re.findall` doc-wide, assertion d'ordre exact ; ancrage direct à la section (1). Patch (même constat pré-vérifié par la couche verif). |
| 11 | blind | Topics d'exemple incohérents : `home/salon/bouton` dans l'exemple, `home/salon/bouton/etat` dans les snippets de la doc | low | Vérifié : incohérence réelle entre artefacts ; un copié-collé sans §(1) donne un topic muet ; alignement direct. Patch. |
| 12 | blind | Catalogue d'erreurs opérateur incomplet : `fcm send retry`, `pipeline error (restarting)` non expliqués | low | Partiellement faux : `debug: message ignored` est expliqué §(5), `config file disappeared` §(2), `unregistered` §(4) ; réel pour retry/pipeline ; catalogue compact direct. Patch (regroupé avec #13/#14). |
| 13 | edge | `mosquitto_sub` muet après plusieurs appuis (mauvais préfixe, bouton mort, broker injoignable) : pas de repli documenté | low | Réel : §(1) n'a pas de chemin d'échec ; étape directe (élargir à `#`, vérifier bouton/broker). Patch. |
| 14 | edge | Payload JSON non-objet (tableau, scalaire, chaîne quotée) tombe entre les cas A et B de §(1) | low | Vérifié : mode json rendrait tout placeholder `[absent]` (main.py:68-74) ; clarification directe : JSON non-dict → cas B (raw, `{payload}`). Patch. |
| 15 | edge | Échec d'envoi pendant la mesure : pas de ligne `fcm sent topic=`, pas de garde | low | Réel : l'opérateur attendrait ou apparierait mal ; garde directe (chercher `fcm send failed`, rejeter la mesure). Patch (même racine que #7). |
| 16 | edge | Appuis multiples dans la fenêtre journald : appariement réception→envoi non spécifié | low | Réel : appariement croisé fausse le segment interne ; consigne directe (un appui par mesure). Patch (même racine que #7). |
| 17 | edge | Rotation de token échouée (faute de frappe) sans guidage de reprise | false | §(4) documente déjà le symptôme (`fcm send failed: unregistered`) comme indicateur d'une rotation à faire ; l'échec est bruyant et la reprise (recopier le token depuis l'app) est la procédure de rotation elle-même. |
| 18 | edge | Commentaire de l'exemple inexact : « detailed_message est optionnel, tout le reste est requis » alors que `payload_format` (défaut json) et `debounce_ms` (défaut 0) sont aussi optionnels | low | Vérifié : config.py:137 et :144 ; correction directe du commentaire. Patch. |
| 19 | edge | Statuts sprint vs spec divergents | carried | Dupliquat de #2, même racine. |
| 20 | verif | Aucun gap de vérification : surfaces comportementales couvertes par des tests passés (4 puis 332), écart d'adoption et supersession vérifiés | — | Confirmation, pas un finding ; les claims factuels de la doc ont tous été vérifiés contre le code. |
| 21 | verif | Autre finding : extraction doc-wide fragile dans `test_exploitation_doc_rule_snippets_load` | carried | Dupliquat de #10 (constat pré-vérifié par la couche verif). |

**Groupes et routage** (boucle 1) : tous les findings vérifiés sont `low` — aucun `intent_gap`, aucun `bad_spec`, pas de loopback. Groupes patch : P1 (#2/#19 — alignement sprint-status → review, fait par l'orchestrateur), P2 (#3/#4 — épinglage renforcé du test exemple), P3 (#10/#21 — extraction ancrée à la section (1)), P4 (#11 — alignement des topics d'exemple), P5 (#18 — commentaire schéma exact), P6 (#5 — procédure de retrait de règle), P7 (#6/#7/#8/#15/#16 — §(3) : garde d'échec, appariement, guidance marge, chemin d'échec du verdict, lieu du relevé), P8 (#12/#13/#14 — arêtes de décision §(1) + catalogue compact de signatures d'erreurs). Rejets : #1, #9, #17.

## Design Notes

- Choix d'emplacement (non perceptible pour l'usage) : la doc d'exploitation vit dans `docs/EXPLOITATION.md`, distincte de `deploy/README.md` (installation) — audiences différentes ; `deploy/README.md` reste à sa place (cross-pin `Documentation=`).
- La mesure journald couvre réception MQTT → envoi FCM ; l'appui → réception MQTT et l'envoi → affichage ne sont pas journalisés : le relevé manuel chronométré comble ces deux segments, la méthode (deux mesures, addition, marge) est consignée dans le relevé — c'est la méthode que l'AC prescrit.
- L'exemple du dépôt ne peut pas contenir les valeurs réelles (secrets, et le topic réel n'est connu qu'après observation terrain) : la règle du bouton y est donc un gabarit à compléter sur la Pi, la procédure de complétion étant la section (1) de la doc.
- Le test de l'exemple passe par le loader réel (`mqtt_notif.config`) plutôt qu'une simple lecture YAML : il épingle la conformité au schéma strict (clés inconnues rejetées), pas seulement la syntaxe.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, y compris le chargement de l'exemple et la garde placeholders.

**Manual checks (if no CLI):**
- AC-3.2.1 : sur la Pi, observer le broker réel (mosquitto_sub sur le topic du bouton), trancher JSON/brut, compléter le YAML de production.
- AC-3.2.2 : appui réel → notification sur le téléphone, écran verrouillé inclus.
- AC-3.2.3 : mesure suivant la procédure (3) de `docs/EXPLOITATION.md`, relevé rempli, chaîne complète < 2 s.
- AC-3.2.4 : journée type suivant la checklist (5), journald relu, aucune erreur fatale.
