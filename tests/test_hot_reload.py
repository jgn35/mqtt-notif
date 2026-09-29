"""Story 2.1 : rechargement à chaud avec swap atomique — ConfigStore (sondage
mtime, validation avant swap, rejet explicite YAML invalide et host/port),
effet à l'événement suivant, resynchronisation des souscriptions sans
déconnexion, rotation du device token, lecteur unique du YAML (grep)."""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import signal
import time
from pathlib import Path

import aiomqtt
import pytest
import yaml

import mqtt_notif
import mqtt_notif.config as config_module
import mqtt_notif.mqtt as mqtt_module
from mqtt_notif.config import Config, ConfigError, ConfigStore, load_config
from mqtt_notif.main import Event, build_event, run_pipeline, serve
from mqtt_notif.mqtt import iter_messages, message_payload
from mqtt_notif.notifier import Notifier
from mqtt_notif.render import render_message
from mqtt_notif.rules import Rule

from .conftest import Episode, FakeMessage

ISO_UTC = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00\] ")

DEFAULT_RULES = (("salon", "home/salon/capteur", "Capteur salon", "Valeur: {valeur}"),)

_MTIME_NS = itertools.count(1)


def bump_mtime(path: Path) -> None:
    """mtimes strictement croissants et déterministes (indépendants du FS)."""
    ns = next(_MTIME_NS) * 1_000_000
    os.utime(path, ns=(ns, ns))


