- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-tranche-fine-mqtt-fcm.md`
  summary: `RESTART_DELAY_S` ne s'applique que sur exception du pipeline ; un retour propre sans stop relancerait en boucle serrée.
  evidence: Chemin inatteignable en story 1.1 (`iter_messages` ne retourne que sur stop) ; deviendra réel si la story 2.1 donne à la source un chemin de retour propre — appliquer le délai sur tout restart à ce moment.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-tranche-fine-mqtt-fcm.md`
  summary: `message_payload` (`mqtt_notif/mqtt.py`) n'a pas d'appelant en production en 1.1 (message FCM fixe).
  evidence: La story 1.2 introduit l'objet événement topic + payload brut figé (AD-10) et consommera cette fonction ; la supprimer maintenant serait du churn.
- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-tranche-fine-mqtt-fcm.md`
  summary: Aucune infrastructure lint/CI (ruff, mypy, workflow) dans le dépôt.
  evidence: Hors périmètre de toutes les stories du sprint ; à considérer comme travail d'entretien séparé si le projet devient multi-contributeur.
- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-service-systemd-resident.md`
  summary: Le démon ne fail-fast pas au démarrage sur un service-account FCM absent/invalide — l'init firebase est paresseuse (premier envoi, `mqtt_notif/notifier.py:263-280`) ; le service reste actif et les notifications échouent envoi par envoi.
  evidence: Comportement préexistant (story 1.x) que la contrainte Always de la spec 3.1 suppose à tort (« service-account manquant → échec vite, état failed ») ; vérifié en boucle de revue 2. À trancher au niveau du démon (validation `credentials.Certificate` au démarrage dans `main()`, story dédiée) ou renégocier l'intention ; la gate d'installation de 3.1 couvre le cas au moment de l'activation seulement.
