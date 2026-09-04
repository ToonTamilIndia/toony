"""Falling back between models, keeping them warm, and not losing the first word.

The three things this covers are the three that are invisible when they break.
A failover that silently does not happen looks like "the assistant is down". A
model that is not resident looks like "the assistant is slow". A pre-roll
buffer that is not used looks like "it never hears the start of my sentence".
"""

from __future__ import annotations

import array
import math
import threading
import time
import unittest
from datetime import datetime

from toony import automation, net
from toony.audio import hotkey
from toony.audio.capture import Microphone, MicrophonePool
from toony.brain import discovery, router
from toony.brain.base import (Brain, BrainError, BrainReply, InvalidRequest,
                              Message, ToolSpec)
from toony.config import Config


def tone(frames: int, amplitude: float = 0.3, rate: int = 16000) -> bytes:
    samples = array.array("h")
    for index in range(frames):
        samples.append(int(amplitude * 32767
                           * math.sin(2 * math.pi * 220 * index / rate)))
    return samples.tobytes()


def silence(frames: int) -> bytes:
    return b"\x00\x00" * frames


class FakeBrain(Brain):
    """A backend that answers, or fails in a way you choose."""

    def __init__(self, name: str, answer: str = "", error: Exception | None = None,
                 emit_then_fail: bool = False):
        self.name = name
        self.answer = answer or f"hello from {name}"
        self.error = error
        self.emit_then_fail = emit_then_fail
        self.calls = 0

    def reply(self, system, messages, tools):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return BrainReply(text=self.answer)

    def stream_reply(self, system, messages, tools, on_text=None):
        self.calls += 1
        if self.emit_then_fail:
            if on_text:
                on_text("half a sen")
            raise self.error or BrainError("could not reach the model")
        if self.error is not None:
            raise self.error
        if on_text:
            on_text(self.answer)
        return BrainReply(text=self.answer)


def route(name: str, brain: Brain, local: bool = False) -> router.Route:
    return router.Route(provider=name, model="m", local=local,
                        build=lambda b=brain: b)


class TestFailureClassification(unittest.TestCase):
    def test_connection_problems_are_worth_another_backend(self):
        for message in ["could not reach the model at http://localhost:11434",
                        "Request timed out",
                        "rate limit exceeded, please try again",
                        "The model endpoint returned an error: 503",
                        "model 'qwen2.5:7b' not found, try pulling it"]:
            with self.subTest(message=message):
                self.assertTrue(router.is_transport_failure(BrainError(message)))

    def test_a_rejected_transcript_is_not(self):
        # Every backend would reject the same transcript, so trying another one
        # is a slower way to fail.
        self.assertFalse(router.is_transport_failure(
            InvalidRequest("invalid message content type: <nil>")))

    def test_an_ordinary_failure_is_not(self):
        self.assertFalse(router.is_transport_failure(
            BrainError("the model produced no output")))

    def test_the_cause_chain_is_searched(self):
        class APIConnectionError(Exception):
            pass

        outer = BrainError("something went wrong")
        outer.__cause__ = APIConnectionError("no route")
        self.assertTrue(router.is_transport_failure(outer))