def write_yaml(
    path: Path,
    *,
    host: str = "localhost",
    port: int = 1883,
    token: str = "token-a",
    rules=DEFAULT_RULES,
) -> Path:
    """Écrit un YAML valide (règles : name, topic, title, short_message),
    échappement garanti par safe_dump (une valeur avec guillemet reste du
    YAML valide), et bombe le mtime : le sondage du store verra le changement."""
    data = {
        "broker": {"host": host, "port": port},
        "device_token": token,
        "rules": [
            {"name": name, "topic": topic, "title": title, "short_message": short_message}
            for name, topic, title, short_message in rules
        ],
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    bump_mtime(path)
    return path


def file_store(path: Path) -> ConfigStore:
    """Store sur fichier réel : rechargement à chaud actif."""
    return ConfigStore(load_config(str(path)), str(path))


class SilentNotifier:
    """Notifier bouchon pour serve : aucune dépendance FCM."""

    async def notify(self, event) -> None:
        pass


class CountingNotifier:
    """Notifier compteur : capture les événements notifiés (dans l'ordre)."""

    def __init__(self):
        self.events = []

    async def notify(self, event) -> None:
        self.events.append(event)

    @property
    def calls(self) -> int:
        return len(self.events)


# --- ConfigStore : chargement initial, sondage, swap atomique, rejets ---


def test_store_get_returns_initial_snapshot(tmp_path):
    """AC-2.1.5 : get() renvoie la configuration initiale figée."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    config = store.get()
    assert config.device_token == "token-a"
    assert [rule.name for rule in config.rules] == ["salon"]


async def test_poll_without_change_is_silent(tmp_path, capsys):
    """Aucun changement de mtime : aucun rechargement, aucune ligne de journal."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    before = store.get()

    await store.poll()

    assert store.get() is before
    assert capsys.readouterr().out == ""


async def test_reload_swaps_whole_config_logs_once(tmp_path, capsys):
    """AC-2.1.1 / AC-2.1.5 : mtime changé -> validation puis swap intégral
    (token, règles, libellés : jamais un mélange), une seule ligne de journal."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()

    write_yaml(
        path,
        token="token-b",
        rules=(
            ("salon", "home/salon/capteur", "Capteur salon v2", "Valeur: {valeur} (v2)"),
            ("garage", "home/garage/#", "Garage", "Portail: {action}"),
        ),
    )
    await store.poll()

    new = store.get()
    assert new is not initial
    assert new.device_token == "token-b"
    assert new.rules[0].title == "Capteur salon v2"
    assert [rule.name for rule in new.rules] == ["salon", "garage"]
    assert new.broker_host == initial.broker_host  # inchangé : swap intégral, pas créatif
    assert store.changed.is_set()  # armé : la source MQTT resynchronisera

    out = capsys.readouterr().out
    assert out.count("config reloaded") == 1
    assert "config reloaded rules=2 topics=home/garage/#,home/salon/capteur" in out
    assert ISO_UTC.match(out)
    # Sondage suivant sans nouveau changement : aucune ligne en double.
    await store.poll()
    assert capsys.readouterr().out == ""


async def test_swap_atomic_never_mixed_view(tmp_path):
    """AC-2.1.5 : le swap est une réaffectation unique d'attribut — le
    snapshot après rechargement est intégralement la nouvelle version
    (token, règles et libellés du même fichier), jamais un mélange."""
    path = write_yaml(tmp_path / "config.yaml", token="token-a")
    store = file_store(path)
    before = store.get()
    assert (before.device_token, len(before.rules)) == ("token-a", 1)

    write_yaml(
        path,
        token="token-b",
        rules=(
            ("salon", "home/salon/capteur", "Capteur salon", "Valeur: {valeur}"),
            ("garage", "home/garage/#", "Garage", "Portail: {action}"),
        ),
    )
    await store.poll()

    after = store.get()
    assert after is not before
    assert (after.device_token, len(after.rules)) == ("token-b", 2)
    assert [rule.name for rule in after.rules] == ["salon", "garage"]
    assert [rule.title for rule in after.rules] == ["Capteur salon", "Garage"]
    # L'ancien snapshot reste intégralement l'ancienne version.
    assert (before.device_token, len(before.rules)) == ("token-a", 1)


def test_store_init_missing_file_raises_config_error(tmp_path):
    """Fichier disparu entre le chargement initial et la construction du
    store : ConfigError explicite (échec rapide au démarrage), jamais
    d'OSError brut remonté hors de main()."""
    path = write_yaml(tmp_path / "config.yaml")
    config = load_config(str(path))
    os.remove(path)
    with pytest.raises(ConfigError, match="introuvable"):
        ConfigStore(config, str(path))


async def test_disappeared_file_logged_once_then_reload_on_return(tmp_path, capsys):
    """Suppression définitive du fichier : une ligne à la transition vers
    l'absence (pas à chaque sondage), rechargements inopérants jusqu'au
    retour du fichier — qui recharge normalement."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()

    os.remove(path)
    await store.poll()
    out = capsys.readouterr().out
    assert out.count("config file disappeared") == 1
    assert "inopérants" in out
    assert store.get() is initial

    await store.poll()
    assert capsys.readouterr().out == ""  # pas de log par sondage

    write_yaml(path, token="token-b")
    await store.poll()
    out = capsys.readouterr().out
    assert "config file disappeared" not in out
    assert "config reloaded" in out
    assert store.get().device_token == "token-b"

    # Nouvelle disparition : nouvelle transition, une seule ligne encore.
    os.remove(path)
    await store.poll()
    assert capsys.readouterr().out.count("config file disappeared") == 1


async def test_unexpected_load_error_rejected_watcher_survives(
    tmp_path, monkeypatch, capsys
):
    """Toute erreur de chargement (pas seulement ConfigError) est rejetée
    avec une trace explicite : l'ancienne config reste active et le
    rechargement suivant fonctionne — le watcher ne peut pas mourir d'un
    mauvais fichier."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()
    real_load = config_module.load_config

    def boom(path_arg):
        raise RuntimeError("bug de chargement")

    monkeypatch.setattr(config_module, "load_config", boom)
    write_yaml(path, token="token-b")
    await store.poll()

    assert store.get() is initial
    out = capsys.readouterr().out
    assert out.count("config reload rejected") == 1
    assert "RuntimeError" in out
    assert "bug de chargement" in out

    # Le watcher survit : le chargement suivant recharge normalement.
    monkeypatch.setattr(config_module, "load_config", real_load)
    write_yaml(path, token="token-c")
    await store.poll()
    assert store.get().device_token == "token-c"
    assert "config reloaded" in capsys.readouterr().out


async def test_same_mtime_rewrite_detected_by_size(tmp_path):
    """Réécriture à mtime identique (granularité du FS simulée) mais à
    taille différente : détectée par la taille, rechargement effectif."""
    path = write_yaml(tmp_path / "config.yaml", token="token-a")
    store = file_store(path)
    before_mtime = os.stat(path).st_mtime_ns

    write_yaml(path, token="token-avec-un-nom-beaucoup-plus-long")
    os.utime(path, ns=(before_mtime, before_mtime))  # même mtime, taille changée

    await store.poll()

    assert store.get().device_token == "token-avec-un-nom-beaucoup-plus-long"


async def test_replacement_during_load_repolled_immediately(tmp_path, monkeypatch):
    """Le fichier est remplacé pendant le chargement : le re-stat d'après
    swap déclenche un re-sondage immédiat — aucune base périmée figée, la
    version la plus récente gagne."""
    path = write_yaml(tmp_path / "config.yaml", token="token-a")
    store = file_store(path)
    real_load = config_module.load_config
    rewritten = []

    def load_then_rewrite(path_arg):
        config = real_load(path_arg)  # lit token-b
        if not rewritten:
            rewritten.append(1)
            write_yaml(Path(path_arg), token="token-c")  # remplacement pendant
        return config

    monkeypatch.setattr(config_module, "load_config", load_then_rewrite)
    write_yaml(path, token="token-b")
    await store.poll()

    assert store.get().device_token == "token-c"
    # Point fixe atteint : un sondage suivant ne recharge pas dans le vide.
    await store.poll()
    assert store.get().device_token == "token-c"


@pytest.mark.parametrize(
    "broken",
    [
        "broker: [non, fermé\n  host: x\n",  # YAML corrompu
        "broker:\n  host: localhost\n  port: 1883\ndevice_token: t\n"
        "rules:\n  - name: salon\n    topic: home/x\n    title: T\n"
        "    short_message: 'Valeur {champ\n",  # gabarit cassé
        "broker:\n  host: localhost\n  port: 1883\ndevice_token: t\n"
        "rules:\n  - name: salon\n    topic: home/x\n    title: T\n"
        "    short_message: M\nextra: inconnu\n",  # clé racine inconnue
    ],
    ids=["yaml-corrompu", "gabarit-casse", "cle-inconnue"],
)
async def test_invalid_yaml_rejected_service_continues(tmp_path, capsys, broken):
    """AC-2.1.2 : YAML invalide -> erreur explicite journalisée, ancienne config
    conservée (identité), service continue : le YAML valide suivant recharge."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()

    path.write_text(broken, encoding="utf-8")
    bump_mtime(path)
    await store.poll()

    assert store.get() is initial  # l'ancienne config reste active, en bloc
    assert not store.changed.is_set()
    out = capsys.readouterr().out
    assert out.count("config reload rejected") == 1
    assert ISO_UTC.match(out)

    # Le service continue : un YAML valide suivant recharge normalement.
    write_yaml(path, token="token-b")
    await store.poll()
    assert store.get().device_token == "token-b"
    assert "config reloaded" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("host", "port"),
    [("autre-hote", 1883), ("localhost", 1884)],
    ids=["host-modifie", "port-modifie"],
)
async def test_broker_change_rejected_restart_required(tmp_path, capsys, host, port):
    """broker.host/port modifiés : rejet explicite « redémarrage requis »,
    ancienne config conservée."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    initial = store.get()

    write_yaml(path, host=host, port=port)
    await store.poll()

    assert store.get() is initial
    assert not store.changed.is_set()
    out = capsys.readouterr().out
    assert out.count("config reload rejected") == 1
    assert "redémarrage requis" in out
    # Le rejet dit explicitement le sort des changements suivants.
    assert "ignorés tant que host/port ne sont pas rétablis" in out


async def test_broker_change_recovers_when_reverted(tmp_path, capsys):
    """Rétablissement de host/port (avec rotation du token dans la même
    écriture) : le rechargement reprend — nouveau token actif."""
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)

    write_yaml(path, host="autre-hote", token="token-ignore")
    await store.poll()
    assert store.get().device_token == "token-a"  # rejet, rien ne bouge
    assert "redémarrage requis" in capsys.readouterr().out

    write_yaml(path, token="token-b")  # host rétabli + token tourné
    await store.poll()

    assert store.get().device_token == "token-b"
    out = capsys.readouterr().out
    assert "config reloaded" in out
    assert "redémarrage requis" not in out


