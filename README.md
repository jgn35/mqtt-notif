# mqtt-notif

Pont MQTT → notifications FCM : un démon Python qui souscrit à un broker
Mosquitto, applique des règles YAML (première gagnante) et envoie une
notification personnalisée sur un téléphone Android via Firebase Cloud
Messaging, adressée par device token.

## Ce que fait le démon

- **Règles YAML** : chaque message MQTT entrant est confronté aux règles
  (topic exact ou motif `+`/`#`) ; la première gagnante produit une
  notification. `payload_format` `json` (champs pointés dans les gabarits)
  ou `raw` (placeholder `{payload}` seul).
- **Gabarits personnalisés** : `title`, `short_message`,
  `detailed_message` par règle ; champ absent rendu `[absent]`.
- **Debounce par règle** : `debounce_ms` borne les appuis rapprochés
  (fenêtre ancrée à la réception).
- **Rechargement à chaud** : le YAML est sondé chaque seconde — règles et
  device token édités sans redémarrage ; `broker.host/port` seuls exigent
  un restart.
- **Robustesse** : erreurs d'exploitation jamais fatales (retry FCM à
  backoff borné, reconnexion broker) ; seules les erreurs de démarrage
  terminent le processus (échec vite, lisible dans journald).
- **Service systemd résident** : démarre au boot, redémarré par systemd sur
  défaillance, logs plats sur stdout confiés à journald.

Aucune persistance : souscription QoS 0, pas de rejeu ; un redémarrage
réinitialise les fenêtres de debounce.

## Démarrage rapide

```sh
# Dépendances (Python >= 3.13)
uv sync

# Tests
uv run pytest -q

# Lancer (chemins passés par variables d'environnement, jamais codés en dur)
export MQTT_NOTIF_CONFIG=/chemin/vers/mqtt-notif.yaml
export MQTT_NOTIF_FIREBASE_CREDENTIALS=/chemin/vers/service-account.json
uv run mqtt-notif
```

Le YAML doit contenir `broker.host`, `broker.port`, `device_token` et au
moins une règle. Exemple complet commenté : [`config/mqtt-notif.yaml`](config/mqtt-notif.yaml).

## Documentation

| Sujet | Où |
|---|---|
| Installation en service systemd sur la machine dédiée (Pi) | [`deploy/README.md`](deploy/README.md) |
| Exploitation courante : confirmer un topic/payload sur le broker, ajouter/retirer une règle, mesurer la latence SM-1, tourner le device token, journée type | [`docs/EXPLOITATION.md`](docs/EXPLOITATION.md) |
| Exemple de YAML réel commenté | [`config/mqtt-notif.yaml`](config/mqtt-notif.yaml) |

## Structure du dépôt

```
mqtt_notif/   le démon (config, règles, gabarits, debounce, MQTT, FCM, main)
deploy/       unité systemd, script d'installation, doc de déploiement
docs/         procédures d'exploitation
config/       exemple de YAML (aucune valeur réelle : placeholders)
tests/        pytest, y compris anti-rot de l'exemple et de la doc
```

## Périmètre (MVP)

LAN fermé sans authentification broker/TLS, un seul téléphone adressé par
device token (jamais par `fid`), pas de conteneur, pas de base de données,
pas d'API de gestion. La rotation du device token passe par l'édition du
YAML (prise d'effet au prochain envoi).
