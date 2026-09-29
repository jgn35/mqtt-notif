# Addendum — mqtt-notif

Contenu technique et rationale issus de l'élaboration du PRD. Destiné aux documents d'architecture et de solution design, pas au PRD lui-même.

## Recon du système mcp-notif existant (2026-09-28)

Source : `github.com/jgn35/mcp-notif` (README, commit 192de446).

Architecture actuelle :

```
LLM --notify(tool)--> MCP server --data-only FCM--> Android app
Android app --POST /enroll--> MCP server --> SQLite token store
```

- **Serveur MCP** (`mcp-server/`) : Python 3.12, FastMCP 4.x, hexagonal. Un outil `notify` (3 champs validés en octets UTF-8 avant tout appel FCM), un endpoint `POST /enroll` (Bearer `ENROLLMENT_TOKEN`, body `{"device_token": ...}`), store SQLite mono-ligne (id=1, WAL), Podman Compose, port 8080 derrière Apache TLS.
- **App Android** (`android-app/`) : Kotlin, minSdk 26 / target 35. Reçoit les messages FCM *data-only* et ne lit que `title`, `short_message`, `detailed_message` de `remoteMessage.data`. `DetailActivity` rend `detailed_message` en Markdown (fallback `short_message`). L'enrôlement POST vers `ENROLL_URL` (unique, défini au build via `local.properties`), avec retry/backoff ; `onNewToken` re-enrôle.
- **Contraintes de validation du serveur (à répliquer côté mqtt-notif)** : `title` ≤ 100 octets UTF-8 non vide, `short_message` ≤ 500 non vide, `detailed_message` ≤ 3500 (Markdown).
- **Types d'erreur FCM à prévoir** : `fcm_unregistered` (token invalide, ne pas retenter), `fcm_invalid_argument`, `fcm_permission_denied`, `fcm_throttled` (retenter), `fcm_internal` (retenter), `network_error`.

## Décision : envoi FCM direct plutôt qu'appel au serveur MCP

Choix de l'utilisateur (2026-09-28) : mqtt-notif envoie lui-même via FCM (firebase-admin), sans passer par l'outil `notify` du serveur MCP.

- **Pour** : indépendance vis-à-vis du serveur MCP (pas de dépendance de disponibilité ni d'auth MCP) ; chemin plus court sur la machine dédiée ; le serveur MCP reste dédié aux LLM.
- **Contre / conséquences** : duplication de la gestion du device token. L'app Android n'enrôle que vers une seule `ENROLL_URL` (au build) ; mqtt-notif doit donc récupérer le token autrement — lecture du store SQLite du serveur mcp-notif, copie manuelle, ou enrôlement dédié. C'est la Question ouverte n° 1 du PRD, bloquante pour l'architecture.
- **Alternative rejetée** : agir comme client MCP et appeler `notify`. Aurait réutilisé le token store et l'enrôlement existants sans duplication, mais aurait couplé la chaîne de notification au serveur MCP (disponibilité, `MCP_AUTH_TOKEN`).

## Environnement cible

- Machine dédiée au réseau domestique (type Raspberry Pi), broker Mosquitto sans authentification, réseau fermé.
- Contrat de sortie imposé par l'app existante : message FCM *data-only* avec les clés `title`, `short_message`, `detailed_message`.