async def test_store_without_file_never_reloads():
    """path=None (configuration en mémoire, tests) : poll et watch sont des
    no-op, get() reste le snapshot initial."""
    config = Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="t",
        rules=(Rule(name="r", topic="home/x", title="T", short_message="M"),),
    )
    store = ConfigStore(config)
    stop = asyncio.Event()
    stop.set()

    await store.poll()
    await store.watch(stop)  # retourne immédiatement

    assert store.get() is config


# --- watch : tâche asyncio de sondage mtime ---


async def test_watch_reloads_until_stop(tmp_path, monkeypatch, capsys):
    """Le watcher sonde en tâche asyncio : rechargement observé sans action
    extérieure, retour propre quand stop est déclenché."""
    monkeypatch.setattr(config_module, "POLL_INTERVAL_S", 0.01)
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    stop = asyncio.Event()

    watcher = asyncio.create_task(store.watch(stop))
    write_yaml(path, token="token-b")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and store.get().device_token != "token-b":
        await asyncio.sleep(0.01)
    assert store.get().device_token == "token-b"
    assert "config reloaded" in capsys.readouterr().out

    stop.set()
    start = time.monotonic()
    await asyncio.wait_for(watcher, timeout=5)
    assert time.monotonic() - start < 1  # bien avant tout intervalle de sondage restant


