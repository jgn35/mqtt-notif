# Epic 2 Context: Piloter sans redémarrer

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Cette épopée rend le YAML pleinement pilotable à chaud : toute modification du fichier de règles (messages, debounce, nouvelles règles, device token) prend effet à l'événement suivant, sans redémarrage ni interruption du service, et un YAML invalide ne casse jamais le démon en marche. Elle ajoute ensuite le debounce par règle, qui silencie les rafales de messages (bouton qui rebondit) selon un délai minimum configurable. Elle réalise UJ-2 et les métriques SM-2 (YAML effectif sans redémarrage) tout en respectant la contre-métrique SM-C1 (au plus une notification par événement, le silence reste la norme).

## Stories

- Story 2.1: Rechargement à chaud avec swap atomique
- Story 2.2: Debounce par règle

## Requirements & Constraints

- Toute modification du YAML (gabarits, nouvelles règles, device token) prend effet sur l'événement suivant, sans redémarrage ni intervention.
- Le rechargement est atomique : une étape du pipeline lit intégralement l'ancienne ou la nouvelle configuration, jamais un mélange des deux, jamais une config à moitié chargée.
- Un YAML invalide (schéma, gabarits, limites) est rejeté : erreur explicite journalisée, les règles précédentes restent actives. Les erreurs ne sont jamais fatales après le démarrage.
- Au swap, la source MQTT resynchronise ses souscriptions (abonnement des nouveaux topics, désabonnement des disparus) sans déconnexion du broker ; un message sur un topic retiré ne déclenche plus rien.
- La rotation du device token se fait par édition du YAML ; le prochain envoi FCM utilise le nouveau token sans redémarrage.
- Chaque rechargement, accepté ou rejeté, produit une ligne de journal (timestamps ISO 8601 UTC).
- Debounce : délai minimum configurable entre deux notifications, par règle. Messages reçus pendant la fenêtre → pas de notification, message ignoré journalisé en debug. Une règle sans délai (ou `debounce_ms: 0`, valeur par défaut) notifie chaque message.
- `debounce_ms` en millisecondes, bornes [0, 3 600 000] ; hors bornes → rejet explicite avec la même politique que le reste (démarrage refusé / rechargement rejeté avec anciennes règles conservées).
- La fenêtre de debounce s'ouvre à la réception du message, jamais à la fin de l'envoi FCM — un envoi lent ne rallonge pas la fenêtre.
- Les fenêtres sont indépendantes entre règles (clé = nom unique de la règle), jamais partagées ; elles ne sont jamais persistées ni partagées avec une autre étape ; un redémarrage les réinitialise toutes (perte assumée, pas de persistance).
- Hors périmètre : un changement de `broker.host/port` exige toujours un redémarrage (à documenter dans le README du YAML).

## Technical Decisions

- Le rechargement vit dans le ConfigStore (`config.py`) : un seul objet détient les règles, seul lui lit le fichier YAML (contrôlable par grep) ; le pipeline et le reloader voient toujours les mêmes règles au même instant.
- Surveillance du fichier par sondage mtime en tâche asyncio (période de sondage : constante d'implémentation) ; lecture fichier via `asyncio.to_thread`.
- Validation intégrale avant swap : schéma + limites + syntaxe des gabarits, puis remplacement en une opération atomique.
- Le debounce est une étape dédiée du pipeline (`debounce.py`), entre rendu et émetteur, dans l'ordre source MQTT → sélection de règle → rendu → debounce → émetteur FCM ; les étapes ne communiquent que par passage de valeur à l'étape suivante, aucune n'appelle une étape non adjacente.
- Le debounce est un simple horodatage en mémoire par règle ; timer via `asyncio.sleep` ; tout s'exécute sur la boucle asyncio unique, aucun thread applicatif.
- L'objet événement reste immuable en traversée (topic, payload, règle figée au moment de la sélection) ; une règle en cours de traitement n'est jamais relue depuis le ConfigStore.
- Au swap de config : purge des fenêtres des règles disparues, persistance de celles des règles conservées.
- Stack : Python 3.13, aiomqtt 2.5.1, firebase-admin 7.7.0, PyYAML 6.0.3 ; clés YAML en `snake_case`, noms de règles uniques.

## Cross-Story Dependencies

- Dépend de l'Épopée 1 : le ConfigStore (`config.py`), la source MQTT multi-topics (`mqtt.py`), le pipeline complet et la politique d'échec rapide au démarrage doivent être en place ; le rechargement à chaud réutilise la validation de schéma et de gabarits posée par les stories 1.2/1.3.
- Story 2.2 s'appuie sur 2.1 : le comportement des fenêtres au swap de config suppose le swap atomique en place.
- L'Épopée 3 dépend de cette épopée : le redémarrage systemd (`Restart=on-failure`) suppose que le redémarrage réinitialise les fenêtres de debounce.
