---
title: 'Story 1.4 — Contrat FCM et robustesse de l'émetteur'
type: 'feature'
created: '2026-09-28'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 065c6d8
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** L'émetteur envoie ce que le rendu produit sans contrôler le contrat de l'app mcp-notif, et aucune politique de retry ne distingue les erreurs : un message hors limites ou une erreur FCM passagère se traduit par une notification perdue ou un comportement erratique.

**Approach:** Consolider `notifier.py` en frontière à rédacteur unique : validation avant tout appel FCM (`title`/`short_message` non vides, tailles en octets UTF-8 ≤ 100 / ≤ 500 / ≤ 3 500), troncature propre à la frontière de caractère, mapping des erreurs FCM (`unregistered`/`invalid_argument`/`permission_denied` → sans retry ; `throttled`/`internal`/`network_error` → retry avec backoff borné) — aucune combinaison d'erreurs n'arrête jamais le service.

## Boundaries & Constraints

**Always:**
- Validation avant tout appel FCM : `title` et `short_message` non vides après rendu ; tailles en octets UTF-8 : `title` ≤ 100, `short_message` ≤ 500, `detailed_message` ≤ 3 500 quand présent.
- Hors limites → tronqué proprement à la frontière de caractère (jamais au milieu d'un octet UTF-8, jamais de message corrompu ni de rejet silencieux).
- Champs vides (`title` ou `short_message` vide après rendu) → envoi refusé, erreur explicite journalisée, service continue.
- `detailed_message` absent → clé non envoyée (inchangé).
- Erreurs sans retry : `unregistered`, `invalid_argument`, `permission_denied` → journalisées avec erreur explicite, aucun retry.
- Erreurs avec retry : `throttled`, `internal`, `network_error` → retries avec backoff borné (nombre de retries et délais bornés par des constantes), puis abandon journalisé si tout échoue ; le service continue.
- Adressement par device token, jamais par `fid` (AD-6 — le DeprecationWarning firebase-admin est assumé).
- Frontière à rédacteur unique : seul `notifier.py` construit et envoie des messages FCM.
- Chaque envoi (avec ses retries) reste une tâche asyncio indépendante ; la boucle n'attend jamais ; appels bloquants via `asyncio.to_thread`.

**Never:**
- Aucun retry sur les erreurs sans retry ; aucun backoff non borné ; aucune persistance d'état d'envoi.
- Pas d'envoi corrompu (troncature octet) ; jamais de notification vide vers le téléphone.
- Pas de modification du moteur de rendu (hors périmètre) ; pas de debounce ni de rechargement (stories suivantes).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Titre hors limites | rendu `title` > 100 octets UTF-8 | tronqué à 100 octets à la frontière de caractère, envoyé | Jamais corrompu |
| Message court hors limites | rendu `short_message` > 500 octets | tronqué à 500 octets, envoyé | N/A |
| Message détaillé hors limites | `detailed_message` > 3 500 octets | tronqué à 3 500 octets, envoyé | N/A |
| Champs vides | `title` ou `short_message` vide après rendu | envoi refusé, erreur explicite journalisée | Service continue |
| Erreur sans retry | `unregistered`, `invalid_argument`, `permission_denied` | journalisée, aucun retry, message suivant traité | Service continue |
| Erreur avec retry | `throttled`, `internal`, `network_error` | retries à backoff borné, puis abandon journalisé | Service continue |
| Rafale d'erreurs mixtes | combinaison des cas ci-dessus | le démon reste connecté, traite les messages suivants | Jamais de crash |
| Envoi lent + erreurs | mock FCM qui traîne puis échoue | la boucle ne bloque jamais, chaque envoi reste indépendant | N/A |

</frozen-after-approval>

## Code Map

- `mqtt_notif/notifier.py` -- à consolider : constantes de limites (octets UTF-8), validation avant appel, troncature à la frontière de caractère, mapping des exceptions firebase-admin vers la politique retry/sans-retry, boucle de retries à backoff borné (constantes : nombre de retries, délais), `to_thread` conservé pour chaque appel bloquant.
- `mqtt_notif/__init__.py` -- sans changement attendu (log_line).
- `tests/test_notifier.py` -- à étendre : troncature (frontière de caractère, multi-octets UTF-8), champs vides, chaque type d'erreur FCM et sa politique, rafale mixte sans crash, indépendance des envois.
- `tests/` -- contrôle de frontière : grep-verifiable que seul notifier.py référence messaging/firebase_admin (AC-1.4.6).

## Tasks & Acceptance

**Execution:**
- [x] `mqtt_notif/notifier.py` -- validation des limites + troncature à la frontière de caractère UTF-8 -- AC-1.4.1, AC-1.4.3.
- [x] `mqtt_notif/notifier.py` -- rejet des champs vides avec erreur explicite journalisée -- AC-1.4.2.
- [x] `mqtt_notif/notifier.py` -- mapping erreurs FCM sans retry (`unregistered`, `invalid_argument`, `permission_denied`) -- AC-1.4.4.
- [x] `mqtt_notif/notifier.py` -- retries à backoff borné sur `throttled`, `internal`, `network_error`, puis abandon journalisé -- AC-1.4.5.
- [x] `tests/` -- couvrir la matrice I/O, la rafale mixte sans crash, et la frontière à rédacteur unique (test qui échoue si un autre module importe firebase_admin) -- AC-1.4.6, AC-1.4.7.

**Acceptance Criteria:**
- Given un rendu hors limites, when l'envoi s'apprête, then valeurs tronquées aux limites du contrat, à la frontière de caractère.
- Given `title` ou `short_message` vide, when l'envoi s'apprête, then envoi refusé avec erreur journalisée, service continue.
- Given chaque erreur sans retry, when elle remonte, then journalisée, aucun retry tenté.
- Given chaque erreur avec retry, when elle remonte, then retries bornés puis abandon journalisé ; le démon reste connecté et traite les messages suivants dans tous les cas.

## Implementation Notes

Décisions internes (non perceptibles pour l'utilisateur) :
- Mapping des exceptions : firebase-admin lève des sous-classes de `messaging.exceptions`/`FirebaseError` avec codes — mapper par type d'exception (UnregisteredError, InvalidArgumentError, PermissionDeniedError d'un côté ; QuotaExceededError/`throttled`, InternalError, réseau (ConnectionError/requests exceptions) de l'autre) ; toute erreur non mappée est traitée comme sans retry (conservateur).
- Troncature : encodage UTF-8, coupe au plus grand préfixe ≤ limite, décodage strict — jamais au milieu d'un octet.
- Le délai de backoff reste court et borné (l'envoi est une tâche indépendante ; le backoff ne bloque jamais la boucle).
- Les retries s'appliquent au seul appel FCM, jamais au rendu.

## Review Triage Log

- `notifier.py:RETRYABLE_ERRORS` — les erreurs transport réelles de firebase-admin (`UnavailableError`, `DeadlineExceededError`) ne sont pas mappées : une panne réseau passe par le chemin non-mappé sans retry, contredisant AC-1.4.5 (`network_error` → retry) — high — réel, confirmé sur la hiérarchie d'exceptions firebase-admin 7.7.0 — patch (mapping complété + test de disjonction des deux politiques).
- `notifier.py:notify` — le bloc validation/troncature s'exécute hors de toute contention : une clé inattendue (KeyError) ou valeur non-chaîne (AttributeError) échapperait à « ne lève jamais », et l'exception serait silencieuse (done-callback ne fait que discard) — medium — réel structurellement (injoignable aujourd'hui, mais la garantie est le contrat) — patch (containment défensif).
- `pyproject.toml` — `requests` importé directement mais non déclaré (résolution transitive par firebase-admin seulement) — medium — réel — patch (déclaration).
- `notifier.py:_truncate_utf8` — troncature silencieuse : l'opérateur ne peut pas savoir qu'un contenu a été amputé (« jamais rejeté silencieusement ») — medium — réel — patch (une ligne de journal par champ réellement tronqué).
- `notifier.py` — `detailed_message` rendu vide : clé envoyée vide vers l'app (asymétrie de validation) — low — réel — patch (clé omise si vide, cohérent avec AC-1.3.9).
- `notifier.py/main.py` — pire cas de retries (1+2+4 = 7 s) supérieur au budget d'arrêt (SHUTDOWN_TIMEOUT_S = 5 s) : tout envoi throttled à l'arrêt est systématiquement abandonné — low — réel, conflit de constantes indépendantes — patch (délais raccourcis à (0.5, 1.0, 2.0), commentaire liant les deux constantes).
- `notifier.py` — tailles par champ 100+500+3 500 = 4 100 octets > limite FCM 4 096 : message rejeté par FCM si les trois champs sont au maximum — low — réel mais improbable — patch (garde-fou : budget total, `detailed_message` rogné en dernier).
- `notifier.py` — `messaging.send` sans délai : un appel qui pend occupe la tâche et le thread indéfiniment, aucun abandon — medium — réel (transport requests sans timeout) — patch (wait_for borné par constante, TimeoutError traité network_error retryable).
- `tests/` — les délais réels de backoff ne sont jamais affirmés dormis (tous les tests les patchent à zéro ; mutation supprimant le sommeil reste verte) — medium — réel, prouvé par mutation — patch (espion sur asyncio.sleep).
- `tests/` — aucune rencontre testée entre un envoi en retry et le drain d'arrêt borné (mutation avalant CancelledError reste verte) — medium — réel — patch (test pipeline avec vrai Notifier).
- `tests/test_notifier.py` — test lent/échouant basé sur des budgets d'horloge (rendez-vous 0.02 s) — low — risque de flake réel — patch (synchronisation par Event).
- Pas de jitter dans le backoff — low — hors périmètre (échelle domestique, rafales traitées par debounce en 2.2) — rejet.
- Token mort jamais agrégé (log par événement, pas de signal unique) — false — FR-6/AD-6 exigent l'erreur explicite journalisée par événement, sans retry ; l'agrégation serait un état hors périmètre — rejet.
- Garde-frontière par scan textuel (pas AST) — low — adéquat pour AC-1.4.6 (« contrôlable par grep »), le passage à AST serait de la complexité — rejet.

## Verification

**Commands:**
- `uv run pytest -q` -- expected: tous les tests passent, chaque AC automatisable couverte.