async def test_watch_returns_promptly_on_stop(tmp_path, monkeypatch):
    """Stop pendant l'attente de sondage : retour immédiat, pas de hang."""
    monkeypatch.setattr(config_module, "POLL_INTERVAL_S", 30.0)
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    stop = asyncio.Event()

    watcher = asyncio.create_task(store.watch(stop))
    await asyncio.sleep(0.01)
    stop.set()
    start = time.monotonic()
    await asyncio.wait_for(watcher, timeout=2)
    assert time.monotonic() - start < 1


# --- Rotation du device token : lue au moment de l'envoi (AC-2.1.7) ---


async def test_token_rotation_applies_at_next_send(tmp_path, fcm_mock):
    """AC-2.1.7 : token modifié -> le prochain envoi utilise le nouveau token,
    sans redémarrage ; l'envoi déjà parti reste figé sur l'ancien."""
    calls, _threads = fcm_mock
    path = write_yaml(tmp_path / "config.yaml", token="token-v1")
    store = file_store(path)
    notifier = Notifier(store)
    event = Event(
        topic="home/x",
        payload={"valeur": "1"},
        rule=Rule(name="r", topic="home/x", title="T", short_message="M"),
    )

    await notifier.notify(event)
    assert calls[0].token == "token-v1"

    write_yaml(path, token="token-v2")
    await store.poll()

    await notifier.notify(event)
    assert calls[1].token == "token-v2"
    assert calls[0].token == "token-v1"  # l'ancien envoi n'est pas rétro-touché


# --- Resynchronisation des souscriptions au swap, sans déconnexion (AC-2.1.4) ---


