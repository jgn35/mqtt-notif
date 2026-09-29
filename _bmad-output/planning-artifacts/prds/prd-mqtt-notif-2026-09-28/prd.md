---
title: mqtt-notif
status: final
created: 2026-09-28
updated: 2026-09-28
---

# PRD : mqtt-notif

## 0. Objet du document

PRD personnel, mono-utilisateur, pour le projet **mqtt-notif** : pont entre les événements MQTT du réseau domotique et les notifications Android. Document compact (enjeux hobby/solo), destiné à alimenter les étapes suivantes du workflow BMad (architecture, epics/stories). Il s'appuie sur le système existant **mcp-notif** (serveur MCP + app Android réceptrice), décrit dans l'addendum.

## 1. Vision

mqtt-notif est un démon léger qui souscrit aux événements MQTT du réseau domotique et les transforme en notifications Android personnalisées. Quand un événement se produit — un appui sur un bouton, par exemple — mqtt-notif envoie au téléphone une notification dont le message est propre à ce type d'événement.

Le téléphone possède déjà l'application **mcp-notif** capable d'afficher ces notifications ; mqtt-notif n'a donc rien à faire côté Android. Le projet tient dans un petit service résident sur une machine dédiée du réseau domestique, piloté par un simple fichier YAML.

## 2. Utilisateur cible

### 2.1 Jobs To Be Done

- En tant que propriétaire du réseau domotique, je veux être averti sur mon téléphone dès qu'un événement MQTT se produit, sans ouvrir d'application domotique.
- En tant que bricoleur, je veux ajuster le texte des notifications par type d'événement en éditant un fichier YAML, sans redéployer l'application.

### 2.2 Non-utilisateurs (v1)

Aucun autre utilisateur ni aucun autre appareil : projet strictement monopersonnel et mono-appareil.

### 2.3 Parcours utilisateurs (forme légère, enjeux hobby)

- **UJ-1.** Brfw9455 appuie sur un bouton du réseau domotique ; mqtt-notif reçoit l'événement MQTT et pousse une notification Android personnalisée en moins de 2 secondes ; le libellé s'affiche sur l'écran verrouillé.
- **UJ-2.** Brfw9455 modifie le libellé d'une règle dans le fichier YAML ; mqtt-notif recharge la configuration à chaud et le prochain événement produit le nouveau message, sans redémarrage.

## 3. Glossaire

- **Broker MQTT** — serveur Mosquitto du réseau domestique, joignable sans authentification en v1 (réseau fermé).
- **Événement MQTT** — message publié sur un topic par le réseau domotique (ex. un appui sur un bouton).
- **Topic** — chaîne hiérarchique identifiant le sujet du message MQTT (ex. `home/salon/bouton`).
- **Type d'événement** — catégorie d'événements partageant le même modèle de message (ex. « appui bouton salon »).
- **Règle** — entrée du fichier de configuration associant un topic (ou un motif) à un modèle de message, et optionnellement à un délai debounce.
- **Modèle de message** — gabarit de notification (titre, message court, message détaillé) propre à un type d'événement, pouvant référencer des champs du payload MQTT.
- **Notification** — message FCM *data-only* affiché par l'app mcp-notif.
- **App mcp-notif** — application Android existante qui reçoit et affiche les notifications (`github.com/jgn35/mcp-notif/android-app`).
- **Device token** — jeton FCM identifiant le téléphone auprès de Firebase.

## 4. Fonctionnalités

### 4.1 Abonnement au broker MQTT

**Description :** au démarrage, mqtt-notif se connecte au Broker MQTT et souscrit aux topics référencés par les Règles. Réalise UJ-1.

**Exigences fonctionnelles :**

#### FR-1 : Connexion et souscription

Le système peut se connecter au Broker MQTT et souscrire, en QoS 0, à l'ensemble des topics définis par les Règles.

**Conséquences (testables) :**
- Le système se reconnecte automatiquement au broker après une déconnexion, avec backoff croissant.
- Le système journalise une ligne par connexion, déconnexion et reconnexion.

#### FR-2 : Filtrage par topic ou motif

Le système peut souscrire à un topic exact ou à un motif MQTT (`+`/`#`), selon la Règle.

**Conséquences (testables) :**
- Un message publié sur un topic ne correspondant à aucune Règle ne déclenche aucune notification.

### 4.2 Règles et messages personnalisés

**Description :** les Règles vivent dans un fichier YAML unique. Chaque Règle définit un type d'événement : son topic (ou motif), son modèle de message et, optionnellement, son délai debounce. Les variables du payload MQTT sont substituées dans le gabarit. Réalise UJ-1, UJ-2.

**Exigences fonctionnelles :**

#### FR-3 : Définition des Règles

L'utilisateur peut définir ses Règles dans un fichier YAML : topic (ou motif) + modèle de message (titre, message court, message détaillé optionnel).

