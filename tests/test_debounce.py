"""Story 2.2 : debounce par règle — fenêtre ancrée à la réception (jamais à
l'envoi), silence pendant la fenêtre avec message ignoré journalisé,
indépendance des règles, purge au swap de config, bornes [0, 3 600 000],
redémarrage = fenêtres réinitialisées."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import mqtt_notif
from mqtt_notif.config import Config, ConfigStore, load_config
from mqtt_notif.debounce import Debouncer
from mqtt_notif.main import run_pipeline
from mqtt_notif.rules import Rule

from .conftest import Episode, FakeMessage, write_config_yaml


def write_rules_yaml(path: Path, rules) -> Path:
    """Écrit un YAML de règles debounce ; rules = (name, topic, debounce_ms),
    via la copie unique de conftest (mtime bombé pour le sondage du store)."""
    return write_config_yaml(
        path,
        [
            {
                "name": name,
                "topic": topic,
                "title": "Titre",
                "short_message": "Message",
                "debounce_ms": debounce_ms,
            }
            for name, topic, debounce_ms in rules
        ],
    )


class FakeClock:
    """Horloge injectable : secondes monotones pilotées par le test."""

    def __init__(self, start: float = 0.0) -> None:
        self.seconds = start

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class CountingNotifier:
    """Notifier compteur : capture les événements notifiés (dans l'ordre)."""

    def __init__(self):
        self.events = []

    async def notify(self, event) -> None:
        self.events.append(event)

    @property
    def calls(self) -> int:
        return len(self.events)


class FailingNotifier:
    """Émetteur dont chaque envoi échoue (rendu ou FCM en erreur) : l'erreur
    est avalée comme dans le vrai Notifier, la fenêtre reste ouverte."""

    def __init__(self):
        self.attempts = 0

    async def notify(self, event) -> None:
        self.attempts += 1  # envoi simulé en échec : jamais retenté ici


class SlowSendingNotifier:
    """Émetteur lent : le premier envoi traîne jusqu'au signal du test
    (synchronisation par événement, jamais de budget d'horloge murale)."""

    def __init__(self):
        self.events = []
        self.release_first = asyncio.Event()
        self.first_finished = asyncio.Event()

    async def notify(self, event) -> None:
        self.events.append(event)
        if len(self.events) == 1:
            await self.release_first.wait()
            self.first_finished.set()

    @property
    def calls(self) -> int:
        return len(self.events)


class Output:
    """Accumule la sortie capturée : readouterr() vide le tampon à chaque appel."""

    def __init__(self, capsys) -> None:
        self._capsys = capsys
        self.seen = ""

    def text(self) -> str:
        self.seen += self._capsys.readouterr().out
        return self.seen


async def until(predicate, timeout: float = 5.0, step: float = 0.01) -> None:
    """Attend que predicate() soit vrai ; lève à l'expiration du délai pour
    échouer à l'endroit exact au lieu d'avaler l'événement manqué."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"attendu non observé après {timeout}s")


def debounce_rule(name: str = "bouton", topic: str = "home/bouton", debounce_ms: int = 1000) -> Rule:
    return Rule(name=name, topic=topic, title="Titre", short_message="Message", debounce_ms=debounce_ms)


def debounce_config(**rules_by_name) -> Config:
    """Config en mémoire : une règle debounce par nom -> topic 'home/<nom>'."""
    return Config(
        broker_host="localhost",
        broker_port=1883,
        device_token="token-test",
        rules=tuple(
            debounce_rule(name=name, topic=f"home/{name}", debounce_ms=debounce_ms)
            for name, debounce_ms in rules_by_name.items()
        ),
    )


def ignored_line(rule_name: str, debounce_ms: int, topic: str, remaining_ms: int) -> str:
    return (
        f"debug: message ignored topic={topic} rule={rule_name} "
        f"debounce_ms={debounce_ms} fenêtre restante={remaining_ms} ms"
    )


# --- Debouncer : fenêtre, expiration, ancrage, indépendance, purge ---


def test_first_message_notifies_and_opens_window():
    """Aucune fenêtre ouverte : le premier message notifie."""
    debouncer = Debouncer()
    assert debouncer.should_notify(debounce_rule(), 0.0) is True


def test_second_message_within_window_silenced():
    """AC-2.2.1 : message reçu pendant le délai de sa règle -> aucune notification."""
    debouncer = Debouncer()
    rule = debounce_rule(debounce_ms=1000)
    assert debouncer.should_notify(rule, 0.0) is True
    assert debouncer.should_notify(rule, 0.4) is False


def test_message_after_expiry_notifies_and_reopens_window():
    """AC-2.2.2 : après expiration, le message notifie et une nouvelle fenêtre s'ouvre."""
    debouncer = Debouncer()
    rule = debounce_rule(debounce_ms=1000)
    assert debouncer.should_notify(rule, 0.0) is True
    assert debouncer.should_notify(rule, 1.0) is True  # délai expiré (1000 ms)
    assert debouncer.should_notify(rule, 1.4) is False  # nouvelle fenêtre active


def test_ignored_message_does_not_reposition_window():
    """Un message ignoré ne prolonge pas la fenêtre : seule une notification l'ouvre."""
    debouncer = Debouncer()
    rule = debounce_rule(debounce_ms=1000)
    assert debouncer.should_notify(rule, 0.0) is True
    assert debouncer.should_notify(rule, 0.4) is False  # ignoré
    # Si la fenêtre était repositionnée sur l'ignoré (0.4), celle-ci courrait
    # jusqu'à 1.4 et ce message serait silencié : elle reste ancrée sur 0.
    assert debouncer.should_notify(rule, 1.2) is True


def test_without_delay_every_message_notifies():
    """AC-2.2.4 : debounce_ms absent ou 0 -> chaque message notifie, aucun état tenu."""
    debouncer = Debouncer()
    rule = debounce_rule(debounce_ms=0)
    assert all(debouncer.should_notify(rule, t) is True for t in (0.0, 0.001, 0.002))
    # Défensif : un négatif (rejeté au chargement) se comporte comme 0 ici.
    negative = debounce_rule(debounce_ms=-1)
    assert debouncer.should_notify(negative, 0.0) is True
    assert debouncer.should_notify(negative, 0.0) is True


def test_windows_independent_between_rules():
    """AC-2.2.5 : les fenêtres ne se partagent jamais, clé = nom unique de la règle."""
    debouncer = Debouncer()
    first = debounce_rule(name="a", topic="home/a", debounce_ms=1000)
    second = debounce_rule(name="b", topic="home/b", debounce_ms=1000)
    assert debouncer.should_notify(first, 0.0) is True
    # La fenêtre de 'first' ne silence pas 'second' (fenêtre propre, vierge).
    assert debouncer.should_notify(second, 0.4) is True
    # Chaque règle garde sa propre fenêtre.
    assert debouncer.should_notify(first, 0.6) is False
    assert debouncer.should_notify(second, 0.6) is False


def test_same_rule_name_shares_window():
    """Clé = nom : deux instances de règle de même nom partagent la fenêtre
    (une règle conservée au swap garde ainsi la sienne)."""
    debouncer = Debouncer()
    assert debouncer.should_notify(debounce_rule(name="a"), 0.0) is True
    assert debouncer.should_notify(debounce_rule(name="a"), 0.4) is False


def test_purge_removes_vanished_rules_keeps_surviving_ones():
    """AC-2.2.7 : purge au swap — fenêtre purgée pour la règle disparue,
    conservée pour l'autre (un message immédiat dessus reste ignoré)."""
    debouncer = Debouncer()
    vanished = debounce_rule(name="a", debounce_ms=60_000)
    kept = debounce_rule(name="b", debounce_ms=60_000)
    assert debouncer.should_notify(vanished, 0.0) is True
    assert debouncer.should_notify(kept, 0.0) is True
    debouncer.purge({"b"})
    assert debouncer.should_notify(kept, 0.5) is False  # conservée : fenêtre intacte
    assert debouncer.should_notify(vanished, 0.5) is True  # purgée : notifie


def test_purge_keeps_window_of_rule_redefined_under_same_name():
    """AD-7 « persistance de celles conservées » : la purge est par nom — une
    règle redéfinie sous le même nom au swap garde sa fenêtre, seul le
    nouveau debounce_ms s'applique ensuite."""
    debouncer = Debouncer()
    assert debouncer.should_notify(debounce_rule(name="a", debounce_ms=1000), 0.0) is True
    debouncer.purge({"a"})  # swap : même nom conservé, debounce_ms changé
    redefined = debounce_rule(name="a", debounce_ms=2000)
    assert debouncer.remaining_ms(redefined, 0.4) == 1600.0  # fenêtre de 0.0 conservée
    assert debouncer.should_notify(redefined, 0.4) is False


def test_injected_clock_used_when_received_at_absent():
    """Horloge injectable : sans received_at explicite, l'horodatage vient
    de l'horloge — tests déterministes sans sommeil réel."""
    clock = FakeClock()
    debouncer = Debouncer(clock=clock)
    rule = debounce_rule(debounce_ms=1000)
    assert debouncer.now() == 0.0
    assert debouncer.should_notify(rule) is True
    clock.advance(0.4)
    assert debouncer.should_notify(rule) is False
    clock.advance(0.7)
    assert debouncer.should_notify(rule) is True


# --- Pipeline : rafale silenciée, message ignoré journalisé, ancrage ---


async def test_pipeline_burst_silenced_then_notifies_after_expiry(fake_broker, capsys):
    """AC-2.2.1 / AC-2.2.2 (rafale bout en bout) : règle debounce_ms 1000 —
    un premier message notifié, un second 400 ms plus tard silencié et
    journalisé, un message après expiration notifie et rouvre la fenêtre."""
    episodes, _clients = fake_broker
    out = Output(capsys)
    clock = FakeClock()
    debouncer = Debouncer(clock=clock)
    notifier = CountingNotifier()
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    pipeline = asyncio.create_task(
        run_pipeline(ConfigStore(debounce_config(bouton=1000)), notifier, stop, debouncer)
    )

    queue.put_nowait(FakeMessage(b'{"n": "1"}', topic="home/bouton"))
    await until(lambda: notifier.calls == 1)

    clock.advance(0.4)  # 400 ms après la réception acceptée
    queue.put_nowait(FakeMessage(b'{"n": "2"}', topic="home/bouton"))
    await until(lambda: ignored_line("bouton", 1000, "home/bouton", 600) in out.text())
    assert notifier.calls == 1  # aucune notification pendant la fenêtre

    clock.advance(0.7)  # 1.1 s : fenêtre expirée
    queue.put_nowait(FakeMessage(b'{"n": "3"}', topic="home/bouton"))
    await until(lambda: notifier.calls == 2)  # passe le filtre, nouvelle fenêtre ouverte

    clock.advance(0.4)  # dans la nouvelle fenêtre (ancrée à 1.1 s)
    queue.put_nowait(FakeMessage(b'{"n": "4"}', topic="home/bouton"))
    await until(lambda: out.text().count("debug: message ignored") == 2)
    assert notifier.calls == 2

    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)
    assert [event.payload["n"] for event in notifier.events] == ["1", "3"]


