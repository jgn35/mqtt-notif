# Epic 3 Context: Vivre sur la machine dédiée

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Cette épopée installe mqtt-notif en conditions réelles d'exploitation sur la Raspberry Pi dédiée : le démon devient un service résident qui démarre au boot, survit à toutes les erreurs d'exploitation et se pilote entièrement depuis journald. Elle couvre l'exigence MVP « service résident » et NFR-5 (exploitabilité systemd/journald), puis valide sur le terrain ce que les épopées précédentes n'ont prouvé qu'avec des mocks : la règle réelle du bouton configurée sur le vrai broker, et la métrique de succès SM-1 (notification en moins de 2 secondes) mesurée sur la chaîne complète réelle. C'est la dernière épopée : sans elle, le projet n'existe qu'en test.

## Stories

- Story 3.1 : Service systemd résident
- Story 3.2 : Règle réelle du bouton et validation terrain de SM-1

## Requirements & Constraints

- Le service démarre automatiquement au boot de la machine et est redémarré par systemd sur défaillance du processus (`Restart=on-failure`). Aucune intervention manuelle ne doit être nécessaire en exploitation courante.
- Les chemins du YAML de règles et du fichier service-account FCM sont passés en variables d'environnement (`MQTT_NOTIF_CONFIG`, `MQTT_NOTIF_FIREBASE_CREDENTIALS`), lues au démarrage — aucun chemin codé en dur, ni dans le démon ni dans l'unité.
- Le démon écrit des logs plats sur stdout, jamais dans un fichier propre : tout est confié à journald et lisible via `journalctl -u mqtt-notif` (une ligne par connexion/déconnexion/reconnexion, rechargement, envoi, erreur), timestamps en ISO 8601 UTC.
- Une erreur de démarrage (YAML absent, service-account manquant) doit échouer vite et de façon visible : le service est marqué failed par systemd et l'erreur est lisible dans journald — pas de boucle de redémarrage silencieuse indéterminée (le réglage exact de `StartLimitBurst` relève du déploiement).
- `systemctl stop` envoie SIGTERM : arrêt propre avec déconnexion du broker et code de sortie 0 — l'état systemd passe à inactive, pas à failed.
- L'installation doit être reproductible : procédure documentée de bout en bout (venv, dépendances, unité copiée, activation), sans étape manuelle non documentée.
- Sur le terrain : la règle du bouton déclare le topic réel et un `payload_format` confirmé par observation des messages réels du broker (la question ouverte « JSON ou brut » est tranchée ici) ; la latence complète appui → téléphone tient sous 2 secondes en conditions normales, mesurée via les timestamps journald (réception MQTT → envoi FCM) complétée d'un relevé manuel de l'affichage, méthode consignée ; une journée type d'exploitation (appuis, au moins un rechargement de YAML, broker qui ondule) ne produit aucune erreur fatale, et le silence reste la norme hors événement réel.
- La documentation d'exploitation (ajouter une règle, mesurer la latence, tourner le device token, exemple de YAML réel commenté) doit permettre d'opérer sans relire le code.
- Hors périmètre : durcissement avancé de l'unité systemd et permissions exactes du fichier service-account (reportés au déploiement) ; authentification broker/TLS ; conteneur ; multi-appareil.

## Technical Decisions

- Enveloppe opérationnelle imposée : service systemd natif, pas de conteneur, pas de base de données ; venv Python 3.13 dédié (Python 3.13 est livré par Raspberry Pi OS Trixie, Debian 13) ; utilisateur dédié non-root pour l'exécution du service.
- L'unité systemd vit dans `deploy/mqtt-notif.service` ; le fichier de règles par défaut est `config/mqtt-notif.yaml` mais le chemin réel vient toujours de l'environnement.
- Aucune persistance, perte assumée : souscription QoS 0, pas de rejeu ; un redémarrage (volontaire ou par systemd) réinitialise toutes les fenêtres de debounce — le premier message de chaque règle notifie.
- Les erreurs ne sont jamais fatales après le démarrage : seule la phase de démarrage peut terminer le processus avec un code non nul (config absente ou invalide). C'est ce qui rend sûr le couplage avec `Restart=on-failure` : systemd ne redémarre que sur défaillance réelle, jamais en boucle sur une erreur d'exploitation.
- Stack figée : Python 3.13, aiomqtt 2.5.1 (paho-mqtt 2.1.0 transitif), firebase-admin 7.7.0, PyYAML 6.0.3, Raspberry Pi OS Trixie ; systemd + journald pour l'exploitation.
- Envoi FCM direct par firebase-admin, adressage par device token (jamais par `fid`), token renseigné manuellement dans le YAML — sa rotation par Firebase impose une édition du fichier (procédure à documenter), pas un arrêt de service.
- Constantes (backoff max, nombre de retries, période de sondage mtime, textes exacts des logs) sont des réglages d'implémentation, non fixés par la planification.
- Le changement de `broker.host/port` exige toujours un redémarrage — à documenter dans le README du YAML.

## Cross-Story Dependencies

- La Story 3.1 s'appuie sur l'arrêt propre SIGTERM/code 0 et la politique d'échec rapide au démarrage posés en Épopée 1 ; le redémarrage par systemd réinitialise les fenêtres de debounce (comportement de l'Épopée 2, assumé).
- La Story 3.2 suppose la Story 3.1 installée (service actif sur la machine dédiée) et exploite les logs journald produits par les épopées 1 et 2 pour mesurer la latence ; elle dépend aussi du rechargement à chaud de l'Épopée 2 pour le test d'édition du YAML en exploitation.
- Environnement externe requis pour la Story 3.2 : broker Mosquitto réel (LAN fermé, sans authentification), téléphone avec l'app mcp-notif enrôlée, device token et service-account Firebase réels.
