---
stepsCompleted:
  - step-01-validate-prerequisites
  - step-02-design-epics
  - step-03-create-stories
  - step-04-final-validation
inputDocuments:
  - _bmad-output/planning-artifacts/prds/prd-mqtt-notif-2026-09-28/prd.md
  - _bmad-output/planning-artifacts/prds/prd-mqtt-notif-2026-09-28/addendum.md
  - _bmad-output/planning-artifacts/architecture/architecture-mqtt-notif-2026-09-28/ARCHITECTURE-SPINE.md
  - _bmad-output/planning-artifacts/architecture/architecture-mqtt-notif-2026-09-28/SOLUTION-DESIGN.md
---

# mqtt-notif — Découpage en épopées et stories

## Vue d'ensemble

Ce document fournit le découpage complet en épopées et stories pour **mqtt-notif**, décomposant les exigences du PRD et de l'architecture en stories implémentables avec critères d'acceptation.

## Inventaire des exigences

### Exigences fonctionnelles

FR-1 : Le système peut se connecter au Broker MQTT et souscrire, en QoS 0, à l'ensemble des topics définis par les Règles. Reconnexion automatique après déconnexion avec backoff croissant ; une ligne de journal par connexion, déconnexion et reconnexion.
FR-2 : Le système peut souscrire à un topic exact ou à un motif MQTT (`+`/`#`) selon la Règle. Un message publié sur un topic ne correspondant à aucune Règle ne déclenche aucune notification.
FR-3 : L'utilisateur peut définir ses Règles dans un fichier YAML : topic (ou motif) + modèle de message (titre, message court, message détaillé optionnel). Chaque Règle produit un message distinct ; message détaillé optionnel ; un champ référencé absent produit un texte de remplacement explicite plutôt qu'une erreur.
FR-4 : Le système peut produire une notification dont le contenu dépend du type d'événement et des champs du payload MQTT. Deux types d'événements différents sur le même broker produisent des notifications au contenu différent.
FR-5 : Le système peut envoyer une notification FCM *data-only* à partir d'une Règle déclenchée (clés `title`, `short_message`, `detailed_message`). La notification respecte les limites de taille du contrat de l'app ; un message hors limites est tronqué ou rejeté proprement, jamais envoyé corrompu.
FR-6 : Le système peut lire le device token du téléphone dans le fichier de configuration. Token absent ou invalide (`fcm_unregistered`) : événement journalisé avec erreur explicite, le service continue ; rotation du token = mise à jour manuelle du fichier.
FR-7 : Le système peut appliquer une modification du fichier de Règles sans redémarrer. Une modification du YAML prend effet sur l'événement suivant ; un YAML invalide est rejeté, les Règles précédentes restent actives et l'erreur est journalisée.
FR-8 : Le système peut appliquer, par Règle, un délai minimum configurable entre deux notifications. Messages reçus pendant le délai de la Règle : pas de notification ; une Règle sans délai configuré notifie chaque message.

### Exigences non fonctionnelles

NFR-1 (SM-1, performance) : chaque événement MQTT déclencheur aboutit à une notification sur le téléphone en moins de 2 secondes en conditions normales. Valide FR-1, FR-5.
NFR-2 (SM-2, évolutivité) : un changement du YAML prend effet sans redémarrage. Valide FR-7.
NFR-3 (SM-C1, fiabilité, contre-métrique — ne pas optimiser au-delà) : aucun événement ne déclenche plus d'une notification ; le silence reste la norme hors événement réel. Valide FR-2, FR-4.
NFR-4 (périmètre) : strictement mono-utilisateur, mono-appareil ; réseau domestique fermé ; pas d'authentification broker ni TLS en v1 ; pas d'interface web ni d'API de gestion (le YAML est la seule surface de configuration).
NFR-5 (exploitabilité) : service résident au démarrage de la machine (systemd, `Restart=on-failure`) ; logs plats sur stdout confiés à journald ; aucune erreur d'envoi n'arrête le service après le démarrage.

### Exigences additionnelles (architecture)