async def test_pipeline_without_delay_notifies_every_message(fake_broker):
    """AC-2.2.4 (rafale bout en bout) : debounce_ms 0 explicite -> chaque
    message d'une rafale notifie (règle sans la clé : couverte par toutes
    les règles des tests pipeline existants)."""
    episodes, _clients = fake_broker
    clock = FakeClock()
    debouncer = Debouncer(clock=clock)
    notifier = CountingNotifier()
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    pipeline = asyncio.create_task(
        run_pipeline(ConfigStore(debounce_config(bouton=0)), notifier, stop, debouncer)
    )

    for index in range(3):
        clock.advance(0.001)  # rafale : reçus quasi simultanément
        queue.put_nowait(FakeMessage(b'{"n": "%d"}' % index, topic="home/bouton"))
    await until(lambda: notifier.calls == 3)

    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)
    assert notifier.calls == 3


async def test_pipeline_failed_send_does_not_release_window(fake_broker, capsys):
    """AD-7 « pas à l'envoi » (bout en bout) : la fenêtre s'ouvre à la décision
    du filtre, jamais au succès de l'envoi — un envoi en échec (rendu ou FCM
    en erreur) ne la libère pas : le message suivant dans le délai reste
    silencié et n'atteint jamais l'émetteur."""
    episodes, _clients = fake_broker
    out = Output(capsys)
    clock = FakeClock()
    debouncer = Debouncer(clock=clock)
    notifier = FailingNotifier()
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    pipeline = asyncio.create_task(
        run_pipeline(ConfigStore(debounce_config(bouton=1000)), notifier, stop, debouncer)
    )

    queue.put_nowait(FakeMessage(b'{"n": "1"}', topic="home/bouton"))
    await until(lambda: notifier.attempts == 1)  # passe le filtre, envoi échoue
    assert debouncer.remaining_ms(debounce_rule("bouton"), 0.0) == 1000.0  # fenêtre ouverte

    clock.advance(0.4)
    queue.put_nowait(FakeMessage(b'{"n": "2"}', topic="home/bouton"))
    await until(lambda: ignored_line("bouton", 1000, "home/bouton", 600) in out.text())
    assert notifier.attempts == 1  # silencié malgré l'échec du premier envoi

    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)


