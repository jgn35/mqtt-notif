---
review-date: 2026-09-28
reviewer-role: rubric walker (checklist good-spine)
target: ../ARCHITECTURE-SPINE.md
verdict: pass
---

# Revue — ARCHITECTURE-SPINE.md (mqtt-notif) contre la checklist good-spine

Document relu : `_bmad-output/planning-artifacts/architecture/architecture-mqtt-notif-2026-09-28/ARCHITECTURE-SPINE.md`
Spéc pilote : `_bmad-output/planning-artifacts/prds/prd-mqtt-notif-2026-09-28/prd.md` (+ `addendum.md`)
Verdict global : **PASS** — 5 findings (1 medium, 3 low, 1 info), aucun bloquant pour la descente au niveau en dessous.

## Checklist, point par point

### 1. Les vrais points de divergence du niveau en dessous — tous fixés ?

**Conforme, avec un écart (F1).**

Les unités du niveau en dessous sont les modules du seed (`main`, `config`, `mqtt`, `rules`, `render`, `debounce`, `notifier`). Les divergences classiques entre ces unités sont toutes couvertes :

- Appels croisés entre étapes / contournement du flux → AD-1 (pipeline unidirectionnel, ordre imposé).
- Modèle de concurrence (threads vs asyncio, firebase-admin bloquant) → AD-2.
- Double lecture du YAML (pipeline vs reloader), config à moitié chargée → AD-3.
- Règles à motifs qui se chevauchent, multi-notification → AD-4 (première gagnante, SM-C1).
- Moteurs de gabarit divergents → AD-5 (syntaxe unique).
- Court-circuit de l'émetteur, envoi hors limites, politique d'erreurs FCM par module → AD-6.
- Debounce implémenté à deux endroits (règle vs émetteur) → AD-7.
- Déploiement conteneur vs natif, chemins non reproductibles → AD-8.
- État volatile vs persistant « au cas où » → AD-9.

**Écart F1 (medium) : la coordination config → source MQTT sur rechargement à chaud n'est ni décidée, ni différée, ni en question ouverte.** FR-7 impose qu'un YAML modifié prenne effet sur l'événement suivant ; FR-1 impose la souscription à l'ensemble des topics des règles. L'interaction des deux — une nouvelle règle ajoutée par rechargement introduit un nouveau topic, qui doit être souscrit sans redémarrage — n'est couverte par aucune AD. La spine dit que les étapes lisent le ConfigStore mais ne dit pas comment `mqtt.py` apprend que l'ensemble de topics a changé (re-souscription à chaque swap ? diff des topics ? notification du watcher ?). C'est précisément le genre de contrat inter-unités qu'un règle d'architecture doit fixer : `config.py` et `mqtt.py` construits séparément peuvent diverger (l'un suppose re-souscription automatique au swap, l'autre souscription unique au démarrage). À trancher avant le build — une phrase dans AD-3 suffirait.

### 2. Chaque Rule applicable, vérifiable, et prévient-elle réellement la divergence visée ?

**Conforme, avec une précision à apporter (F2).**

Chaque règle a un `Prevents` explicite et une `Rule` vérifiable par inspection du code ou par test :

| AD | Vérifiable ? | Prévient réellement ? |
| --- | --- | --- |
| AD-1 | Oui — inspection : appel uniquement à l'étape aval | Oui — interdit appels croisés et écritures latérales |
| AD-2 | Oui — grep threads/locks, `asyncio.to_thread` | Oui — un seul modèle de concurrence |
| AD-3 | Oui — un seul lecteur fichier, validation avant swap | Oui — atomique, précédent conservé si invalide |
| AD-4 | Oui — test : message sur motifs chevauchants → 1 notification | Oui — première gagnante bornée à SM-C1 |
| AD-5 | Partiellement (F2) | Oui pour la divergence « plusieurs moteurs de gabarit » |
| AD-6 | Oui — test des limites octets, mapping des codes d'erreur | Oui — rédacteur unique, jamais corrompu |
| AD-7 | Oui — test fenêtre par règle | Oui — un seul propriétaire du debounce |
| AD-8 | Oui — l'unité systemd existe, pas de Dockerfile | Oui — dimensionnement déploiement figé |
| AD-9 | Oui — aucun fichier/DB dans le code | Oui — mondes volatile/persistant incompatibles écartés |

Petites remarques de cohérence interne, sans gravité :