class TestRouting(unittest.TestCase):
    def setUp(self):
        # The router asks the network before skipping remote routes; pin it so
        # the tests do not depend on whether this machine has a connection.
        self._saved = net.NETWORK
        net.NETWORK = net.Connectivity()
        net.NETWORK.note_success()
        router.NETWORK = net.NETWORK
        self.addCleanup(self._restore)

    def _restore(self):
        net.NETWORK = self._saved
        router.NETWORK = self._saved

    def test_the_first_route_answers_when_it_can(self):
        cloud, local = FakeBrain("cloud"), FakeBrain("local")
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", local, local=True)])
        self.assertEqual(brain.reply("", [], []).text, "hello from cloud")
        self.assertEqual(local.calls, 0)

    def test_it_falls_back_when_the_first_cannot_be_reached(self):
        cloud = FakeBrain("cloud", error=BrainError("could not reach the model"))
        local = FakeBrain("local")
        announced = []
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", local, local=True)],
                                    announce=announced.append)
        self.assertEqual(brain.reply("", [], []).text, "hello from local")
        self.assertEqual(len(announced), 1)
        self.assertIn("ollama:m", announced[0])

    def test_a_failed_route_rests_rather_than_being_retried_every_turn(self):
        cloud = FakeBrain("cloud", error=BrainError("could not reach the model"))
        local = FakeBrain("local")
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", local, local=True)],
                                    cooldown=60)
        for _ in range(3):
            brain.reply("", [], [])
        # Tried once, then left alone: a dead endpoint should not cost a
        # timeout on every single question.
        self.assertEqual(cloud.calls, 1)
        self.assertEqual(local.calls, 3)

    def test_it_goes_back_to_the_good_one_when_the_cooldown_expires(self):
        cloud = FakeBrain("cloud", error=BrainError("could not reach the model"))
        local = FakeBrain("local")
        first, second = route("cloud", cloud), route("ollama", local, local=True)
        brain = router.RoutingBrain([first, second], cooldown=60)
        brain.reply("", [], [])
        cloud.error = None
        first.down_until = 0.0          # as if the cooldown had passed
        self.assertEqual(brain.reply("", [], []).text, "hello from cloud")

    def test_a_rejected_transcript_is_raised_not_routed_around(self):
        cloud = FakeBrain("cloud", error=InvalidRequest("bad transcript"))
        local = FakeBrain("local")
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", local, local=True)])
        with self.assertRaises(InvalidRequest):
            brain.reply("", [], [])
        # The agent knows how to recover from this; a second model does not.
        self.assertEqual(local.calls, 0)

    def test_a_stream_that_has_already_spoken_is_not_retried_elsewhere(self):
        cloud = FakeBrain("cloud", emit_then_fail=True)
        local = FakeBrain("local")
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", local, local=True)])
        heard: list[str] = []
        with self.assertRaises(BrainError):
            brain.stream_reply("", [], [], heard.append)
        # Otherwise the user hears the first half of one answer followed by the
        # whole of a different one.
        self.assertEqual(heard, ["half a sen"])
        self.assertEqual(local.calls, 0)

    def test_a_stream_that_has_not_spoken_yet_falls_back_cleanly(self):
        cloud = FakeBrain("cloud", error=BrainError("could not reach the model"))
        local = FakeBrain("local")
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", local, local=True)])
        heard: list[str] = []
        result = brain.stream_reply("", [], [], heard.append)
        self.assertEqual(result.text, "hello from local")
        self.assertEqual(heard, ["hello from local"])

    def test_offline_skips_the_cloud_entirely(self):
        net.NETWORK._probe = lambda: False
        net.NETWORK.online(force=True)
        cloud = FakeBrain("cloud")
        local = FakeBrain("local")
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", local, local=True)])
        self.assertEqual(brain.reply("", [], []).text, "hello from local")
        # No point spending a connection timeout to discover what we know.
        self.assertEqual(cloud.calls, 0)

    def test_offline_with_no_local_model_says_so_usefully(self):
        net.NETWORK._probe = lambda: False
        net.NETWORK.online(force=True)
        cloud = FakeBrain("cloud", error=BrainError("could not reach the model"))
        brain = router.RoutingBrain([route("cloud", cloud)])
        with self.assertRaises(BrainError) as caught:
            brain.reply("", [], [])
        self.assertIn("ollama pull", str(caught.exception))

    def test_status_names_what_is_actually_answering(self):
        cloud = FakeBrain("cloud", error=BrainError("could not reach the model"))
        brain = router.RoutingBrain([route("cloud", cloud),
                                     route("ollama", FakeBrain("l"), local=True)])
        brain.reply("", [], [])
        status = brain.status()
        self.assertEqual(status["current"], "ollama:m")
        self.assertEqual(status["primary"], "cloud:m")
        self.assertEqual(status["switches"], 1)