async def test_resync_subscriptions_without_disconnection(fake_broker, tmp_path, capsys):
    """AC-2.1.3 / AC-2.1.4 : règles ajoutée et retirée -> nouveaux topics
    abonnés, disparus désabonnés, sur la même connexion (jamais de
    déconnexion) ; un message sur un topic retiré ne déclenche plus rien."""
    episodes, clients = fake_broker
    path = write_yaml(
        tmp_path / "config.yaml",
        rules=(
            ("cave", "home/cave/temp", "Cave", "Temp: {valeur}"),
            ("salon", "home/salon/capteur", "Salon", "Valeur: {valeur}"),
        ),
    )
    store = file_store(path)
    episode = Episode(messages=[FakeMessage(b'{"valeur": "1"}', topic="home/cave/temp")])
    episodes.append(episode)
    stop = asyncio.Event()

    it = iter_messages(store, stop)
    first = await asyncio.wait_for(anext(it), timeout=5)
    assert str(first.topic) == "home/cave/temp"  # encore abonné avant le swap
    assert sorted(clients[0].subscribed) == [
        ("home/cave/temp", 0),
        ("home/salon/capteur", 0),
    ]

    # Swap : retire 'cave', ajoute 'garage' — pendant la connexion établie.
    write_yaml(
        path,
        rules=(
            ("salon", "home/salon/capteur", "Salon", "Valeur: {valeur}"),
            ("garage", "home/garage/#", "Garage", "Portail: {action}"),
        ),
    )
    await store.poll()
    assert store.changed.is_set()

    # Les deux messages suivants : le topic retiré ne doit plus rien livrer,
    # le nouveau topic doit livrer via la souscription ajoutée.
    episode.messages.append(FakeMessage(b'{"valeur": "2"}', topic="home/cave/temp"))
    episode.messages.append(FakeMessage(b'{"action": "x"}', topic="home/garage/porte"))
    second = await asyncio.wait_for(anext(it), timeout=5)

    assert str(second.topic) == "home/garage/porte"  # le topic retiré a été sauté
    assert clients[0].connected_count == 1  # jamais déconnecté, jamais reconnecté
    assert clients[0].unsubscribed == ["home/cave/temp"]
    assert sorted(clients[0].subscribed) == [
        ("home/garage/#", 0),
        ("home/salon/capteur", 0),
    ]
    out = capsys.readouterr().out
    assert "mqtt resync subscribed=home/garage/# unsubscribed=home/cave/temp qos=0" in out
    assert "mqtt disconnected" not in out
    assert "mqtt reconnected" not in out

    stop.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(it), timeout=5)


async def test_resync_without_topic_change_is_silent(fake_broker, tmp_path, capsys):
    """Rechargement sans changement de topics (libellés seulement) : aucun
    delta, aucune ligne resync, la connexion poursuit son service."""
    episodes, clients = fake_broker
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    episode = Episode(messages=[FakeMessage(b'{"valeur": "1"}')])
    episodes.append(episode)
    stop = asyncio.Event()

    it = iter_messages(store, stop)
    await asyncio.wait_for(anext(it), timeout=5)

    write_yaml(path, rules=(("salon", "home/salon/capteur", "Capteur salon v2", "V2: {valeur}"),))
    await store.poll()
    episode.messages.append(FakeMessage(b'{"valeur": "2"}'))
    message = await asyncio.wait_for(anext(it), timeout=5)  # reprise après resync vide

    assert str(message.topic) == "home/salon/capteur"
    assert clients[0].subscribed == [("home/salon/capteur", 0)]
    out = capsys.readouterr().out
    assert "mqtt resync" not in out
    assert "config reloaded" in out

    stop.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(it), timeout=5)


# --- Prise d'effet à l'événement suivant, événement figé (AC-2.1.1) ---


async def test_next_event_uses_reloaded_label(fake_broker, tmp_path):
    """AC-2.1.1 : libellé modifié -> l'événement suivant utilise le nouveau
    libellé ; l'événement en traversée reste figé sur l'ancienne règle."""
    episodes, _clients = fake_broker
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    episodes.append(
        Episode(
            messages=[
                FakeMessage(b'{"valeur": "1"}', topic="home/salon/capteur"),
                FakeMessage(b'{"valeur": "2"}', topic="home/salon/capteur"),
            ],
            drop=aiomqtt.MqttError("fin d'épisode"),
        )
    )
    stop = asyncio.Event()

    it = iter_messages(store, stop)
    first = await asyncio.wait_for(anext(it), timeout=5)
    event1 = build_event(store.get().rules, str(first.topic), message_payload(first))

    write_yaml(
        path,
        rules=(("salon", "home/salon/capteur", "Capteur salon v2", "Valeur: {valeur} (v2)"),),
    )
    await store.poll()

    second = await asyncio.wait_for(anext(it), timeout=5)
    event2 = build_event(store.get().rules, str(second.topic), message_payload(second))

    assert render_message(event1.rule, event1.payload)["title"] == "Capteur salon"
    assert render_message(event2.rule, event2.payload)["title"] == "Capteur salon v2"
    # La règle est figée dans l'événement en traversée : jamais relue.
    assert event1.rule.title == "Capteur salon"
    assert event2.rule is not event1.rule

    stop.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(it), timeout=5)