**Conséquences (testables) :**
- Chaque Règle produit un message distinct pour son type d'événement.
- Le message détaillé est optionnel ; absent, la notification n'affiche que le titre et le message court.
- Un payload dont un champ référencé par le modèle est absent produit un texte de remplacement explicite plutôt qu'une erreur.

#### FR-4 : Personnalisation par type d'événement

Le système peut produire une notification dont le contenu dépend du type d'événement et des champs du payload MQTT.

**Conséquences (testables) :**
- Deux types d'événements différents sur le même broker produisent des notifications au contenu différent.

#### FR-8 : Debounce par Règle

Le système peut appliquer, par Règle, un délai minimum configurable entre deux notifications.

**Conséquences (testables) :**
- Les messages MQTT reçus sur un topic pendant le délai de sa Règle ne déclenchent pas de notification.
- Une Règle sans délai configuré notifie chaque message reçu.

### 4.3 Envoi de la notification

**Description :** mqtt-notif envoie directement la notification via FCM, au format attendu par l'app mcp-notif (message *data-only*, clés `title`, `short_message`, `detailed_message`). `[ASSUMPTION: les clés et limites du message sont celles du contrat de l'app mcp-notif — title ≤ 100 octets UTF-8, short_message ≤ 500, detailed_message ≤ 3500 (Markdown, optionnel).]` Réalise UJ-1.

**Exigences fonctionnelles :**

#### FR-5 : Envoi FCM direct

Le système peut envoyer une notification FCM *data-only* à partir d'une Règle déclenchée.

**Conséquences (testables) :**
- Une Règle déclenchée par un événement MQTT entraîne l'affichage d'une notification sur le téléphone.
- La notification respecte les limites de taille du contrat de l'app ; un message hors limites est tronqué ou rejeté proprement, jamais envoyé corrompu.

#### FR-6 : Résolution du device token

Le système peut lire le device token du téléphone dans le fichier de configuration, où il est renseigné manuellement.

**Conséquences (testables) :**
- Si le token est absent ou invalide (erreur `fcm_unregistered`), l'événement est journalisé avec une erreur explicite ; le service continue de tourner.
- La rotation du token par Firebase impose une mise à jour manuelle du fichier de configuration.

### 4.4 Rechargement à chaud de la configuration

**Description :** le fichier YAML des Règles est surveillé ; toute modification prend effet sans redémarrage. Réalise UJ-2.

**Exigences fonctionnelles :**

#### FR-7 : Rechargement sans interruption

Le système peut appliquer une modification du fichier de Règles sans redémarrer.

**Conséquences (testables) :**
- Une modification du YAML prend effet sur l'événement suivant.
- Un YAML invalide est rejeté : les Règles précédentes restent actives et l'erreur est journalisée.

## 5. Non-objectifs (explicites)

- Pas d'interface web ni d'API de gestion : le YAML est la seule surface de configuration.
- Pas de multi-utilisateur, pas de multi-appareil.
- Aucune modification de l'app Android mcp-notif ni de son serveur MCP : mqtt-notif est un client FCM indépendant.
- Pas d'authentification broker ni TLS en v1 (réseau domestique fermé).
- Pas d'historique ni de regroupement de notifications en v1.
- Pas de transformation scriptée des payloads (pas de fonctions/filtres complexes dans les modèles).

## 6. Périmètre MVP

**Inclus :** FR-1 à FR-8 ; exécution en service résident sur la machine dédiée (démarrage automatique) ; une Règle initiale couvrant l'appui sur le bouton.

**Exclu pour le MVP :** authentification broker, multi-appareil, tableau de bord d'état, nouvelles règles au-delà du bouton — aucun besoin identifié à ce stade ; le YAML permet d'en ajouter sans redéployer.

## 7. Métriques de succès

- **SM-1** : chaque événement MQTT déclencheur aboutit à une notification sur le téléphone en moins de 2 secondes en conditions normales. Valide FR-1, FR-5.
- **SM-2** : un changement du YAML prend effet sans redémarrage. Valide FR-7.
- **SM-C1 (contre-métrique, ne pas optimiser)** : aucun événement ne déclenche plus d'une notification — le silence reste la norme hors événement réel. Contrebalance SM-1.

## 8. Questions ouvertes

Toutes résolues lors de l'élaboration (journalisées dans `.memlog.md`) :

1. ~~Obtention du device token~~ — copie manuelle dans le fichier de configuration (FR-6).
2. ~~Types d'événements initiaux~~ — l'appui sur le bouton uniquement.
3. ~~Message détaillé~~ — optionnel, rarement renseigné (FR-3).
4. ~~QoS~~ — 0 (FR-1).
5. ~~Debounce~~ — délai minimum configurable par Règle (FR-8).

## 9. Index des hypothèses

- §4.3 — clés et limites du message reprises du contrat de l'app mcp-notif.
- §4.1 — broker Mosquitto joignable sans authentification depuis la machine dédiée.
- §6 — déploiement en service résident au démarrage de la machine (systemd ou conteneur).
- SM-1 — latence cible de 2 secondes.