async def test_pipeline_slow_send_does_not_extend_window(fake_broker, capsys):
    """AC-2.2.3 (bout en bout) : envoi lent — le délai expire pendant l'envoi,
    la fenêtre reste ancrée sur la réception : le message suivant passe le
    filtre, puis la nouvelle fenêtre s'applique. Le premier envoi traîne
    jusqu'au signal du test (synchronisation par événement)."""
    episodes, _clients = fake_broker
    out = Output(capsys)
    clock = FakeClock()
    debouncer = Debouncer(clock=clock)
    notifier = SlowSendingNotifier()
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    pipeline = asyncio.create_task(
        run_pipeline(ConfigStore(debounce_config(bouton=1000)), notifier, stop, debouncer)
    )

    queue.put_nowait(FakeMessage(b'{"n": "1"}', topic="home/bouton"))
    await until(lambda: notifier.calls == 1)  # premier envoi parti (il traîne)

    clock.advance(0.4)
    queue.put_nowait(FakeMessage(b'{"n": "2"}', topic="home/bouton"))
    await until(lambda: ignored_line("bouton", 1000, "home/bouton", 600) in out.text())
    # Silencié pendant que le premier envoi est encore en vol : la fenêtre
    # est ancrée sur la réception, pas sur la fin d'envoi.
    assert not notifier.first_finished.is_set()
    assert notifier.calls == 1

    clock.advance(0.65)  # 1.05 s : délai expiré pendant l'envoi lent
    queue.put_nowait(FakeMessage(b'{"n": "3"}', topic="home/bouton"))
    await until(lambda: notifier.calls == 2)
    assert not notifier.first_finished.is_set()  # l'envoi lent traîne toujours
    # La fenêtre du message 3 est ancrée à sa réception (1.05 s), pas à la
    # fin de l'envoi lent : 1.3 s reste dedans.
    clock.advance(0.25)
    queue.put_nowait(FakeMessage(b'{"n": "4"}', topic="home/bouton"))
    await until(lambda: out.text().count("debug: message ignored") == 2)
    assert notifier.calls == 2

    notifier.release_first.set()  # l'envoi lent se termine avant l'arrêt
    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)
    assert notifier.first_finished.is_set()
    assert [event.payload["n"] for event in notifier.events] == ["1", "3"]


