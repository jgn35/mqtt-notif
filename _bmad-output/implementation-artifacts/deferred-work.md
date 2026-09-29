- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-tranche-fine-mqtt-fcm.md`
  summary: `RESTART_DELAY_S` ne s'applique que sur exception du pipeline ; un retour propre sans stop relancerait en boucle serrée.
  evidence: Chemin inatteignable en story 1.1 (`iter_messages` ne retourne que sur stop) ; deviendra réel si la story 2.1 donne à la source un chemin de retour propre — appliquer le délai sur tout restart à ce moment.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-tranche-fine-mqtt-fcm.md`
  summary: `message_payload` (`mqtt_notif/mqtt.py`) n'a pas d'appelant en production en 1.1 (message FCM fixe).
  evidence: La story 1.2 introduit l'objet événement topic + payload brut figé (AD-10) et consommera cette fonction ; la supprimer maintenant serait du churn.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-tranche-fine-mqtt-fcm.md`
  summary: Aucune infrastructure lint/CI (ruff, mypy, workflow) dans le dépôt.
  evidence: Hors périmètre de toutes les stories du sprint ; à considérer comme travail d'entretien séparé si le projet devient multi-contributeur.