class TestRouteBuilding(unittest.TestCase):
    def test_fallback_off_gives_exactly_one_route(self):
        config = Config()
        config.set("brain.fallback", "off", save=False)
        config.set("brain.auto_model", False, save=False)
        routes = router.build_routes(config)
        self.assertEqual([r.provider for r in routes], ["ollama"])

    def test_a_provider_with_no_key_is_left_out_of_the_chain(self):
        # Otherwise every failover pauses on a guaranteed 401.
        config = Config()
        config.set("brain.auto_model", False, save=False)
        config.set("brain.claude.api_key", "", save=False)
        config.set("brain.claude.api_key_env", "TOONY_NO_SUCH_KEY", save=False)
        config.set("brain.openai.api_key", "", save=False)
        config.set("brain.openai.api_key_env", "TOONY_NO_SUCH_KEY", save=False)
        routes = router.build_routes(config)
        self.assertEqual([r.provider for r in routes], ["ollama"])

    def test_an_explicit_list_is_honoured_in_order(self):
        config = Config()
        config.set("brain.provider", "claude", save=False)
        config.set("brain.fallback", ["claude", "ollama"], save=False)
        config.set("brain.auto_model", False, save=False)
        config.set("brain.claude.api_key", "sk-test", save=False)
        routes = router.build_routes(config)
        self.assertEqual([r.provider for r in routes], ["claude", "ollama"])

    def test_the_local_endpoint_is_recognised_as_local(self):
        config = Config()
        config.set("brain.auto_model", False, save=False)
        routes = router.build_routes(config)
        self.assertTrue(routes[0].local)

    def test_a_missing_model_is_replaced_by_one_that_exists(self):
        config = Config()
        config.set("brain.ollama.model", "never-pulled:7b", save=False)
        original = discovery.ollama_models
        discovery.ollama_models = lambda *a, **k: ["qwen2.5:7b", "gemma2:2b"]
        try:
            self.assertEqual(router.resolve_model(config, "ollama"), "qwen2.5:7b")
        finally:
            discovery.ollama_models = original

    def test_a_model_that_is_installed_is_left_alone(self):
        config = Config()
        config.set("brain.ollama.model", "qwen2.5:7b", save=False)
        original = discovery.ollama_models
        discovery.ollama_models = lambda *a, **k: ["qwen2.5:7b", "llama3.1:8b"]
        try:
            self.assertEqual(router.resolve_model(config, "ollama"), "qwen2.5:7b")
        finally:
            discovery.ollama_models = original


class TestModelRanking(unittest.TestCase):
    def test_a_tool_calling_model_beats_one_that_cannot(self):
        # A model that will not emit a function call cannot drive the desktop,
        # however well it writes.
        self.assertEqual(discovery.best_local(["codellama:13b", "qwen2.5:7b"]),
                         "qwen2.5:7b")

    def test_models_too_large_for_a_laptop_are_not_chosen(self):
        self.assertEqual(discovery.best_local(["llama3.1:70b", "qwen2.5:7b"]),
                         "qwen2.5:7b")

    def test_a_bigger_model_of_the_same_family_wins(self):
        self.assertEqual(discovery.best_local(["qwen2.5:3b", "qwen2.5:14b"]),
                         "qwen2.5:14b")

    def test_it_still_answers_when_every_option_is_poor(self):
        self.assertEqual(discovery.best_local(["codellama:7b"]), "codellama:7b")

    def test_nothing_installed_gives_nothing(self):
        self.assertEqual(discovery.best_local([]), "")

    def test_the_vision_pick_only_considers_models_that_can_see(self):
        self.assertEqual(
            discovery.best_local_vision(["qwen2.5:7b", "llava:7b"]), "llava:7b")
        self.assertEqual(discovery.best_local_vision(["qwen2.5:7b"]), "")


class TestConnectivity(unittest.TestCase):
    def test_the_answer_is_cached(self):
        probes = []
        check = net.Connectivity(ttl=60)
        check._probe = lambda: probes.append(1) or True
        self.assertTrue(check.online())
        self.assertTrue(check.online())
        self.assertEqual(len(probes), 1)

    def test_a_failed_call_expires_the_cache(self):
        # The reading was taken before the cable was pulled.
        probes = []
        check = net.Connectivity(ttl=60)
        check._probe = lambda: probes.append(1) or True
        check.online()
        check.note_failure()
        check.online()
        self.assertEqual(len(probes), 2)

    def test_changes_are_counted(self):
        answers = iter([True, False])
        check = net.Connectivity(ttl=0)
        check._probe = lambda: next(answers)
        check.online()
        check.online()
        self.assertEqual(check.changes, 1)

    def test_a_local_endpoint_is_recognised(self):
        for url in ["http://localhost:11434/v1", "http://127.0.0.1:8080",
                    "http://192.168.1.5:11434/v1"]:
            with self.subTest(url=url):
                self.assertTrue(net.is_local(url))
        self.assertFalse(net.is_local("https://api.openai.com/v1"))