async def test_pipeline_rule_windows_independent(fake_broker, capsys):
    """AC-2.2.5 (bout en bout) : deux règles avec debounce — un message sur la
    deuxième pendant la fenêtre de la première notifie (fenêtre propre) ;
    chacune silencie ensuite ses propres messages."""
    episodes, _clients = fake_broker
    out = Output(capsys)
    clock = FakeClock()
    debouncer = Debouncer(clock=clock)
    notifier = CountingNotifier()
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    pipeline = asyncio.create_task(
        run_pipeline(ConfigStore(debounce_config(bouton=1000, sonnette=1000)), notifier, stop, debouncer)
    )

    def put(topic: str) -> None:
        queue.put_nowait(FakeMessage(b'{"n": "x"}', topic=topic))

    put("home/bouton")  # fenêtre bouton ouverte (réception 0)
    await until(lambda: notifier.calls == 1)
    clock.advance(0.4)
    put("home/sonnette")  # fenêtre sonnette vierge : notifie malgré celle de bouton
    await until(lambda: notifier.calls == 2)
    clock.advance(0.2)  # 0.6 s : dans les deux fenêtres
    put("home/sonnette")
    put("home/bouton")
    await until(lambda: out.text().count("debug: message ignored") == 2)
    assert notifier.calls == 2
    clock.advance(0.7)  # 1.3 s : fenêtre bouton expirée, sonnette (0.4 + 1.0) non
    put("home/bouton")
    await until(lambda: notifier.calls == 3)
    put("home/sonnette")
    await until(lambda: out.text().count("debug: message ignored") == 3)

    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)
    assert notifier.calls == 3
    assert [event.topic for event in notifier.events] == [
        "home/bouton",
        "home/sonnette",
        "home/bouton",
    ]


