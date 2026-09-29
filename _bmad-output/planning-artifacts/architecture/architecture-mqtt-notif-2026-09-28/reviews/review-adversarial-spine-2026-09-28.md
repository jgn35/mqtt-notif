# Revue adverse — Architecture Spine mqtt-notif

- **Spine relue :** `ARCHITECTURE-SPINE.md` (2026-09-28, status draft)
- **Rôle :** attaquant adverse. Chaque section construit une paire d'unités du niveau en dessous (modules du Structural Seed / futures stories) dont chaque membre obéit **à la lettre** à tous les AD, mais qui ne se raccordent pas l'un à l'autre.
- **Date :** 2026-09-28
- **Verdict : FAIL** — le paradigme tient, mais la spine laisse des libertés qui permettent des builds mutuellement incompatibles. 8 trous identifiés (2 critiques, 3 majeurs, 3 moyens), à fermer par des AD nouveaux ou resserrés avant d'épicer.

---

## F1 — Enveloppe de valeur inter-étages jamais définie (CRITIQUE)

**Paire :** story « sélection de règle » (`rules.py`) × story « rendu du gabarit » (`render.py`).

- AD-1 impose « passage de valeur à l'étape suivante » mais **ne définit nulle part ce qu'est cette valeur**. Le Structural Seed et la Capability Map ne fixent aucune signature.
- Builder A (sélection) rend `(rule, payload: bytes)` — il traite le payload comme une chaîne opaque, son AD (AD-4) ne parle que de topic.
- Builder B (rendu) rend une fonction `render(rule, payload: dict)` — AD-5 dit « résolus contre le payload JSON du message », donc il suppose le JSON déjà parsé.

Les deux obéissent à tous les AD. Assemblés : `render` reçoit des octets, ou `rules` passe un dict alors que le payload n'est pas encore parsé — les étapes ne se composent pas. Variantes non tranchées : qui fait le `json.loads` (et le fait-il une seule fois ?) ; la règle voyage-t-elle comme objet immuable résolu au moment de la sélection, ou chaque étape relit-elle le ConfigStore par clé ? Dans le second cas, un swap entre sélection et rendu produit une **notification hybride** (nouveau gabarit, ancienne règle) sans qu'aucun AD soit violé — l'atomicité d'AD-3 porte sur le store, pas sur la cohérence d'un message en vol.

**Trou à fermer :** AD nouveau (ex. AD-10 « Contrat de message ») : une enveloppe figée (ex. `IncomingMessage` frozen dataclass : topic, payload brut, payload parsé ou `None`, règle résolue **une seule fois** à la sélection et immuable jusqu'à l'émetteur) ; désigner l'étape qui parse le JSON ; interdire aux étages aval de relire la règle dans le ConfigStore.

## F2 — Rechargement à chaud vs souscriptions MQTT : l'arête ConfigStore → source n'existe pas (CRITIQUE)

**Paire :** story « source MQTT » (`mqtt.py`) × story « rechargement à chaud » (`config.py`).

- Le diagramme du paradigme relie le ConfigStore à B, C, D, E — **jamais à A (source)**. La Capability Map de FR-1 liste AD-1, AD-2, AD-8 : AD-3 (ConfigStore) n'y figure pas.
- FR-1 impose pourtant de souscrire à « l'ensemble des topics définis par les Règles ».
- Builder A (source MQTT), ne voyant aucun AD le liant au ConfigStore, souscrit une fois au démarrage à la liste des topics des règles. Une règle ajoutant un topic `home/garage/porte` après rechargement ne reçoit **jamais** de message : FR-7/SM-2 sont silencieusement violés côté source, sans qu'il ait désobéi.
- Builder B, pour satisfaire FR-7 « au plus large », souscrit d'emblée à `#` et laisse `rules.py` filtrer. Littéralement conforme lui aussi (FR-2 : « ne correspondant à aucune Règle ne déclenche aucune notification » — vrai). Mais le service reçoit alors tout le trafic du broker, et le comportement diverge totalement d'un build à l'autre (bruit, volume, journalisation debug).