# --- Critère d'acceptation utilisateur, bout en bout via run_pipeline ---


async def test_pipeline_end_to_end_reloaded_label_and_removed_rule_ignored(
    fake_broker, tmp_path, capsys
):
    """AC-2.1.1 (critère d'acceptation de la story, bout en bout) : un
    événement part avec l'ancien libellé, le YAML est rechargé pendant la
    connexion, l'événement suivant notifie avec le nouveau libellé et le
    topic de la règle retirée ne déclenche plus rien."""
    episodes, clients = fake_broker
    path = write_yaml(
        tmp_path / "config.yaml",
        rules=(
            ("salon", "home/salon/capteur", "Capteur salon", "Valeur: {valeur}"),
            ("cave", "home/cave/temp", "Cave", "Temp: {valeur}"),
        ),
    )
    store = file_store(path)
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(
        Episode(
            messages=[FakeMessage(b'{"valeur": "1"}', topic="home/salon/capteur")],
            queue=queue,
        )
    )
    notifier = CountingNotifier()
    stop = asyncio.Event()
    pipeline = asyncio.create_task(run_pipeline(store, notifier, stop))

    # Premier événement : ancien libellé, règle cave encore active.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and notifier.calls < 1:
        await asyncio.sleep(0.01)
    assert notifier.calls == 1
    first = notifier.events[0]
    assert first.topic == "home/salon/capteur"
    assert render_message(first.rule, first.payload)["title"] == "Capteur salon"

    # Rechargement pendant la connexion : nouveau libellé, règle cave retirée.
    write_yaml(
        path,
        rules=(("salon", "home/salon/capteur", "Capteur salon v2", "Valeur: {valeur} (v2)"),),
    )
    await store.poll()

    # Événements suivants : le topic retiré est ignoré (désabonné, aucun
    # message livré), le topic conservé notifie avec le nouveau libellé.
    queue.put_nowait(FakeMessage(b'{"valeur": "2"}', topic="home/cave/temp"))
    queue.put_nowait(FakeMessage(b'{"valeur": "3"}', topic="home/salon/capteur"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and notifier.calls < 2:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)

    assert notifier.calls == 2  # le message sur le topic retiré n'a rien déclenché
    second = notifier.events[1]
    assert second.topic == "home/salon/capteur"
    assert render_message(second.rule, second.payload)["title"] == "Capteur salon v2"
    out = capsys.readouterr().out
    assert "mqtt message topic=home/cave/temp" not in out
    assert clients[0].connected_count == 1  # jamais déconnecté
    assert clients[0].unsubscribed == ["home/cave/temp"]
    assert "mqtt resync subscribed=- unsubscribed=home/cave/temp qos=0" in out


async def test_reload_during_backoff_next_connection_subscribes_new_set(
    fake_broker, tmp_path, monkeypatch, capsys
):
    """Swap pendant le backoff (source entre deux épisodes, déconnectée) :
    la prochaine connexion souscrit le NOUVEL ensemble, et le drapeau
    `changed` périmé est consommé sans resync parasite à la connexion."""
    episodes, clients = fake_broker
    monkeypatch.setattr(mqtt_module, "RECONNECT_BACKOFF_INITIAL", 0.05)
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    notifier = CountingNotifier()
    stop = asyncio.Event()
    pipeline = asyncio.create_task(run_pipeline(store, notifier, stop))

    # Broker injoignable (aucun épisode) : la source entre en backoff.
    deadline = time.monotonic() + 5
    seen = ""
    while time.monotonic() < deadline and "mqtt connect failed" not in seen:
        await asyncio.sleep(0.01)
        seen += capsys.readouterr().out
    assert "mqtt connect failed" in seen

    # Swap pendant le backoff : la règle salon est remplacée par garage.
    write_yaml(
        path,
        rules=(("garage", "home/garage/#", "Garage", "Portail: {action}"),),
    )
    await store.poll()
    assert store.changed.is_set()

    # Le broker revient : la nouvelle connexion souscrit le nouvel ensemble,
    # le drapeau périmé est consommé sans resync parasite.
    episodes.append(
        Episode(
            messages=[FakeMessage(b'{"action": "x"}', topic="home/garage/porte")],
            drop=aiomqtt.MqttError("fin d'épisode"),
        )
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and notifier.calls < 1:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)

    assert notifier.calls == 1
    assert notifier.events[0].topic == "home/garage/porte"
    connected = [client for client in clients if client.connected_count == 1]
    assert len(connected) == 1
    assert connected[0].subscribed == [("home/garage/#", 0)]  # nouvel ensemble
    assert connected[0].unsubscribed == []
    assert not store.changed.is_set()  # consommé
    out = seen + capsys.readouterr().out
    assert "mqtt resync" not in out


# --- Assemblage : le watcher vit dans serve, arrêt propre au signal ---


async def test_serve_reloads_at_runtime_and_stops_cleanly(
    fake_broker, tmp_path, monkeypatch, capsys
):
    """Le watcher est créé à l'assemblage : rechargement à chaud pendant le
    service, arrêt propre du watcher et du pipeline au SIGTERM."""
    episodes, _clients = fake_broker
    monkeypatch.setattr(config_module, "POLL_INTERVAL_S", 0.01)
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    episodes.append(Episode())  # connexion silencieuse
    write_yaml(path, token="token-b")

    loop = asyncio.get_running_loop()
    loop.call_later(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))

    await asyncio.wait_for(serve(store, SilentNotifier()), timeout=5)

    out = capsys.readouterr().out
    assert "config reloaded" in out
    assert "stopped cleanly" in out