# --- Swap de config : purge via le store, rejet des bornes au rechargement ---


async def test_pipeline_purge_on_config_swap(fake_broker, tmp_path, capsys):
    """AC-2.2.7 (bout en bout, swap via le store) : fenêtre purgée pour la
    règle disparue, conservée pour l'autre (message immédiat dessus ignoré) ;
    la règle ré-apparue retrouve une fenêtre vierge."""
    episodes, _clients = fake_broker
    out = Output(capsys)
    path = write_rules_yaml(
        tmp_path / "config.yaml",
        (("bouton", "home/bouton", 60_000), ("sonnette", "home/sonnette", 60_000)),
    )
    store = ConfigStore(load_config(str(path)), str(path))
    clock = FakeClock()
    debouncer = Debouncer(clock=clock)
    notifier = CountingNotifier()
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    pipeline = asyncio.create_task(run_pipeline(store, notifier, stop, debouncer))

    def put(topic: str) -> None:
        queue.put_nowait(FakeMessage(b'{"n": "x"}', topic=topic))

    put("home/bouton")  # fenêtre bouton ouverte
    await until(lambda: notifier.calls == 1)
    put("home/sonnette")  # fenêtre sonnette ouverte
    await until(lambda: notifier.calls == 2)

    # Swap : bouton supprimée, sonnette conservée.
    write_rules_yaml(path, (("sonnette", "home/sonnette", 60_000),))
    await store.poll()
    await until(lambda: "mqtt resync" in out.text())  # purge faite au moment du changed

    put("home/sonnette")  # règle conservée : fenêtre intacte -> ignoré
    await until(lambda: out.text().count("debug: message ignored") == 1)
    assert notifier.calls == 2

    # Swap : bouton ré-apparait — sa fenêtre a été purgée au premier swap.
    write_rules_yaml(
        path,
        (("bouton", "home/bouton", 60_000), ("sonnette", "home/sonnette", 60_000)),
    )
    await store.poll()
    await until(lambda: out.text().count("mqtt resync") == 2)

    put("home/bouton")  # fenêtre purgée au premier swap -> notifie
    await until(lambda: notifier.calls == 3)

    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)
    assert [event.topic for event in notifier.events] == [
        "home/bouton",
        "home/sonnette",
        "home/bouton",
    ]


async def test_out_of_bounds_debounce_rejected_on_reload(tmp_path, capsys):
    """AC-2.2.6 au rechargement : debounce_ms hors bornes -> rejet explicite
    journalisé, anciennes règles conservées ; le YAML valide suivant recharge."""
    path = write_rules_yaml(tmp_path / "config.yaml", (("salon", "home/salon", 1000),))
    store = ConfigStore(load_config(str(path)), str(path))
    initial = store.get()

    write_rules_yaml(path, (("salon", "home/salon", -5),))
    await store.poll()

    assert store.get() is initial  # anciennes règles conservées
    out = capsys.readouterr().out
    assert "config reload rejected" in out
    assert "debounce_ms" in out
    assert "salon" in out

    write_rules_yaml(path, (("salon", "home/salon", 2000),))
    await store.poll()
    assert store.get().rules[0].debounce_ms == 2000
    assert "config reloaded" in capsys.readouterr().out


