---
review-date: 2026-09-28
reviewer-role: verification d'actualite technologique
target: ../ARCHITECTURE-SPINE.md
verdict: pass
---

# Revue — ARCHITECTURE-SPINE.md (mqtt-notif) : verification d'actualite technologique

Document relu : `_bmad-output/planning-artifacts/architecture/architecture-mqtt-notif-2026-09-28/ARCHITECTURE-SPINE.md`

Role : verifier que chaque decision technologique engagee par la spine (table Stack + decisions incorporees dans les AD) a ete confirmee contre une source vivante (PyPI, GitHub, Debian, code reel de mcp-notif) plutot qu'assertee de memoire.

Verdict global : **PASS** — 4 findings (1 medium, 1 low, 2 info). Aucune decision invalide ; une version perimee dans le Stack (firebase-admin) et deux precisions a reporter.

## Methode

Chaque point a ete verifie le 2026-09-28 contre :

- PyPI JSON API (`pypi.org/pypi/<pkg>/json`) — version courante, date de publication, dependances declarees.
- GitHub API (`api.github.com`) — activite des repos, notes de version.
- `packages.debian.org` — contenu du paquet `python3` dans Debian Trixie.
- Archive source de `github.com/jgn35/mcp-notif` (tarball du 2026-09-10, derniere activite 2026-09-10) — lecture du code Python et Kotlin de l'app, pas de la documentation seule.

Aucune des verifications ci-dessous ne repose sur la memoire du relecteur.

## Verification point par point

| Decision de la spine | Source verifiee | Resultat |
| --- | --- | --- |
| aiomqtt 2.5.1 existe et est actif | PyPI : 2.5.1 = derniere version, publiee 2026-03-05. Repo `empicano/aiomqtt` (non archive, dernier push 2026-04-23, 15 issues ouvertes) | Confirme, avec nuance (F2) |
| aiomqtt enveloppe paho-mqtt 2.x | PyPI `requires_dist` : `paho-mqtt>=2.1.0,<3.0.0` | Confirme |
| paho-mqtt 2.1.0 transitif, impose par aiomqtt | PyPI : 2.1.0 = derniere version de paho-mqtt (publiee 2024-04-29) ; la borne aiomqtt resout exactement vers 2.1.0 | Confirme (F3) |
| firebase-admin 7.5.0 est la version courante | PyPI : dernieres versions 7.5.0 (2026-07-02), 7.6.0 (2026-09-17), 7.7.0 (2026-09-23) | **Non confirme — perime** (F1) |
| firebase-admin supporte l'envoi FCM data-only | mcp-notif utilise `messaging.Message(data={...}, token=...)` sans champ `notification` (`fcm_sender.py`) ; teste e2e cote serveur et cote Android | Confirme |
| PyYAML 6.0.3 | PyPI : 6.0.3 = derniere version, publiee 2025-09-25 | Confirme |
| Python 3.13 par defaut sur Raspberry Pi OS Trixie | `packages.debian.org/trixie/python3` : paquet `python3 3.13.5-1` (Trixie = Debian 13, base de Raspberry Pi OS Trixie) | Confirme, avec residuel (F4) |
| FCM data-only, cles `title` / `short_message` / `detailed_message`, limite 100/500/3500 octets | Code reel mcp-notif : `core/notify.py` (limites 100/500/3500), `McpNotifMessagingService.kt` (lecture des trois cles, skip defensif si `title`/`short_message` vides) | Confirme — AD-6 aligne au code existant |
| `detailed_message` optionnel, repli sur `short_message`, rendu Markdown du detail | `McpNotifMessagingService.kt` : « The detail falls back to short_message when detailed_message is absent » ; spec mcp-notif : detail en Markdown, corps de notification = `short_message` texte brut | Confirme |

## Findings

### F1 — firebase-admin 7.5.0 n'est plus la version courante (severite : medium)

La spine epingle `firebase-admin 7.5.0` dans le Stack. Le 2026-09-28, la version courante est **7.7.0** (publiee 2026-09-23), apres 7.6.0 (2026-09-17). 7.5.0 n'est pas perimee au sens de la securite, mais l'assertion implicite « version courante » est fausse au moment de la relecture.

Point plus important pour ce projet : **7.5.0 est precisement la version qui introduit la deprecation de `token` au profit de `fid` pour le Send API FCM** (note de v7.5.0 : « feat(fcm): Enable `fid` and deprecate `token` for Send API »). Le code mcp-notif lui-meme documente ce piege : `fid` attend un identifiant different, et l'utiliser avec un registration token FCM provoque `fcm_unregistered` ; `token` fonctionne encore pendant la periode de migration. Epingle une version dans une fenetre de deprecation, sans le dire dans la spine, expose le builder a deux erreurs : installer 7.7.0 en suivant le Stack sans re-tester, ou voir un builder « moderniser » vers `fid` et casser l'envoi.