async def test_serve_logs_dead_watcher(fake_broker, tmp_path, monkeypatch, capsys):
    """Un watcher qui meurt en service (bug interne, pas un mauvais fichier)
    laisse une trace explicite : le rechargement à chaud est signalé
    inopérant, le service poursuit et s'arrête proprement."""
    episodes, _clients = fake_broker
    path = write_yaml(tmp_path / "config.yaml")
    store = file_store(path)
    episodes.append(Episode())  # connexion silencieuse

    async def broken_watch(stop):
        raise ValueError("bug du watcher")

    monkeypatch.setattr(store, "watch", broken_watch)
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))

    await asyncio.wait_for(serve(store, SilentNotifier()), timeout=5)

    out = capsys.readouterr().out
    assert "config watcher error" in out
    assert "bug du watcher" in out
    assert "stopped cleanly" in out


# --- Lecteur unique du YAML (AC-2.1.6), contrôlable par grep ---


def test_config_store_is_sole_yaml_reader():
    """AC-2.1.6 : seul config.py (le ConfigStore) lit le fichier YAML —
    échoue si un autre module du paquet importe/utilise yaml, ouvre un
    fichier (open/read_text) ou charge la config hors du store. Les motifs
    visent les usages réels (import, appel), pas le mot « yaml » en
    commentaire."""
    package_dir = Path(mqtt_notif.__file__).parent
    for path in sorted(package_dir.glob("*.py")):
        if path.name == "config.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"import\s+yaml|from\s+yaml\b|\byaml\.", source), (
            f"{path.name} référence yaml"
        )
        assert not re.search(r"\bload_config\b", source), (
            f"{path.name} charge la config hors store"
        )
        assert not re.search(r"\bopen\(|\bread_text\b", source), (
            f"{path.name} lit un fichier"
        )