# --- Redémarrage : toutes les fenêtres réinitialisées (AC-2.2.8) ---


async def test_pipeline_default_clock_real_window(fake_broker, capsys):
    """Câblage de l'horloge par défaut (time.monotonic), que tous les autres
    tests court-circuitent par horloge injectée : fenêtre réelle de 20 ms —
    message immédiat silencié, message suivant notifié après expiration."""
    episodes, _clients = fake_broker
    out = Output(capsys)
    notifier = CountingNotifier()
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    pipeline = asyncio.create_task(
        run_pipeline(ConfigStore(debounce_config(bouton=20)), notifier, stop, Debouncer())
    )

    queue.put_nowait(FakeMessage(b'{"n": "1"}', topic="home/bouton"))
    await until(lambda: notifier.calls == 1, step=0.002)
    queue.put_nowait(FakeMessage(b'{"n": "2"}', topic="home/bouton"))
    await until(lambda: "debug: message ignored" in out.text(), step=0.002)
    assert notifier.calls == 1  # silencié : la fenêtre de 20 ms est ouverte

    await asyncio.sleep(0.05)  # > 20 ms réels : fenêtre expirée
    queue.put_nowait(FakeMessage(b'{"n": "3"}', topic="home/bouton"))
    await until(lambda: notifier.calls == 2)

    stop.set()
    await asyncio.wait_for(pipeline, timeout=5)
    assert [event.payload["n"] for event in notifier.events] == ["1", "3"]


async def test_pipeline_restart_resets_windows(fake_broker, capsys):
    """AC-2.2.8 : les fenêtres vivent en mémoire avec le pipeline — un nouveau
    pipeline (redémarrage) les réinitialise toutes : le premier message de
    la règle notifie malgré la fenêtre ouverte avant l'arrêt. Aucun état
    persisté, aucune fenêtre partagée entre les deux exécutions."""
    episodes, _clients = fake_broker
    out = Output(capsys)
    long_window = debounce_config(bouton=60_000)
    stop = asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    first_notifier = CountingNotifier()
    first = asyncio.create_task(run_pipeline(ConfigStore(long_window), first_notifier, stop))

    queue.put_nowait(FakeMessage(b'{"n": "1"}', topic="home/bouton"))
    await until(lambda: first_notifier.calls == 1)
    queue.put_nowait(FakeMessage(b'{"n": "2"}', topic="home/bouton"))
    await until(lambda: "debug: message ignored" in out.text())  # fenêtre ouverte (60 s)
    stop.set()
    await asyncio.wait_for(first, timeout=5)

    stop = asyncio.Event()
    queue = asyncio.Queue()
    episodes.append(Episode(queue=queue))
    second_notifier = CountingNotifier()
    second = asyncio.create_task(run_pipeline(ConfigStore(long_window), second_notifier, stop))

    queue.put_nowait(FakeMessage(b'{"n": "3"}', topic="home/bouton"))
    await until(lambda: second_notifier.calls == 1)  # fenêtres réinitialisées

    stop.set()
    await asyncio.wait_for(second, timeout=5)
    assert second_notifier.calls == 1


# --- AD-1 : filtre avant le rendu, isolation préservée ---


def test_debounce_gate_precedes_render_isolation_preserved():
    """AD-1 : le filtre s'exécute physiquement AVANT le rendu (le rendu vit
    dans notifier.notify, appelé seulement si la fenêtre l'autorise — un
    message silencié n'est jamais rendu). L'isolation est préservée : ni la
    sélection, ni le rendu, ni l'émetteur ne référencent le debounce."""
    package_dir = Path(mqtt_notif.__file__).parent
    for name in ("render.py", "notifier.py", "rules.py"):
        source = (package_dir / name).read_text(encoding="utf-8")
        assert "Debouncer" not in source, f"{name} référence le Debouncer"
        assert "should_notify" not in source, f"{name} référence la fenêtre de debounce"