Par ailleurs, si l'on décide que la source doit se re-souscrire au swap, **aucun chemin ne le permet** : AD-1 (« sens unique strict », « jamais l'inverse ») interdit au ConfigStore ou au pipeline d'informer la source. Le re-souscription sur rechargement est architecturalement impossible en l'état.

**Trou à fermer :** AD nouveau (ex. AD-11 « Souscriptions pilotées par la configuration ») : (a) autoriser explicitement l'arête lecture ConfigStore → source (la source est une étape qui lit, comme les autres) ; (b) fixer la stratégie : souscription exacte à l'ensemble des topics des règles, recalculée à chaque swap atomique (ajouts/suppressions symétriques), interdiction du catch-all `#` ; (c) définir la sémantique de désabonnement des topics disparus.

## F3 — Sémantique du debounce non fixée + identité de règle à travers les rechargements (MAJEUR)

**Paire :** story « debounce » (`debounce.py`) × story « rechargement » (`config.py`).

Trois libertés laissées par AD-7, chacune suffisant à deux builds conformes de comportement opposé :

1. **Ouverture de la fenêtre.** « Message reçu pendant la fenêtre → ignoré » se lit de deux façons : builder A met à jour l'horodatage **au passage** (notification émise → fenêtre ouverte) ; builder B le met à jour **à chaque message reçu** sur la règle, même ignoré. Résultat sur un flot soutenu : A notifie une fois par fenêtre, B ne notifie **plus jamais** jusqu'au calme. FR-8 (« délai minimum entre deux notifications ») suggère A, mais AD-7 n'interdit pas B. Préciser aussi le bord d'attaque : premier message notifie immédiatement (leading), ce que l'esprit du texte implique mais ne dit pas.
2. **Identité de la règle.** « Par règle » sans définir la clé. Builder A clé = index de déclaration : après un rechargement où l'utilisateur réordonne ses règles, la fenêtre du bouton salon s'applique au capteur garage. Builder B clé = topic : or AD-4 **autorise** des topics en doublon (il prévoit l'avertissement de chevauchement), donc la clé est ambiguë, et un topic renommé perd sa fenêtre. Aucun des deux ne viole un AD.
3. **Swap et fenêtres.** AD-9 réinitialise les fenêtres au **redémarrage**, mais rien ne dit ce qu'un **rechargement** fait des fenêtres ouvertes. Builder A les conserve (clé identique), builder B les purge à chaque swap. Après chaque édition du YAML : soit une rafale peut notifier immédiatement, soit non.

**Trou à fermer :** resserrer AD-7 (fenêtre ouverte par notification émise, leading edge, purge ou conservation explicite au swap) + AD nouveau (identité de règle : clé `name` unique obligatoire dans le schéma YAML — voir F4 — servant à debounce et journaux).

## F4 — Schéma de règles, unités et moment de validation des gabarits non définis (MAJEUR)

**Paire :** story « schéma/validation YAML » (`config.py`) × story « rendu » (`render.py`).

AD-3 exige de « valider le fichier intégralement (schéma + limites) » mais **le schéma n'est défini nulle part** : ni les champs d'une règle, ni les types, ni les bornes.

- **Unité du debounce :** builder A du schéma accepte `debounce: 30` (secondes, lecture littérale du PRD) ; builder B du schéma documente des millisecondes. Les deux sont conformes — la même config produit un délai 1000× différent selon le build.
- **Bornes :** debounce négatif ? nul (équivalent à « sans délai » ou rejeté) ? énorme (24 h) ? Non tranché.
- **Validation du topic :** un topic contenant `+`/`#` mal placé, vide, avec espace — accepté au chargement puis explosant (ou silencieusement ne correspondant jamais) côté `rules.py` ? Non tranché.
- **Syntaxe de gabarit :** AD-5 définit la syntaxe `{champ}` / `{a.b}` mais ne dit pas **quand** une syntaxe malformée (`{capteur..`, accolade non fermée) est détectée. Builder A valide les gabarits au chargement → YAML rejeté, règles précédentes conservées (conforme AD-3). Builder B détecte au rendu → YAML accepté, la notification affiche le texte littéral `{capteur..` ou est ignorée. **Même fichier d'entrée, deux services aux comportements opposés, aucun des deux en tort.**

**Trou à fermer :** resserrer AD-3 : schéma de règle explicite (champs, types, unité — secondes, bornes du debounce, format de topic valide, unicité du `name` de F3) ; resserrer AD-5 : la syntaxe des gabarits est validée **au chargement** dans AD-3, un gabarit malformé rejette le YAML entier.

## F5 — Modèle d'exécution intra-pipeline, retry FCM et ordre des notifications (MAJEUR)

**Paire :** story « émetteur FCM / retry » (`notifier.py`) × story « pipeline / SM-1 » (`main.py`).

AD-2 impose une seule boucle asyncio mais ne dit pas **comment les messages s'y répartissent** :

- Builder A : `main.py` traite les messages **séquentiellement** dans une unique coroutine de consommation. L'émetteur de builder C fait un retry inline avec backoff borné (AD-6 l'autorise : `throttled/internal/network_error` → retry). Résultat : un backoff de plusieurs secondes sur le message n°1 **retient** le message n°2 → SM-1 (latence < 2 s) violé par assemblage de deux unités pourtant conformes.
- Builder B : `main.py` lance une tâche par message. Aucune violation d'AD-2 (même boucle). Mais alors : un send en retry et un send frais pour la **même règle** peuvent se croiser — la notification retryée arrive **après** la nouvelle → ordre inversé sur le téléphone, et deux appels FCM concurrents pour la même règle. Rien dans AD-6 ne fixe d'ordre ni de sérialisation.