- AD-1 : pipeline unidirectionnel — les étapes communiquent uniquement par passage de valeur à l'étape suivante, dans l'ordre source MQTT → sélection de règle → rendu → debounce → émetteur FCM ; aucune étape n'appelle une étape non adjacente.
- AD-2 : boucle d'événements asyncio unique ; appels bloquants (firebase-admin, lecture fichier) via `asyncio.to_thread` ; aucun thread applicatif ; debounce = `asyncio.sleep`.
- AD-3 : ConfigStore unique, swap atomique — validation intégrale (schéma + limites + syntaxe des gabarits) avant remplacement ; YAML invalide = règles précédentes conservées + erreur journalisée ; seule la source MQTT resynchronise ses souscriptions au swap (abonnement des nouveaux topics, désabonnement des disparus).
- AD-4 : première règle gagnante — évaluation dans l'ordre de déclaration, première correspondance (exacte ou motif `+`/`#`) traite le message ; plusieurs correspondances → avertissement journalisé ; aucune → message ignoré (debug).
- AD-5 : syntaxe de substitution unique — `{champ}` et `{chemin.pointé}` en mode `json` (défaut), `{payload}` seul en mode `raw` ; `{{` → `{` littéral ; champ absent → `[absent]`, jamais d'exception ; payload JSON non parsable → rendu avec `[absent]` + avertissement, jamais ignoré en bloc ; aucun filtre ni logique scriptée ; gabarits validés au chargement de la config.
- AD-6 : frontière FCM à rédacteur unique — seul le module émetteur construit et envoie (data-only) ; validation avant tout appel : `title` et `short_message` non vides, ≤ 100 / ≤ 500 / ≤ 3 500 octets UTF-8 ; hors limites → tronqué proprement ; adressage par device token, jamais par `fid` ; erreurs FCM : `unregistered`/`invalid_argument`/`permission_denied` → journalisées sans retry ; `throttled`/`internal`/`network_error` → retry avec backoff borné.
- AD-7 : debounce en mémoire, propriété du pipeline — fenêtre ouverte à la réception (pas à l'envoi), clé = nom unique de la règle ; au swap, purge des fenêtres des règles disparues, persistance de celles conservées ; état jamais persisté ni partagé.
- AD-8 : enveloppe opérationnelle — service systemd (`Restart=on-failure`), venv Python 3.13, logs stdout/journald ; chemins du YAML des règles et du service-account FCM passés en variables d'environnement ; pas de conteneur, pas de base de données.
- AD-9 : aucune persistance, perte assumée — souscription QoS 0, pas de rejeu ; un redémarrage réinitialise les fenêtres de debounce.
- AD-10 : enveloppe inter-étapes fixe — objet événement unique immuable : topic (str), payload (dict parsé pour une règle `json`, str pour une règle `raw`), règle retenue figée au moment de la sélection.
- AD-11 : envois FCM découplés de la file — chaque envoi (avec ses retries) s'exécute comme tâche asyncio indépendante ; la file n'attend jamais la fin d'un envoi ; ordre d'affichage sur le téléphone non garanti (accepté).
- Conventions : paquet `mqtt_notif` en `snake_case` ; topics MQTT en minuscules ; clés YAML en `snake_case` ; `debounce_ms` en millisecondes, bornes [0, 3 600 000] (0 = pas de debounce) ; noms de règles uniques ; timestamps de journal en ISO 8601 UTC.
- Stack imposée : Python 3.13, aiomqtt 2.5.1 (paho-mqtt 2.1.0 transitif), firebase-admin 7.7.0, PyYAML 6.0.3, Raspberry Pi OS Trixie.
- Seed structurel : `main.py` (assemblage, boucle, signals), `config.py` (ConfigStore), `mqtt.py` (source), `rules.py` (sélection), `render.py` (rendu), `debounce.py` (fenêtres), `notifier.py` (frontière FCM), `deploy/mqtt-notif.service`.
- Pas de starter template externe : projet greenfield, le seed structurel de la spine sert de point de départ (Epic 1, Story 1).

### Exigences UX Design

Aucune — le projet n'a pas d'interface utilisateur (PRD §5) ; l'affichage appartient à l'app mcp-notif existante, non modifiée.

### Carte de couverture des FR

FR-1 : Épopée 1 — connexion, souscription QoS 0, reconnexion avec backoff croissant, journaux de cycle de vie
FR-2 : Épopée 1 — filtrage par topic exact ou motif `+`/`#`, première règle gagnante
FR-3 : Épopée 1 — définition des règles en YAML, gabarits, texte de remplacement pour champ absent
FR-4 : Épopée 1 — personnalisation du contenu par type d'événement et champs du payload
FR-5 : Épopée 1 — envoi FCM data-only conforme au contrat de l'app mcp-notif (limites, troncature propre)
FR-6 : Épopée 1 — device token lu dans la config, erreurs journalisées sans arrêter le service
FR-7 : Épopée 2 — rechargement à chaud, swap atomique, YAML invalide rejeté
FR-8 : Épopée 2 — debounce par règle en mémoire, purge au swap

NFR-1 (SM-1, latence < 2 s) : transversale, portée par l'Épopée 1 (pipeline complet, envois découplés)
NFR-2 (SM-2, YAML effectif sans redémarrage) : Épopée 2
NFR-3 (SM-C1, au plus une notification par événement) : Épopée 1 (AD-4, première règle gagnante)
NFR-4 (périmètre) : transversale, respectée par toutes les épopées
NFR-5 (exploitation systemd/journald) : Épopée 3

## Liste des épopées

### Épopée 1 : Première notification de bout en bout
Brfw9455 appuie sur le bouton du salon ; la notification personnalisée s'affiche sur son téléphone en moins de 2 secondes (UJ-1). Socle greenfield : pipeline complet source MQTT → sélection de règle → rendu → émetteur FCM, piloté par le YAML, résilient aux erreurs.
**FR couvertes :** FR-1, FR-2, FR-3, FR-4, FR-5, FR-6

### Épopée 2 : Piloter sans redémarrer
Brfw9455 édite le YAML (messages, debounce, nouvelles règles) ; le changement prend effet à l'événement suivant, sans interruption, et le spam du bouton est silencié par le debounce (UJ-2).
**FR couvertes :** FR-7, FR-8

### Épopée 3 : Vivre sur la machine dédiée
Le service démarre au boot de la Raspberry Pi, survit à toutes les erreurs et se pilote depuis journald : unité systemd, venv, chemins en variables d'environnement, règle initiale réelle pour le bouton.
**FR couvertes :** aucune directement — couvre NFR-5, l'exigence MVP « service résident » (AD-8)

## Épopée 1 : Première notification de bout en bout

Brfw9455 appuie sur le bouton du salon ; la notification personnalisée s'affiche sur son téléphone en moins de 2 secondes (UJ-1). Socle greenfield : pipeline complet source MQTT → sélection de règle → rendu → émetteur FCM, piloté par le YAML, résilient aux erreurs. Stories ordonnées « tranche fine d'abord » : la première est livrable seule.

### Story 1.1 : Tranche fine — du message MQTT à la notification FCM

En tant que **propriétaire du réseau domotique**,
je veux qu'un message MQTT publié sur le topic configuré déclenche l'envoi d'une notification FCM à mon téléphone,
pour que la chaîne complète broker → démon → FCM existe et se vérifie dès la première story.

**Périmètre technique :** pyproject (Python 3.13, aiomqtt 2.5.1, firebase-admin 7.7.0, PyYAML 6.0.3) ; paquet `mqtt_notif` avec `main.py` (assemblage, boucle asyncio unique, signals), `config.py` (lecture YAML minimale : `broker.host`, `broker.port`, `device_token`, topic d'écoute unique), `mqtt.py` (connexion, souscription QoS 0, reconnexion), `notifier.py` (envoi data-only à message fixe). Appels bloquants firebase-admin via `asyncio.to_thread` (AD-2) ; chaque envoi FCM en tâche asyncio indépendante (AD-11) ; envoi à message fixe, clés `title`/`short_message` (AD-6 simplifiée, consolidation en Story 1.4).

**Critères d'acceptation :**

**AC-1.1.1** — Démarrage et connexion
**Given** un YAML valide (broker joignable, topic, device_token renseigné)
**When** le démon démarre
**Then** il se connecte au broker, souscrit au topic en QoS 0 et journalise une ligne de connexion (timestamp ISO 8601 UTC).

**AC-1.1.2** — Notification de bout en bout
**Given** le démon connecté et le téléphone enrôlé
**When** un message est publié sur le topic souscrit
**Then** un message FCM *data-only* avec les clés `title` et `short_message` fixes est envoyé et s'affiche sur le téléphone (test automatisé sur le payload envoyé via FCM mocké ; vérification manuelle de l'affichage).

**AC-1.1.3** — Reconnexion automatique
**Given** le démon connecté
**When** le broker devient injoignable puis revient
**Then** le démon se reconnecte automatiquement avec un backoff croissant, journalise chaque déconnexion et reconnexion, et ne s'arrête jamais de lui-même.

**AC-1.1.4** — Erreur de token non fatale
**Given** un device_token absent ou refusé par FCM (`fcm_unregistered`)
**When** un message MQTT arrive
**Then** l'erreur est journalisée de façon explicite et le service continue de tourner — les messages suivants sont traités normalement.

**AC-1.1.5** — Envoi découplé
**Given** un envoi FCM lent (réseau perturbé, mock qui traîne)
**When** d'autres messages MQTT arrivent pendant l'envoi
**Then** ils sont traités sans attendre la fin de l'envoi — chaque envoi est une tâche asyncio indépendante, les appels bloquants firebase-admin passent par `asyncio.to_thread`, aucun thread applicatif.

**AC-1.1.6** — Arrêt propre
**Given** le démon en cours d'exécution
**When** un SIGTERM est reçu
**Then** il se déconnecte proprement du broker et termine avec le code 0 (prépare `Restart=on-failure` de l'Épopée 3).

**AC-1.1.7** — Échec rapide au démarrage
**Given** un YAML absent, illisible ou manquant une clé obligatoire
**When** le démon démarre
**Then** il affiche une erreur explicite et termine avec un code non nul — jamais de démarrage à moitié configuré (les erreurs ne sont jamais fatales qu'avant le démarrage).

**Vérification :** chaque AC testable avec broker et FCM mockés (pytest), sauf l'affichage réel du téléphone (AC-1.1.2, vérification manuelle).

### Story 1.2 : Règles YAML et sélection première-gagnante

En tant que **bricoleur**,
je veux définir mes règles dans le YAML — topic exact ou motif `+`/`#` — pour que seuls les événements qui m'intéressent déclenchent une notification, chacun avec sa propre identité,
pour que le fichier devienne la seule surface de pilotage, sans rien redéployer.

**Périmètre technique :** extension de `config.py` (section `rules` : nom unique, topic ; validation au chargement), nouveau `rules.py` (correspondance première-gagnante, motifs `+`/`#`, AD-4), `mqtt.py` souscrit désormais à l'ensemble des topics des règles (le topic unique de la Story 1.1 disparaît), objet événement immuable `topic` + `payload brut` + `règle figée` (AD-10, le parsing par `payload_format` arrive en Story 1.3). Contenu de notification toujours fixe tant que le rendu n'existe pas.

**Critères d'acceptation :**

**AC-1.2.1** — Schéma des règles
**Given** un YAML dont une règle manque le topic, porte un nom vide, ou duplique le nom d'une autre règle
**When** le démon démarre
**Then** démarrage refusé avec erreur explicite citant la règle fautive (échec rapide, même politique que AC-1.1.7).

**AC-1.2.2** — Souscription à l'ensemble des règles
**Given** plusieurs règles avec topics distincts dans le YAML
**When** le démon démarre
**Then** il souscrit en QoS 0 à tous les topics des règles, et un message reçu déclenche au plus une notification.

**AC-1.2.3** — Première règle gagnante
**Given** deux règles dont les motifs couvrent le même message, déclarées dans cet ordre
**When** le message arrive
**Then** seule la première règle traite le message et un avertissement est journalisé ; la seconde est ignorée.

**AC-1.2.4** — Correspondance par motif
**Given** une règle avec un motif `+` ou `#` (ex. `home/+/bouton`)
**When** un message est publié sur un topic couvert par le motif
**Then** la règle correspond et traite le message ; un topic non couvert ne correspond pas.

**AC-1.2.5** — Silence hors périmètre
**Given** un message publié sur un topic ne correspondant à aucune règle
**When** il est reçu
**Then** aucune notification n'est envoyée et le message est journalisé en debug.

**AC-1.2.6** — Événement figé en traversée
**Given** un message en cours de traitement par le pipeline
**When** la configuration est modifiée pendant la traversée
**Then** le message continue d'être traité avec la règle figée au moment de la sélection — l'objet événement n'est jamais modifié ni relu depuis le ConfigStore.

### Story 1.3 : Rendu des gabarits personnalisés

En tant que **bricoleur**,
je veux écrire le titre et les messages de chaque règle comme des gabarits qui référencent les champs du payload MQTT,
pour que chaque type d'événement affiche son propre message, personnalisé, sans jamais toucher au code.

**Périmètre technique :** nouveau `render.py` (substitution, AD-5) ; `config.py` gagne `payload_format` par règle (`json` défaut, `raw`) et la validation de syntaxe des gabarits au chargement ; l'objet événement porte désormais le payload parsé (dict pour `json`, str pour `raw`, AD-10) ; `title`, `short_message`, `detailed_message` optionnel. Aucune fonction, filtre ni logique scriptée dans les gabarits.

**Critères d'acceptation :**

**AC-1.3.1** — Substitution en mode `json`
**Given** une règle `json` (défaut) et un payload `{"action": "simple"}` avec le gabarit `Bouton appuye ({action})`
**When** le message est traité
**Then** la notification contient `Bouton appuye (simple)`.

**AC-1.3.2** — Chemin pointé imbriqué
**Given** un payload imbriqué `{"capteur": {"lieu": "salon"}}` et le gabarit `{capteur.lieu}`
**When** le message est traité
**Then** la valeur `salon` est substituée ; un chemin inexistant rend `[absent]`.

**AC-1.3.3** — Champ absent, jamais d'exception
**Given** un gabarit référençant un champ absent du payload
**When** le rendu s'exécute
**Then** le texte `[absent]` est substitué et la notification est envoyée — aucune exception, aucun envoi avorté.

**AC-1.3.4** — Payload JSON non parsable
**Given** une règle `json` et un payload qui n'est pas du JSON valide
**When** le message est traité
**Then** la notification est rendue comme si tous les champs étaient absents (rendu avec `[absent]`) et un avertissement est journalisé — le message n'est jamais ignoré en bloc.

**AC-1.3.5** — Mode `raw` et placeholder unique
**Given** une règle `payload_format: raw` avec le gabarit `{payload}`
**When** le message est traité
**Then** le message brut décodé UTF-8 est substitué ; tout autre placeholder dans une règle `raw` est rejeté au chargement.

**AC-1.3.6** — Accolade littérale
**Given** un gabarit contenant `{{`
**When** le rendu s'exécute
**Then** il produit un `{` littéral, sans tenter de substitution.

**AC-1.3.7** — Validation des gabarits au chargement
**Given** un gabarit malformé (placeholder avec espace, accolade non fermée)
**When** le démon démarre
**Then** démarrage refusé avec erreur explicite citant la règle et le champ fautifs — jamais découverts seulement au rendu.

**AC-1.3.8** — Personnalisation par type d'événement
**Given** deux règles aux gabarits différents, déclenchées chacune par leur événement
**When** les deux messages arrivent
**Then** les deux notifications affichent des contenus distincts, fidèles à leurs gabarits (FR-4).

**AC-1.3.9** — Message détaillé optionnel
**Given** une règle sans `detailed_message`
**When** la notification est envoyée
**Then** seules les clés `title` et `short_message` sont présentes dans le message FCM.

### Story 1.4 : Contrat FCM et robustesse de l'émetteur

En tant que **propriétaire du réseau domotique**,
je veux que chaque notification envoyée respecte strictement le contrat de l'app mcp-notif et qu'aucune erreur d'envoi ne mette le service à terre,
pour que le téléphone reçoive toujours des messages propres et que le démon tourne des mois sans surveillance.

**Périmètre technique :** consolidation de `notifier.py` (AD-6) : validation avant tout appel FCM (`title`/`short_message` non vides, tailles en octets UTF-8 ≤ 100 / ≤ 500 / ≤ 3 500), troncature propre, mapping des erreurs FCM, retries avec backoff borné. Aucun autre module ne construit ni n'envoie de message FCM.

**Critères d'acceptation :**

**AC-1.4.1** — Validation des limites du contrat
**Given** un rendu dont `title` fait plus de 100 octets UTF-8 ou `short_message` plus de 500
**When** l'envoi s'apprête
**Then** les valeurs sont tronquées proprement à leurs limites (à la frontière de caractère, jamais au milieu d'un octet) et le message envoyé respecte le contrat — jamais envoyé corrompu ni rejeté silencieusement.

**AC-1.4.2** — Champs non vides
**Given** un rendu dont `title` ou `short_message` est vide après substitution
**When** l'envoi s'apprête
**Then** l'envoi est refusé avec une erreur explicite journalisée (jamais de notification vide vers le téléphone) et le service continue.

**AC-1.4.3** — Message détaillé dans les limites
**Given** un `detailed_message` de plus de 3 500 octets UTF-8
**When** l'envoi s'apprête
**Then** il est tronqué proprement à 3 500 octets ; absent, la clé n'est pas envoyée (déjà AC-1.3.9, inchangé).

**AC-1.4.4** — Erreurs FCM sans retry
**Given** un envoi qui échoue avec `fcm_unregistered`, `fcm_invalid_argument` ou `fcm_permission_denied`
**When** l'erreur remonte
**Then** elle est journalisée avec l'erreur explicite, aucun retry n'est tenté, et le service continue (AC-1.1.4 déjà posé pour `fcm_unregistered` — désormais une politique générale).

**AC-1.4.5** — Erreurs FCM avec retry
**Given** un envoi qui échoue avec `fcm_throttled`, `fcm_internal` ou `network_error`
**When** l'erreur remonte
**Then** l'envoi est retenté avec un backoff borné (nombre de retries et délais bornés par des constantes), puis abandonné avec erreur journalisée si tout échoue ; le service continue.

**AC-1.4.6** — Frontière à rédacteur unique
**Given** le code du projet
**When** on cherche les points d'appel FCM
**Then** seul `notifier.py` construit et envoie des messages FCM — aucune autre unité ne touche firebase-admin ni les clés du contrat (revue de code, contrôlable par grep).

**AC-1.4.7** — Aucune erreur d'envoi n'arrête le service
**Given** n'importe quelle combinaison d'erreurs des AC-1.4.4 et 1.4.5
**When** elles se produisent
**Then** le démon reste connecté au broker et traite les messages suivants — jamais de crash, jamais d'arrêt après le démarrage (NFR-5).

## Épopée 2 : Piloter sans redémarrer

Brfw9455 édite le YAML (messages, debounce, nouvelles règles) ; le changement prend effet à l'événement suivant, sans interruption, et le spam du bouton est silencié par le debounce (UJ-2).

### Story 2.1 : Rechargement à chaud avec swap atomique

En tant que **bricoleur**,
je veux que toute modification du fichier YAML prenne effet sans redémarrer — messages, nouvelles règles, device token,
pour que le prochain événement utilise la nouvelle configuration et qu'un YAML invalide ne casse jamais le service en marche.

**Périmètre technique :** `config.py` devient le ConfigStore (AD-3) : surveillance du fichier par sondage mtime en tâche asyncio, validation intégrale avant swap (schéma + limites + gabarits), remplacement en une opération atomique ; `mqtt.py` resynchronise ses souscriptions au swap (abonnement des nouveaux topics, désabonnement des disparus), sans déconnexion du broker. Hors périmètre : un changement de `broker.host/port` exige toujours un redémarrage (documenté dans le README du YAML) ; les fenêtres de debounce sont gérées en Story 2.2.

**Critères d'acceptation :**

**AC-2.1.1** — Prise d'effet sans redémarrage
**Given** le démon en marche et un événement traité avec l'ancien libellé
**When** le libellé de la règle est modifié dans le YAML
**Then** le changement prend effet à partir de l'événement suivant, sans redémarrage ni aucune intervention (SM-2, FR-7).

**AC-2.1.2** — Swap atomique
**Given** un rechargement en cours
**When** une étape du pipeline lit la configuration
**Then** elle voit intégralement l'ancienne ou la nouvelle — jamais un mélange des deux, jamais une config à moitié chargée.

**AC-2.1.3** — YAML invalide rejeté
**Given** le YAML rendu invalide (schéma, gabarit, limites)
**When** le watcher recharge
**Then** l'erreur est journalisée de façon explicite, les règles précédentes restent actives et l'événement suivant est traité avec l'ancienne configuration (FR-7).

**AC-2.1.4** — Resynchronisation des souscriptions
**Given** un rechargement qui ajoute et retire des règles
**When** le swap a lieu
**Then** la source MQTT s'abonne aux nouveaux topics et se désabonne des disparus sans déconnexion du broker ; un message sur un topic retiré ne déclenche plus rien.

**AC-2.1.5** — Rechargement journalisé
**Given** chaque rechargement, accepté ou rejeté
**When** il se produit
**Then** une ligne de journal est écrite (rechargement réussi / YAML invalide conservé), timestamps ISO 8601 UTC.

**AC-2.1.6** — Lecteur unique du YAML
**Given** le code du projet
**When** on cherche les lectures du fichier YAML
**Then** seul le ConfigStore le lit — le pipeline et le reloader voient toujours les mêmes règles au même instant (revue de code, contrôlable par grep).

**AC-2.1.7** — Rotation du device token à chaud
**Given** le device_token modifié dans le YAML (token retourné par Firebase)
**When** le rechargement a lieu
**Then** le prochain envoi FCM utilise le nouveau token, sans redémarrage (complète FR-6 : la rotation impose une édition du fichier, pas un arrêt de service).

### Story 2.2 : Debounce par règle

En tant que **propriétaire du réseau domotique**,
je veux pouvoir fixer un délai minimum entre deux notifications pour chaque règle,
pour qu'un bouton qui rebondit ou qu'on tripote n'envoie pas une rafale de notifications sur mon téléphone.

**Périmètre technique :** nouveau `debounce.py`, étape dédiée du pipeline entre rendu et émetteur (AD-1, AD-7) ; `debounce_ms` optionnel par règle dans le YAML, bornes [0, 3 600 000] (0 = pas de debounce, valeur par défaut) ; horodatage en mémoire, clé = nom unique de la règle ; fenêtre ouverte à la réception du message, jamais à l'envoi ; timer via `asyncio.sleep`, aucun thread (AD-2) ; jamais persisté ni partagé (AD-9). Au swap de config : purge des fenêtres des règles disparues, conservation de celles des règles qui restent.

**Critères d'acceptation :**

**AC-2.2.1** — Silence pendant la fenêtre
**Given** une règle avec `debounce_ms: 1000` et un premier message notifié
**When** un deuxième message arrive sur le même topic 400 ms plus tard
**Then** aucune notification n'est envoyée et le message ignoré est journalisé en debug (FR-8).

**AC-2.2.2** — Retour au silence après la fenêtre
**Given** la même règle et le premier message notifié
**When** un message arrive après l'expiration du délai (1000 ms écoulés)
**Then** la notification est envoyée et une nouvelle fenêtre s'ouvre.

**AC-2.2.3** — Fenêtre ouverte à la réception
**Given** une règle avec debounce et un envoi FCM lent (mock qui traîne)
**When** le délai expire pendant l'envoi
**Then** la fenêtre reste ancrée sur l'horodatage de réception du message, pas sur la fin de l'envoi — un message arrivé pendant le délai reste ignoré même si l'envoi traîne.

**AC-2.2.4** — Sans délai, chaque message notifie
**Given** une règle sans `debounce_ms` (ou `debounce_ms: 0`)
**When** plusieurs messages arrivent en rafale
**Then** chacun déclenche sa notification (FR-8).

**AC-2.2.5** — Indépendance des règles
**Given** deux règles avec debounce, chacune notifiée
**When** un message arrive sur la deuxième pendant sa fenêtre
**Then** elle est ignorée alors que la première, hors de sa propre fenêtre, reste pleinement active — les fenêtres ne se partagent jamais.

**AC-2.2.6** — Bornes de configuration
**Given** un `debounce_ms` négatif ou supérieur à 3 600 000
**When** la configuration est chargée ou rechargée
**Then** rejet explicite (démarrage refusé / rechargement rejeté avec anciennes règles conservées, mêmes politiques que AC-1.1.7 et AC-2.1.3).

**AC-2.2.7** — Comportement au swap de config
**Given** des fenêtres ouvertes pour deux règles, dont une supprimée du YAML au rechargement
**When** le swap a lieu
**Then** la fenêtre de la règle disparue est purgée et celle de la règle conservée persiste — un message immédiat sur la règle conservée pendant sa fenêtre reste ignoré.

**AC-2.2.8** — Redémarrage réinitialise les fenêtres
**Given** des fenêtres ouvertes
**When** le démon redémarre
**Then** toutes les fenêtres sont réinitialisées — le premier message de chaque règle notifie (AD-9, aucun état persisté).

## Épopée 3 : Vivre sur la machine dédiée

Le service démarre au boot de la Raspberry Pi, survit à toutes les erreurs et se pilote depuis journald : unité systemd, venv, chemins en variables d'environnement, règle initiale réelle pour le bouton.

### Story 3.1 : Service systemd résident

En tant que **propriétaire du réseau domotique**,
je veux que mqtt-notif tourne en service résident sur la machine dédiée, démarré au boot et redémarré en cas de défaillance,
pour que les notifications fonctionnent sans que je pense à lancer quoi que ce soit, pendant des mois.

**Périmètre technique :** `deploy/mqtt-notif.service` (unité systemd : `Restart=on-failure`, utilisateur dédié non-root, venv Python 3.13) ; chemins du YAML des règles et du fichier service-account FCM passés en variables d'environnement (`MQTT_NOTIF_CONFIG`, `MQTT_NOTIF_FIREBASE_CREDENTIALS` — lecture au démarrage, AD-8) ; logs plats sur stdout confiés à journald ; procédure d'installation documentée (activation, `enable --now`). Hors périmètre : durcissement avancé de l'unité et permissions exactes du service-account (reportés au déploiement par la spine, section Deferred).

**Critères d'acceptation :**

**AC-3.1.1** — Démarrage au boot
**Given** la machine dédiée avec l'unité installée et activée
**When** la machine démarre
**Then** le service démarre sans intervention manuelle et journalise sa connexion au broker.

**AC-3.1.2** — Redémarrage sur défaillance
**Given** le service en cours d'exécution
**When** le processus meurt (kill -9)
**Then** systemd le redémarre automatiquement (`Restart=on-failure`) et le démon repart proprement — fenêtres de debounce réinitialisées (AC-2.2.8).

**AC-3.1.3** — Chemins par variables d'environnement
**Given** l'unité systemd
**When** on inspecte sa configuration
**Then** les chemins du YAML et du service-account sont passés par `MQTT_NOTIF_CONFIG` et `MQTT_NOTIF_FIREBASE_CREDENTIALS` — aucun chemin codé en dur dans le démon ni dans l'unité.

**AC-3.1.4** — Échec de démarrage visible
**Given** un YAML absent ou un service-account manquant au boot
**When** le service démarre
**Then** il échoue vite (politique AC-1.1.7), systemd le marque failed et l'erreur est lisible dans journald — pas de boucle de redémarrage silencieuse indéfinie (le comportement exact de `StartLimitBurst` est un réglage du déploiement, documenté).

**AC-3.1.5** — Logs dans journald
**Given** le service en marche
**When** on lit `journalctl -u mqtt-notif`
**Then** les lignes du démon y sont visibles (connexions, rechargements, envois, erreurs), timestamps ISO 8601 UTC — le démon écrit sur stdout, jamais dans un fichier de log propre.

**AC-3.1.6** — Arrêt propre sous systemd
**Given** le service en marche
**When** `systemctl stop mqtt-notif`
**Then** le démon reçoit SIGTERM, se déconnecte proprement et termine code 0 (AC-1.1.6) — l'état systemd passe à inactive, pas à failed.

**AC-3.1.7** — Installation reproductible
**Given** la machine dédiée vierge
**When** on suit la procédure documentée
**Then** venv créé, dépendances installées, unité copiée, service activé — sans étape manuelle non documentée.

### Story 3.2 : Règle réelle du bouton et validation terrain de SM-1

En tant que **propriétaire du réseau domotique**,
je veux que la règle réelle du bouton soit configurée et que la latence soit mesurée sur le vrai broker et le vrai téléphone,
pour que le projet tienne sa métrique de succès (SM-1, moins de 2 secondes) dans ses conditions réelles d'exploitation, pas seulement dans des tests mockés.

**Périmètre technique :** configuration réelle sur la machine dédiée : règle du bouton avec le topic réel, `payload_format` confirmé sur le broker réel (la question ouverte de la spine : JSON ou brut), device token réel, service-account Firebase réel ; mesure de latence de l'appui au téléphone via les timestamps journald (réception MQTT → envoi FCM) complétée d'un relevé manuel de l'affichage ; documentation de la procédure (ajouter une règle, mesurer, tourner le token). C'est une story de validation terrain : les mocks ne peuvent pas prouver SM-1.

**Critères d'acceptation :**

**AC-3.2.1** — Règle du bouton configurée sur le réel
**Given** le broker réel et le YAML de production
**When** on inspecte la règle du bouton
**Then** elle déclare le topic réel du bouton et un `payload_format` confirmé par observation des messages réels du broker (la question ouverte de la spine est tranchée sur le terrain).

**AC-3.2.2** — UJ-1 réel de bout en bout
**Given** le service installé et la règle du bouton active
**When** Brfw9455 appuie sur le bouton du salon
**Then** la notification personnalisée s'affiche sur le téléphone, écran verrouillé inclus.

**AC-3.2.3** — Latence mesurée sous 2 secondes
**Given** le service en marche en conditions normales (broker et réseau au calme)
**When** on déclenche l'appui et on relève les timestamps journald (réception MQTT → envoi FCM) et l'affichage sur le téléphone
**Then** la chaîne complète tient en moins de 2 secondes, relevé consigné avec la méthode de mesure (SM-1).

**AC-3.2.4** — Stabilité sur une journée d'exploitation
**Given** le service tournant
**When** une journée type s'écoule (appuis, un rechargement de YAML au moins, le broker qui ondule)
**Then** aucune erreur fatale, journald lisible (une ligne par événement attendu), notifications correctes — le silence reste la norme hors événement réel (SM-C1).

**AC-3.2.5** — Documentation d'exploitation
**Given** le dépôt du projet
**When** Brfw9455 veut ajouter une règle, mesurer la latence ou tourner le device token
**Then** la procédure est documentée (exemple de YAML réel commenté incluse), sans avoir à relire le code.
