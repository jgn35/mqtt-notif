# Epic 1 Context: Première notification de bout en bout

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Livrer le socle greenfield complet : un message MQTT publié sur le réseau domotique aboutit à une notification personnalisée sur le téléphone en moins de 2 secondes, via le pipeline source MQTT → sélection de règle → rendu → émetteur FCM, piloté par le YAML. À la fin de cette épopée, la chaîne broker → démon → FCM existe de bout en bout, filtrée par règles, avec des messages personnalisés et un émetteur résilient : aucune erreur d'envoi ne doit jamais arrêter le service après le démarrage. C'est la tranche qui rend le projet vérifiable dès la première story.

## Stories

- Story 1.1: Tranche fine — du message MQTT à la notification FCM
- Story 1.2: Règles YAML et sélection première-gagnante
- Story 1.3: Rendu des gabarits personnalisés
- Story 1.4: Contrat FCM et robustesse de l'émetteur

## Requirements & Constraints

- Connexion au broker MQTT et souscription QoS 0 à tous les topics des règles ; reconnexion automatique avec backoff croissant, journalisée (une ligne par connexion, déconnexion, reconnexion).
- Un message sur un topic ne correspondant à aucune règle ne déclenche aucune notification (silence = norme hors événement réel) ; au plus une notification par événement.
- Règles définies en YAML : topic exact ou motif `+`/`#`, gabarits de message ; chaque règle produit un message distinct pour son type d'événement.
- Notification FCM *data-only* avec les clés `title`, `short_message`, `detailed_message` (optionnel) ; les limites du contrat de l'app Android doivent être respectées — un message hors limites est tronqué proprement, jamais envoyé corrompu.
- Device token lu dans le YAML (rotation = mise à jour manuelle du fichier) ; token absent ou rejeté : erreur explicite journalisée, le service continue.
- Latence de bout en bout < 2 secondes en conditions normales.
- Aucune erreur d'envoi n'arrête le service après le démarrage ; erreurs fatales uniquement avant le démarrage (échec rapide sur config invalide).
- Champ référencé absent dans le payload → texte de remplacement explicite, jamais d'exception ; payload JSON non parsable → rendu avec remplacement, jamais ignoré en bloc.
- Périmètre : mono-utilisateur, mono-appareil, réseau domestique fermé ; pas d'authentification broker ni TLS ; pas d'interface web ni d'API de gestion ; aucune transformation scriptée des payloads (substitution de champs uniquement).

## Technical Decisions

- Pipeline unidirectionnel mono-processus : les étapes communiquent uniquement par passage de valeur à l'étape suivante, dans l'ordre source MQTT → sélection de règle → rendu → debounce (Épopée 2) → émetteur FCM. Aucune étape n'appelle une étape non adjacente.
- Boucle d'événements asyncio unique ; appels bloquants (firebase-admin, lecture fichier) via `asyncio.to_thread` ; aucun thread applicatif.
- Première règle gagnante : évaluation dans l'ordre de déclaration, première correspondance (exacte ou motif `+`/`#`) traite le message ; plusieurs correspondances → avertissement journalisé ; aucune → message ignoré (debug).
- Syntaxe de substitution unique : `{champ}` et `{chemin.pointé}` en mode `json` (défaut), `{payload}` seul en mode `raw` ; `{{` → `{` littéral ; champ absent → `[absent]`, jamais d'exception. Aucun filtre ni fonction dans les gabarits ; syntaxe validée au chargement de la config, jamais seulement au rendu.
- Frontière FCM à rédacteur unique : seul `notifier.py` construit et envoie des messages FCM ; validation avant tout appel (`title`/`short_message` non vides, ≤ 100 / ≤ 500 / ≤ 3 500 octets UTF-8) ; adressage par device token, jamais par `fid`. Erreurs sans retry : `unregistered`, `invalid_argument`, `permission_denied` ; erreurs avec retry et backoff borné : `throttled`, `internal`, `network_error`.
- Envois FCM découplés : chaque envoi (avec ses retries) s'exécute comme tâche asyncio indépendante ; la file n'attend jamais la fin d'un envoi ; l'ordre d'affichage sur le téléphone n'est pas garanti (accepté).
- Objet événement unique immuable entre étapes : topic (str), payload (dict parsé pour `json`, str pour `raw`), règle figée au moment de la sélection, jamais relue en cours de traitement.
- Aucune persistance : souscription QoS 0, pas de rejeu ; un redémarrage perd l'état en cours (assumé).
- Seed structurel : paquet `mqtt_notif` avec `main.py` (assemblage, boucle, signals), `config.py` (lecture/ validation YAML), `mqtt.py` (source), `rules.py` (sélection), `render.py` (rendu), `notifier.py` (frontière FCM). Projet greenfield, pas de starter externe.
- Stack imposée : Python 3.13, aiomqtt 2.5.1 (paho-mqtt 2.1.0 transitif), firebase-admin 7.7.0, PyYAML 6.0.3, Raspberry Pi OS Trixie.
- Conventions : Python et clés YAML en `snake_case`, topics MQTT en minuscules, noms de règles uniques, limites comptées en octets UTF-8, timestamps de journal en ISO 8601 UTC, logs plats sur stdout.

## Cross-Story Dependencies

- La Story 1.1 pose la tranche fine livrable seule (connexion, souscription à un topic unique, envoi FCM à message fixe) ; la 1.2 remplace le topic unique par l'ensemble des topics des règles ; la 1.3 introduit le rendu des gabarits (le contenu fixe des stories 1.1–1.2 est transitoire) ; la 1.4 consolide `notifier.py` avec la validation complète du contrat FCM et la politique d'erreurs.
- L'arrêt propre sur SIGTERM avec code 0 (Story 1.1) prépare `Restart=on-failure` de l'Épopée 3 ; le debounce (AD-7) est une étape dédiée du pipeline introduite en Épopée 2, entre rendu et émetteur.
- Le rechargement à chaud du YAML (Épopée 2) : en Épopée 1, la configuration n'est lue qu'au démarrage ; l'événement figé en traversée (règle retenue jamais relue) doit déjà être respecté pour rester compatible avec le swap atomique à venir.
