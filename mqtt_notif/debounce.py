"""Fenêtre de debounce par règle, en mémoire, ancrée à la réception du message.

La fenêtre d'une règle s'ouvre au moment où le filtre accepte un message
(décision de notification, horodatée à la réception) : elle ne dépend jamais
du résultat de l'envoi — un envoi FCM en échec ne libère pas la fenêtre, le
message suivant dans le délai reste silencié. Un message reçu pendant
« accepté à + debounce_ms » est ignoré (aucune notification), sans
repositionner la fenêtre — seule une décision d'acceptation la prolonge.
L'horodatage de réception est capturé en amont, jamais à l'envoi : un envoi
FCM lent n'étend jamais la fenêtre. Clé = nom unique de la règle : les
fenêtres ne se partagent jamais entre règles. État en mémoire, propriété du
pipeline : jamais persisté (un redémarrage réinitialise tout), jamais
partagé avec une autre étape. Au swap de configuration, `purge` retire les
fenêtres des règles disparues et conserve celles des règles restées.

Boucle asyncio unique : simple dict nom -> horodatage, aucun thread, aucun
verrou externe (mono-boucle) ; l'horloge (time.monotonic) est injectable pour
des tests déterministes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection

from .rules import Rule

# received_at est en secondes (time.monotonic), debounce_ms en millisecondes.
MS_PER_S = 1000.0


class Debouncer:
    """État du debounce par règle : nom -> horodatage de réception du dernier
    message accepté par le filtre."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._notified_at: dict[str, float] = {}

    def now(self) -> float:
        """Horodatage de réception (secondes monotones, horloge injectable)."""
        return self._clock()

    def should_notify(self, rule: Rule, received_at: float | None = None) -> bool:
        """Vrai si le message reçu à `received_at` passe le filtre (hors fenêtre).

        `received_at` vient de la réception du message (jamais de l'envoi) ;
        absent, il est pris sur l'horloge injectée. Sans délai (0 ou moins,
        défaut 0), chaque message passe et aucun état n'est tenu. La fenêtre
        s'ouvre à cette décision d'acceptation, indépendamment du succès de
        l'envoi ultérieur ; un message refusé ne la repositionne pas.
        """
        if rule.debounce_ms <= 0:
            # Défensif : hors bornes, le chargement rejette déjà ; un
            # debounce_ms négatif est traité comme l'absence de délai.
            self._notified_at.pop(rule.name, None)
            return True
        if received_at is None:
            received_at = self._clock()
        last = self._notified_at.get(rule.name)
        if last is not None and (received_at - last) * MS_PER_S < rule.debounce_ms:
            return False
        self._notified_at[rule.name] = received_at
        return True

    def remaining_ms(self, rule: Rule, received_at: float) -> float:
        """Millisecondes restant dans la fenêtre de la règle pour un message
        reçu à `received_at` (0 si la fenêtre est fermée ou inexistante)."""
        last = self._notified_at.get(rule.name)
        if last is None or rule.debounce_ms <= 0:
            return 0.0
        return max(0.0, rule.debounce_ms - (received_at - last) * MS_PER_S)

    def purge(self, kept_names: Collection[str]) -> None:
        """Au swap de config : retire les fenêtres des règles disparues,
        conserve celles des règles conservées — une règle redéfinie sous le
        même nom garde donc sa fenêtre (la clé est le nom, seul le nouveau
        debounce_ms s'applique à la suite)."""
        self._notified_at = {
            name: at for name, at in self._notified_at.items() if name in kept_names
        }