- AD-4 : « si plusieurs règles correspondent, un avertissement est journalisé » — avec la sémantique première-gagnante, tout message matché par un motif générique puis une règle plus spécifique plus bas déclencherait l'avertissement à chaque événement. Le comportement est vérifiable mais le bruit de journal potentiel n'est pas anticipé ; clarification souhaitable (p.ex. avertissement seulement au chargement, quand deux motifs se chevauchent).
- AD-6 : les noms d'erreurs (`unregistered`, `invalid_argument`, `permission_denied`, `throttled`, `internal`, `network_error`) reprennent le mapping de l'addendum mais sans le préfixe `fcm_` de la source. Pas de divergence de comportement, mais la traceabilité avec l'addendum gagnerait à être explicite (et à nommer les exceptions concrètes de firebase-admin 7.x au niveau story).

**F2 (low) : la syntaxe AD-5 est donnée par deux exemples, pas par une grammaire.** Pointé par AD-5, un builder différent ne peut pas réinventer le moteur ; en revanche, au sein même du rendu, il manque : la règle d'échappement d'un `{` littéral, le comportement pour un chemin dans un tableau (`{capteurs.0.lieu}` ?), et le traitement des valeurs non-chaîne (nombres, booléens — substitution probable, à l'écrire). Risque de divergence entre la documentation du YAML, le validateur de `config.py` et `render.py` si plusieurs stories touchent le rendu. Une grammaire de dix lignes dans AD-5 clôt le point.

### 3. Rien sous Deferred ne pourrait laisser deux unités diverger ?

**Conforme.**

Examen de chaque item :

- Arborescence fine au-delà du seed — découpage interne d'une unité, pas inter-unités. OK.
- Stratégie de test — pas d'impact sur les contrats entre modules. OK.
- Constantes (backoff max, retries, période mtime, textes de log) — réglages, les contrats restent fixes. OK.
- Permissions service-account / durcissement systemd — au déploiement, après assemblage. OK.
- Auth broker / TLS — exclu du PRD v1 ; les unités n'ont aucune raison de diverger puisque personne n'implémente. OK.
- Multi-appareil, interface web, historique/regroupement — exclus du PRD v1. OK.

Aucun item différé ne porte un contrat entre deux unités. En revanche, les points silencieux (F1 ci-dessus, F3 et F4 ci-dessous) ne figurent pas non plus dans Deferred — c'est précisément le problème : ils ne sont nulle part.

### 4. Technologies d'actualité ?

**Conforme (non revérifié indépendamment — F5).**

Les versions sont cohérentes entre elles et avec l'environnement cible : Python 3.13 est bien le Python de Trixie (Debian 13) ; aiomqtt 2.5.1 imposer paho-mqtt 2.x est plausible et la dépendance transitive est explicitée ; firebase-admin 7.5.0 et PyYAML 6.0.3 n'ont rien d'anormal ; l'auteur indique avoir vérifié ces versions le 2026-09-28. Je n'ai pas de connecteur web dans cette session pour revérifier moi-même ; je prends la vérification de l'auteur comme donnée. Cohérence interne Stack ↔ Open Questions (Python 3.13 sur la machine dédiée est listé comme hypothèse, à bon escient).

### 5. La spine couvre-t-elle les capacités du spec pilote ?

**Conforme.**

Table de couverture vérifiée ligne à ligne contre le Capability → Architecture Map :

| Capacité | Couverture |
| --- | --- |
| FR-1 (connexion, souscription, reconnexion QoS 0) | Oui — `mqtt.py`, AD-1/2/8 ; reconnexion backoff dans AD-8 |
| FR-2 (filtrage topic/motif) | Oui — `rules.py`, AD-4 |
| FR-3 (définition des règles YAML) | Oui — `config.py`, AD-3 ; message détaillé optionnel dans AD-6 |
| FR-4 (personnalisation par type d'événement) | Oui — `render.py`, AD-5 |
| FR-5 (envoi FCM direct data-only) | Oui — `notifier.py`, AD-6 |
| FR-6 (device token depuis la config) | Oui — `config.py`/`notifier.py`, AD-3/6/8 ; token absent → erreur, service continue (AD-6) |
| FR-7 (rechargement sans redémarrage) | Oui — `config.py`, AD-3 ; valide SM-2 |
| FR-8 (debounce par règle) | Oui — `debounce.py`, AD-7 |
| SM-1 (latence < 2 s) | Mappée AD-1/2 ; voir réserve ci-dessous |
| SM-2 (YAML effectif sans redémarrage) | Oui — AD-3 |
| SM-C1 (au plus une notification) | Oui — AD-4 |

Aucune capacité, conséquence testable ou métrique du PRD n'est orpheline. Les hypothèses du PRD (contrat mcp-notif, broker sans auth, latence 2 s) sont reprises dans AD-6, Deferred et Open Questions respectivement.

Réserve mineure sur SM-1 : la latence est « mappée » au pipeline entier mais aucune règle ne borne le travail par étape (le pire risque réaliste est un appel firebase-admin lent dans `to_thread` sans timeout d'envoi). À cette altitude ce n'est pas exigible — SM-1 est une métrique runtime, pas un contrat structurel — mais un timeout d'envoi borné serait un candidat naturel aux constantes différées, mentionné pour mémoire.

### 6. Toute dimension de cette altitude est-elle décidée, différée ou en question ouverte ?

**Conforme sur l'essentiel ; trois dimensions silencieuses (F1 déjà vu, F3, F4).**

L'enveloppe opérationnelle, souvent laissée silencieuse, est ici bien traitée :

- Déploiement : décidé (AD-8 — systemd, venv dédié, pas de conteneur, pas de base).
- Environnements : un seul environnement cible (machine dédiée), hypothèse explicite dans Open Questions ; stratégie de test différée à raison.
- Infra : décidé (broker Mosquitto LAN fermé, QoS 0, FCM en HTTPS, diagramme de déploiement).
- Opérations : décidé — reconnexion avec backoff, logs stdout/journald avec le contenu journalisé par événement, aucune erreur fatale après démarrage (Conventions + AD-6/AD-8).
- Persistance/données : décidé (AD-9).
- Sécurité : en partie différé à raison (permissions fichiers, durcissement), en partie exclu du PRD (TLS) ; les deux sont explicités.
- Configuration/secrets : décidé dans les grandes lignes, avec une vraie question ouverte (F4 ci-dessous).

Dimensions silencieuses :

**F3 (low) : fenêtres de debounce vs rechargement de configuration.** AD-7 précise le comportement au redémarrage via AD-9 (fenêtres réinitialisées), mais pas lors d'un swap à chaud : une règle dont le délai change, ou une règle supprimée puis recréée, doit-elle repartir avec une fenêtre vierge ou conserver l'horodatage ? `debounce.py` et `config.py` construits séparément peuvent diverger (clé de fenêtre = nom de règle ? identité stable ?). Une phrase dans AD-7 ou AD-3 clôt le point.

**F4 (low) : tension mineure entre le Structural Seed et AD-8 sur la localisation de la configuration.** AD-8 impose que le chemin du fichier de règles et celui du service-account soient passés par variables d'environnement via l'unité systemd ; le seed montre en parallèle `config/mqtt-notif.yaml` à un chemin fixe dans l'arborescence du dépôt, et le PRD liste le broker, le token et les règles dans « un fichier YAML unique ». L'intention est lisible (le seed montre le fichier d'exemple, l'unité passe le chemin), mais un builder peut légitimement lire « le code lit `config/mqtt-notif.yaml` en dur ». De plus, la question ouverte « un seul YAML ou séparer les secrets » (token + service-account dans deux fichiers à l'heure actuelle) est correctement identifiée comme à trancher avant le build — c'est le bon état ; le rendrait-on résolu en alignant le seed sur AD-8 et en disant si `mqtt-notif.yaml` est un exemple versionné ou un artefact de la machine cible.

Arrêt propre (SIGTERM, `main.py` mentionne « signals » sans règle) : jugé sous le seuil du finding — mono-processus, QoS 0, perte assumée par AD-9 ; aucun risque de divergence inter-unités.

## Findings récapitulés

| # | Sévérité | Finding | Recommandation |
| --- | --- | --- | --- |
| F1 | Medium | Coordination config → MQTT sur rechargement à chaud (re-souscription des nouveaux topics) : ni règle, ni différé, ni question ouverte | Trancher avant le build ; une phrase dans AD-3 suffit |
| F2 | Low | Syntaxe de substitution AD-5 sans grammaire (échappement de `{`, chemins tableau, valeurs non-chaîne) | Ajouter ~10 lignes de grammaire à AD-5 |
| F3 | Low | Comportement des fenêtres de debounce lors d'un swap de config à chaud non spécifié | Une phrase dans AD-7 ou AD-3 |
| F4 | Low | Tension seed (`config/mqtt-notif.yaml` chemin fixe) vs AD-8 (chemin par variable d'environnement) ; statut exemple vs artefact | Aligner le seed sur AD-8 ; la question ouverte secrets est correctement posée |
| F5 | Info | Versions de la Stack non revérifiées indépendamment (vérification de l'auteur du 2026-09-28 prise comme donnée) ; cohérence interne OK | Néant |

## Verdict

**PASS.** La spine fixe les vrais points de divergence du niveau module avec des règles vérifiables, couvre l'intégralité des capacités du PRD (FR-1 à FR-8, SM-1, SM-2, SM-C1), traite explicitement l'enveloppe opérationnelle (AD-8, AD-9) et ne diffère rien qui pourrait laisser deux unités diverger. Les cinq findings sont des compléments de spécification, aucun n'invalide le paradigme ni la descente vers stories/epics ; F1 devrait néanmoins être tranché avant le build, et la question ouverte sur le format du fichier de configuration l'exige déjà.