**Trou à fermer :** AD nouveau (ex. AD-12 « Modèle d'exécution du pipeline ») : une tâche par message (ou file entre debounce et émetteur), l'émetteur est **single-flight FIFO** (au plus un envoi en cours, les suivants attendent — ce qui borne aussi la latence : préciser si un send en attente peut être coalescé/abandonné), le retry ne bloque jamais le traitement des messages suivants.

## F6 — Contrat de démarrage absent + messages retained non traités (MOYEN)

**Paire :** story « assemblage/démarrage » (`main.py`) × story « déploiement » (`deploy/mqtt-notif.service`).

Les Consistency Conventions disent « les erreurs ne sont jamais fatales **après le démarrage** » — donc un régime d'erreurs fatales *au* démarrage existe, sans jamais être défini :

- YAML absent ou invalide **au premier chargement** : builder A sort en non-zéro (systemd `Restart=on-failure` → boucle de crash) ; builder B démarre avec un ensemble de règles vide et attend (lecture défendable d'AD-3 : « laisse les règles précédentes actives » — au premier chargement, il n'y en a pas). AD-6 ne couvre que le token absent, pas le YAML ni le fichier service-account absent/illisible.
- Broker injoignable au démarrage : builder A termine en erreur (le backoff devient l'affaire de systemd) ; builder B boucle interne de reconnexion non fatale (lecture de FR-1). Deux cycles de vie opérés différents, `Restart=` rendus incohérents entre eux.
- **Messages retained :** ni la spine ni le PRD n'en parlent. Au démarrage et à **chaque reconnexion**, Mosquitto redélivre les messages retained des topics souscrits (QoS 0). Builder A les notifie normalement ; builder B consulte le drapeau `retain` de paho et les ignore. Sur un topic d'état retained (usage domotique banal — l'utilisateur peut en ajouter une règle), le build A produit **une notification à chaque redémarrage et à chaque reconnexion**, ce qui contredit l'esprit de SM-C1 (« le silence reste la norme »). AD-9 réinitialise les fenêtres de debounce au redémarrage, donc le debounce n'est d'aucun secours ici.
- Arrêt (SIGTERM) : drain des envois en cours ou annulation immédiate ? Non tranché (lié à F5).

**Trou à fermer :** AD nouveau (ex. AD-13 « Contrat de cycle de vie ») : liste fermée des conditions fatales au démarrage (YAML absent/invalide au premier chargement, service-account absent — token absent restant non fatal per AD-6), politique broker (reconnexion interne non fatale dès le premier échec), comportement SIGTERM, et politique retained explicite (proposition : ignorer les messages retained au premier delivery après souscription).

## F7 — Troncature : double propriété possible, sémantique de coupe non fixée (MOYEN)

**Paire :** story « rendu » (`render.py`, seed : « substitution + limites », AD-5) × story « émetteur » (`notifier.py`, AD-6 : « un rendu hors limites est tronqué proprement aux limites »).

Les **deux** ADs revendiquent la troncature. Builder A tronque dans le rendu (100/500/3500 octets) ; builder B valide et tronque dans l'émetteur. Chacun est conforme à son AD ; les deux ensemble, c'est de la double troncature — bénigne seulement si la sémantique est identique, or elle n'est pas fixée :

- **Coupe UTF-8 :** tronquer à 100 octets peut couper au milieu d'un caractère multi-octets → payload invalide, précisément ce qu'interdit « jamais envoyé corrompu ». Coupe au point de code le plus proche ? Ellipse `…` ajoutée (et comptée dans la limite) ou pas ?
- **Valeurs substituées non scalaires :** que substitue `{capteur}` si le champ est un dict, une liste, ou `null` ? AD-5 ne couvre que l'absence (`[absent]`). Builder A : `str()` Python (`"{'a': 1}"`) ; builder B : `json.dumps`. Titres de formes différentes, tous conformes.
- **Titre vide après substitution :** un champ **présent mais vide** n'est pas « absent » (pas de `[absent]`), et AD-6 exige un titre non vide — mais ne dit pas ce que fait l'émetteur d'un titre vide : abandon silencieux, journalisé, remplacement par un texte de secours ? Builder A abandonne, builder B substitue un repli et envoie.

**Trou à fermer :** resserrer AD-5/AD-6 : un seul propriétaire de la troncature (recommandé : le rendu produit des chaînes toujours valides ; l'émetteur ne fait que vérifier en assertion), coupe au point de code, sans ellipse (ou ellipse comptée dans la limite — trancher), règle de rendu des non-scalaires, et politique explicite pour « requis vide après substitution ».

## F8 — Open Question « placement du device token » : à moitié tranchée, divergence permise (MOYEN)

**Paire :** story « configuration » (`config.py`) × story « déploiement » (`deploy/mqtt-notif.service`).

L'Open Question finale (« un seul YAML pour broker + token + règles, ou séparer secrets — **trancher avant le build** ») est contredite en creux par la spine elle-même : les Consistency Conventions fixent déjà « device token renseigné manuellement **dans le YAML** », tandis qu'AD-8 ne passe en variables d'environnement que « le chemin du fichier de règles et celui des identifiants FCM (service-account) ». Une story « déploiement » qui tranche l'open question dans le sens « séparer les secrets » passe le token par env var ; la story « configuration » l'attend dans le YAML. Les deux se réfèrent à des énoncés de la spine, ils divergent. C'est exactement le type d'Open Question qui ne doit pas survivre à la relecture.

**Trou à fermer :** clore l'Open Question maintenant dans la spine (le YAML unique semble être la décision déjà prise — l'écrire comme AD ou convention, et supprimer la question ouverte). Profiter de l'AD de F4 pour fixer la section broker du schéma (hôte, port) puisque FR-1 en dépend.

---

## Examen des zones volontairement floues

| Zone | Divergence réelle possible entre deux unités ? |
| --- | --- |
| Deferred — arborescence fine au-delà du seed | Non. Les modules du seed suffisent à cadrer les stories ; découpage interne sans impact inter-unités. |
| Deferred — stratégie de test (pytest, mocks) | Non. Sans effet inter-unités, conforme à l'altitude. |
| Deferred — constantes (backoff max, retries, période mtime, textes de log) | Non, à une exception près : si le **nombre de retries** et le **backoff max** restent libres, ils déterminent la latence maximale de F5 et donc la faisabilité de SM-1. Recommandation : borner dans l'AD de F5 (« le chemin de retry ne retarde jamais un message frais »), les constantes restant libres. |
| Deferred — permissions service-account, durcissement systemd | Non. Déploiement, sans impact inter-unités. |
| Deferred — auth broker/TLS, multi-appareil, interface web, historique | Non. Exclusions v1 nettes, sans ambiguïté exploitable. |
| Open Question — payloads JSON | Non, dégradation sûre : AD-5 prévoit le repli (message ignoré, journalisé) si le payload n'est pas JSON. À confirmer sur le broker réel comme indiqué. |
| Open Question — machine dédiée / Python 3.13 | Non, hypothèse d'environnement. |
| Open Question — asyncio/aiomqtt vs paho threads | Non : AD-2 tranche déjà dans les faits (« tout s'exécute sur une seule boucle asyncio »). La question devrait être marquée résolue pour éviter qu'une story ne la « re-tranche » à l'envers. |
| Open Question — format du fichier de configuration | **Oui — voir F8.** |

## Consolidation — ADs à créer ou resserrer

1. **AD-10 (nouveau) — Contrat de message inter-étages :** enveloppe figée (topic, payload brut, payload parsé, règle résolue une fois et immuable) ; désignation de l'étape qui parse le JSON. (F1)
2. **AD-11 (nouveau) — Souscriptions pilotées par la configuration :** arête lecture ConfigStore → source autorisée ; souscription exacte recalculée à chaque swap ; `#` interdit ; sémantique de désabonnement. (F2)
3. **AD-7 (resserré) — Debounce :** fenêtre ouverte par notification émise (pas par message reçu), leading edge, comportement au swap ; + clé d'identité de règle (`name` unique). (F3, avec F4)
4. **AD-3 (resserré) — Schéma YAML explicite :** champs, types, unités (secondes), bornes, validation des topics et de la syntaxe des gabarits **au chargement**. (F4)
5. **AD-12 (nouveau) — Modèle d'exécution du pipeline :** tâche par message ; émetteur single-flight FIFO ; le retry ne retarde jamais un message frais. (F5)
6. **AD-13 (nouveau) — Contrat de cycle de vie :** liste fermée des fatals au démarrage ; reconnexion interne non fatale dès le premier échec ; comportement SIGTERM ; politique des messages retained. (F6)
7. **AD-5/AD-6 (resserrés) — Troncature et rendu :** propriétaire unique de la troncature, coupe au point de code, règle pour les valeurs non scalaires et pour un champ requis vide après substitution. (F7)
8. **Open Questions (closes) :** placement du token (décision actée dans la spine) ; question asyncio marquée résolue. (F8)

## Ce qui résiste à l'attaque

- AD-1 (unicité du flux et du point de sortie) et AD-6 (rédacteur unique FCM) ferment efficacement les contournements de frontière — aucune paire conforme ne peut court-circuiter l'émetteur.
- AD-4 (première règle gagnante) verrouille SM-C1 au niveau du filtrage : aucun couple `rules.py`/`debounce.py` conforme ne peut double-notifier un même message.
- AD-3 couvre la cohérence du *store* (swap atomique, rejet du YAML invalide avec conservation des règles précédentes) — les trous trouvés (F1, F3, F4) sont sur la *consommation* de la config, pas sur sa tenue.
- AD-9 (perte assumée, QoS 0) élimine proprement toute la famille d'attaques « file durable au cas où ».
- Le périmètre Deferred est tenu : les reports sont tous sans effet inter-unités (réserve mineure sur les constantes de retry, voir tableau).