Recommandation : mettre le Stack a jour vers 7.7.0 (7.5.0 reste fonctionnel si le pin est delibere), et ajouter une ligne dans AD-6 : l'emetteur utilise `messaging.Message(data=..., token=...)` avec le registration token FCM, jamais `fid`, tant que mcp-notif enrôle des registration tokens.

### F2 — aiomqtt actif mais cadence lente et statut « Beta » (severite : low)

aiomqtt 2.5.1 est bien la derniere version, le repo n'est pas archive. Mais : derniere publication PyPI il y a ~7 mois (2026-03-05), dernier push repo 2026-04-23, et le trove classifier PyPI reste `Development Status :: 4 - Beta`. Pour un demon mono-usage sur LAN, le risque est faible et le choix reste justifie (asynchrone natif, reconnexion integree, AD-2 en depend). A noter pour le build : aiomqtt etant en Beta, les breaking changes entre mineures sont possibles — le pin exact `2.5.1` dans le Stack est la bonne decision, a conserver tel quel dans les requirements.

### F3 — paho-mqtt 2.1.0 : exact, mais projet quasi immobile (severite : info)

L'affirmation de la spine « paho-mqtt 2.1.0 (transitif, impose par aiomqtt) » est exactement vraie : 2.1.0 est la derniere version et la borne aiomqtt (`>=2.1.0,<3.0.0`) resout vers elle. A titre de contexte seulement : pas de release paho-mqtt depuis avril 2024. Ce n'est pas un probleme pour la spine (dependance transitive, jamais importee directement par mqtt-notif selon le seed), mais cela renforce le fait que toute interaction MQTT passera par la couche aiomqtt — le seed structural est coherent avec cette realite.

### F4 — Python 3.13 sur Trixie : confirme via Debian, image Pi OS non verifiee directement (severite : info)

Le paquet Debian Trixie est `python3 3.13.5-1`. Raspberry Pi OS Trixie etant construit sur Debian 13, le Python systeme sera 3.13. La verification a ete faite au niveau du paquet Debian, pas sur l'image Raspberry Pi OS elle-meme (les notes de release raspberrypi.com n'exposent pas la version Python de facon scrapeable) ; residuel faible car Pi OS herite du pool Debian. Consequence directe : l'Open Question `[ASSUMPTION] Python 3.13 disponible` de la spine peut etre retiree des suppositions et marquee confirmee, avec le venv dedie (AD-8) qui isole de toute facon des paquets systeme.

## Ce qui a ete verifie et est solide

Le point le plus critique de la spine — le contrat FCM avec l'app mcp-notif (AD-6) — est confirme integralement contre le code reel de l'app, pas sa documentation :

- Cles exactes `title` / `short_message` / `detailed_message`, et rien d'autre attendu par l'app.
- Limites d'octets 100/500/3500 : identiques dans `core/notify.py` de mcp-notif et dans AD-6 — les limites de la spine ne sont pas inventees, elles doublonnent la validation du pair existant.
- `detailed_message` reellement optionnel cote app (repli sur `short_message`), alors que le serveur MCP de mcp-notif l'exige — l'envoi a deux cles par mqtt-notif est donc valide pour l'app.
- Comportement de l'app si `title` ou `short_message` vide : skip defensif silencieux. La validation en amont imposee par AD-6 (`title`/`short_message` non vides avant tout envoi) est donc necessaire et bien placee.
- Le message est bien data-only (aucun champ `notification`), et l'app le gere en foreground et background (`FirebaseMessagingService.onMessageReceived`).

La politique d'erreurs FCM de AD-6 (`unregistered`, `invalid_argument`, `permission_denied` → pas de retry ; `throttled`, `internal`, `network_error` → retry) correspond aux types d'exception reels exposes par firebase-admin et au mapping implemente dans mcp-notif — les noms ne sont pas des hypotheses.

## Verdict

**PASS.** Aucune decision technologique de la spine n'est invalide : le format FCM, les limites d'octets, le comportement de l'app cible, aiomqtt/paho-mqtt 2.x et PyYAML 6.0.3 sont confirmes contre sources vivantes. Un point doit etre corrige avant le build (F1, medium) : le Stack epingle firebase-admin 7.5.0 qui n'est plus courant (7.7.0), au sein d'une fenetre de deprecation `token`→`fid` que la spine ignore — mettre a jour la version et consigner dans AD-6 que l'envoi se fait via `token`, pas `fid`.