class TestPreroll(unittest.TestCase):
    """The fix for "it never hears the first word of my sentence"."""

    def _microphone(self, **overrides):
        config = Config()
        config.set("audio.silence_ms", 100, save=False)
        config.set("audio.min_utterance_ms", 40, save=False)
        for key, value in overrides.items():
            config.set(key, value, save=False)
        return Microphone(config)

    def _feed(self, mic, frames):
        def push():
            time.sleep(0.05)
            for frame in frames:
                mic._queue.put(frame)

        threading.Thread(target=push, daemon=True).start()

    def test_speech_from_before_the_key_press_is_kept(self):
        mic = self._microphone()
        size = mic.frame_samples
        # What the user said while the key was still travelling.
        for _ in range(6):
            mic._preroll.append(tone(size))
        self._feed(mic, [tone(size)] * 6 + [silence(size)] * 6)
        pcm = mic.record_utterance()
        self.assertIsNotNone(pcm)
        self.assertGreaterEqual(len(pcm) // (size * 2), 12)

    def test_the_pre_roll_can_be_refused(self):
        # After barge-in it holds Toony's own reply, not the user's voice.
        mic = self._microphone()
        size = mic.frame_samples
        for _ in range(6):
            mic._preroll.append(tone(size))
        self._feed(mic, [tone(size)] * 6 + [silence(size)] * 6)
        pcm = mic.record_utterance(use_preroll=False)
        self.assertIsNotNone(pcm)
        self.assertLess(len(pcm) // (size * 2), 12)

    def test_a_buffer_of_room_tone_is_not_prepended(self):
        # Otherwise every utterance starts with a second of silence, which the
        # decoder is happy to hallucinate words out of.
        mic = self._microphone()
        size = mic.frame_samples
        for _ in range(20):
            mic._preroll.append(silence(size))
        self.assertEqual(mic._trim_lead(list(mic._preroll)), [])

    def test_the_trim_keeps_a_little_air_before_the_first_word(self):
        mic = self._microphone()
        size = mic.frame_samples
        lead = [silence(size)] * 5 + [tone(size)] * 3
        kept = mic._trim_lead(lead)
        self.assertEqual(len(kept), 5)     # two frames of silence plus the word

    def test_the_ring_buffer_does_not_grow(self):
        mic = self._microphone(**{"audio.preroll_ms": 300})
        size = mic.frame_samples
        for _ in range(500):
            mic._preroll.append(tone(size))
        self.assertLessEqual(len(mic._preroll), mic._preroll_frames)

    def test_pre_roll_can_be_switched_off_entirely(self):
        mic = self._microphone(**{"audio.preroll_ms": 0})
        self.assertFalse(mic._keep_preroll)
        self.assertEqual(mic.preroll(), [])


class TestMicrophonePool(unittest.TestCase):
    def test_it_reuses_one_microphone(self):
        pool = MicrophonePool(Config())
        pool._mic = Microphone(Config())
        pool._mic.open = lambda: pool._mic
        self.assertIs(pool.acquire(), pool.acquire())

    def test_release_keeps_the_stream_when_asked_to(self):
        config = Config()
        pool = MicrophonePool(config)
        closed = []
        pool._mic = Microphone(config)
        pool._mic.close = lambda: closed.append(1)
        pool.release()
        self.assertEqual(closed, [])
        config.set("audio.keep_stream_open", False, save=False)
        pool.release()
        self.assertEqual(closed, [1])

    def test_an_idle_stream_is_eventually_handed_back(self):
        config = Config()
        config.set("audio.stream_idle_s", 0.01, save=False)
        pool = MicrophonePool(config)
        mic = Microphone(config)
        mic._stream = object()          # pretend it is open
        mic.last_used = time.monotonic() - 5
        closed = []
        mic.close = lambda: closed.append(1) or setattr(mic, "_stream", None)
        pool._mic = mic
        self.assertTrue(pool.reap())
        self.assertEqual(closed, [1])

    def test_a_stream_in_use_is_left_alone(self):
        config = Config()
        config.set("audio.stream_idle_s", 600, save=False)
        pool = MicrophonePool(config)
        mic = Microphone(config)
        mic._stream = object()
        mic.last_used = time.monotonic()
        pool._mic = mic
        self.assertFalse(pool.reap())


class TestHotkeyParsing(unittest.TestCase):
    def test_a_modifier_and_a_key(self):
        modifiers, triggers = hotkey.parse("Meta+Space")
        self.assertEqual(triggers, (hotkey.KEYS["space"],))
        # Either Meta key will do.
        self.assertEqual(modifiers, frozenset({125, 126}))

    def test_a_bare_modifier_is_a_valid_talk_key(self):
        # "hold right control" is what every voice chat uses.
        modifiers, triggers = hotkey.parse("rightctrl")
        self.assertEqual(modifiers, frozenset())
        self.assertEqual(triggers, (97,))

    def test_an_unknown_key_says_so(self):
        with self.assertRaises(hotkey.HotkeyUnavailable):
            hotkey.parse("Meta+Nonsense")
        with self.assertRaises(hotkey.HotkeyUnavailable):
            hotkey.parse("")

    def test_the_combination_only_fires_with_its_modifier_held(self):
        fired = []
        listener = hotkey.HotkeyListener("Meta+Space", lambda: fired.append("down"),
                                         lambda: fired.append("up"))
        listener._key(hotkey.KEYS["space"], hotkey.PRESS)
        self.assertEqual(fired, [])                 # no Meta held
        listener._key(125, hotkey.PRESS)
        listener._key(hotkey.KEYS["space"], hotkey.PRESS)
        listener._key(hotkey.KEYS["space"], hotkey.RELEASE)
        self.assertEqual(fired, ["down", "up"])

    def test_holding_the_key_does_not_fire_again(self):
        fired = []
        listener = hotkey.HotkeyListener("rightctrl", lambda: fired.append("down"),
                                         lambda: fired.append("up"))
        listener._key(97, hotkey.PRESS)
        listener._key(97, hotkey.REPEAT)
        listener._key(97, hotkey.REPEAT)
        listener._key(97, hotkey.RELEASE)
        self.assertEqual(fired, ["down", "up"])

    def test_the_capability_bitmap_is_read_least_significant_word_last(self):
        # The kernel prints the bitmap high word first, which is the opposite
        # of how it reads.
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "key"
        path.write_text("1 8000000000000000")     # bit 63, and bit 64
        bits = hotkey._capability_bits(str(path))
        self.assertEqual(bits, {63, 64})


class TestRoutines(unittest.TestCase):
    def test_triggers_are_read_the_way_they_are_written(self):
        self.assertEqual(automation.parse_trigger("every 30m").seconds, 1800)
        self.assertEqual(automation.parse_trigger("every 2h").seconds, 7200)
        daily = automation.parse_trigger("at 08:30")
        self.assertEqual((daily.hour, daily.minute), (8, 30))
        self.assertEqual(automation.parse_trigger("on startup").event, "startup")

    def test_nonsense_is_explained_rather_than_ignored(self):
        for text in ["", "sometimes", "at 99:99", "on the_moon", "every 5s"]:
            with self.subTest(text=text):
                with self.assertRaises(automation.BadRoutine):
                    automation.parse_trigger(text)

    def test_a_routine_round_trips_through_the_config_file(self):
        raw = {"name": "morning", "when": "at 07:15", "prompt": "what broke?",
               "speak": False, "enabled": True, "days": ["mon", "fri"]}
        routine = automation.Routine.from_dict(raw)
        self.assertEqual(automation.Routine.from_dict(routine.to_dict()).to_dict(),
                         routine.to_dict())

    def test_a_routine_with_nothing_to_do_is_refused(self):
        with self.assertRaises(automation.BadRoutine):
            automation.Routine.from_dict({"name": "empty", "when": "every 5m"})

    def test_a_broken_routine_does_not_take_the_others_down(self):
        config = Config()
        config.set("automation.routines", [
            {"name": "good", "when": "every 10m", "prompt": "hello"},
            {"name": "broken", "when": "whenever", "prompt": "hello"},
        ], save=False)
        self.assertEqual([r.name for r in automation.load(config)], ["good"])

    def test_a_due_routine_runs(self):
        config = Config()
        config.set("automation.tick_s", 5, save=False)
        config.set("automation.routines",
                   [{"name": "tick", "when": "every 10m", "prompt": "hello"}],
                   save=False)
        ran = []
        scheduler = automation.Scheduler(config, lambda r: ran.append(r.name) or "")
        scheduler.reload()
        scheduler.routines[0].next_run = time.monotonic() - 1
        scheduler._running.set()
        scheduler._wake.set()
        thread = threading.Thread(target=scheduler._loop, daemon=True)
        thread.start()
        for _ in range(100):
            if ran:
                break
            time.sleep(0.02)
        scheduler.stop()
        self.assertEqual(ran, ["tick"])

    def test_an_event_routine_waits_for_its_event(self):
        config = Config()
        config.set("automation.routines", [
            {"name": "on-net", "when": "on network_up", "prompt": "hi"},
            {"name": "hourly", "when": "every 1h", "prompt": "hi"},
        ], save=False)
        ran = []
        scheduler = automation.Scheduler(config, lambda r: ran.append(r.name) or "")
        scheduler.reload()
        self.assertEqual(scheduler.fire("battery_low"), 0)
        self.assertEqual(scheduler.fire("network_up"), 1)
        self.assertEqual(ran, ["on-net"])

    def test_quiet_hours_that_cross_midnight(self):
        config = Config()
        config.set("automation.quiet_hours", "22:30-07:30", save=False)
        scheduler = automation.Scheduler(config, lambda r: "")
        self.assertTrue(scheduler.in_quiet_hours(datetime(2026, 1, 1, 23, 0)))
        self.assertTrue(scheduler.in_quiet_hours(datetime(2026, 1, 1, 3, 0)))
        self.assertFalse(scheduler.in_quiet_hours(datetime(2026, 1, 1, 12, 0)))

    def test_no_quiet_hours_means_never_quiet(self):
        scheduler = automation.Scheduler(Config(), lambda r: "")
        self.assertFalse(scheduler.in_quiet_hours(datetime(2026, 1, 1, 3, 0)))

    def test_a_routine_only_runs_on_the_days_it_was_given(self):
        routine = automation.Routine.from_dict(
            {"name": "weekday", "when": "at 08:00", "prompt": "hi",
             "days": ["mon", "tue", "wed", "thu", "fri"]})
        self.assertTrue(routine.runs_today(datetime(2026, 1, 1)))    # Thursday
        self.assertFalse(routine.runs_today(datetime(2026, 1, 3)))   # Saturday

    def test_reloading_keeps_the_countdown_of_an_unchanged_routine(self):
        config = Config()
        config.set("automation.routines",
                   [{"name": "tick", "when": "every 1h", "prompt": "hi"}],
                   save=False)
        scheduler = automation.Scheduler(config, lambda r: "")
        scheduler.reload()
        first = scheduler.routines[0].next_run
        scheduler.reload()
        # Otherwise editing any setting would postpone every routine.
        self.assertEqual(scheduler.routines[0].next_run, first)


class TestWatcher(unittest.TestCase):
    def _watcher(self, fired):
        return automation.Watcher(Config(), fired.append)

    def test_the_first_reading_does_not_fire_anything(self):
        # Announcing "the network went down" at boot because the probe has not
        # run yet is a lie every single time.
        fired: list[str] = []
        watcher = self._watcher(fired)
        watcher.poll(announce=False)
        self.assertEqual(fired, [])

    def test_a_change_fires_once_not_repeatedly(self):
        fired: list[str] = []
        watcher = self._watcher(fired)
        watcher._online = True
        original = net.NETWORK.online
        net.NETWORK.online = lambda force=False: False
        try:
            watcher.poll()
            watcher.poll()
        finally:
            net.NETWORK.online = original
        self.assertEqual(fired, ["network_down"])

    def test_the_battery_warning_has_hysteresis(self):
        fired: list[str] = []
        watcher = self._watcher(fired)
        watcher._online = True
        levels = iter([15, 14, 13, 40, 15])
        original_battery = automation.battery_percent
        original_mains = automation.on_mains
        original_online = net.NETWORK.online
        automation.battery_percent = lambda: next(levels)
        automation.on_mains = lambda: False
        net.NETWORK.online = lambda force=False: True
        try:
            for _ in range(5):
                watcher.poll()
        finally:
            automation.battery_percent = original_battery
            automation.on_mains = original_mains
            net.NETWORK.online = original_online
        # Once on the way down, once again after it recovered — not every tick.
        self.assertEqual(fired, ["battery_low", "battery_low"])


class TestToolCaching(unittest.TestCase):
    def test_the_enabled_set_is_computed_once(self):
        from toony.tools import REGISTRY

        config = Config()
        REGISTRY.forget()
        first = REGISTRY.enabled(config)
        self.assertIs(REGISTRY.enabled(config), first)
        self.assertIs(REGISTRY.specs(config), REGISTRY.specs(config))

    def test_a_config_change_invalidates_it(self):
        from toony.tools import REGISTRY

        config = Config()
        before = len(REGISTRY.enabled(config))
        config.set("tools.disabled", ["get_datetime"], save=False)
        self.assertEqual(len(REGISTRY.enabled(config)), before - 1)

    def test_specs_are_the_shape_the_model_is_given(self):
        from toony.tools import REGISTRY

        specs = REGISTRY.specs(Config())
        self.assertTrue(specs)
        self.assertIsInstance(specs[0], ToolSpec)


if __name__ == "__main__":
    unittest.main()
