"""The Toony daemon.

One thread owns the microphone, one owns the control socket, and turns run
serially. States: idle -> listening -> thinking -> speaking -> idle.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid

from .agent import Agent
from .audio.capture import AudioUnavailable, Microphone
from .audio.playback import Player
from .brain import build_brain
from .brain.base import BrainError
from .config import Config
from .history import store_for
from .ipc import ControlServer
from .log import get
from .stt import STTError, build_stt
from .tts import TTSError, build_tts
from .tts.speaker import Speaker

log = get("app")

_YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "go", "ahead", "do", "please",
        "affirmative", "confirm", "alright", "fine", "yup"}
_NO = {"no", "nope", "don't", "dont", "stop", "cancel", "never", "nevermind",
       "negative", "nah"}


class Assistant:
    def __init__(self, config: Config):
        self.config = config
        self._state = "starting"
        self.started_at = time.monotonic()
        self.last_error = ""
        self.turns = 0

        self.player = Player(config)
        self.brain = build_brain(config)
        self.stt = build_stt(config)
        self.tts = build_tts(config)
        self.voice = Speaker.from_config(self.tts, self.player, config)
        self.store = store_for(config)
        self.agent = Agent(config, self.brain, speak=self.say,
                           confirm=self._confirm, store=self.store,
                           on_tool=self._on_tool)
        self.agent.resume()

        self.wakeword = self._build_wakeword(config)

        self._server = ControlServer(self._handle)
        self._running = threading.Event()
        self._turn_requested = threading.Event()
        self._stop_listening = threading.Event()
        self._turn_lock = threading.Lock()
        self._audio_thread: threading.Thread | None = None
        # Permission questions waiting on an answer from the GUI.
        self._pending: dict[str, queue.Queue] = {}

    # ---- state, published as it changes ----------------------------------
    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        changed = value != getattr(self, "_state", None)
        self._state = value
        if changed:
            self.publish("state", state=value)

    def publish(self, event: str, **data) -> None:
        """Tell every subscriber (the GUI) what just happened."""
        server = getattr(self, "_server", None)
        if server is None:
            return
        try:
            server.broadcast({"event": event, "at": time.time(), **data})
        except Exception:
            log.debug("could not publish %s", event, exc_info=True)

    def _on_tool(self, name: str, arguments: dict, output: str,
                 is_error: bool) -> None:
        self.publish("tool", tool=name, arguments=arguments,
                     result=output[:400], error=is_error)

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._running.set()
        self._server.start()
        self._warm()
        self._audio_thread = threading.Thread(target=self._audio_loop,
                                              name="toony-audio", daemon=True)
        self._audio_thread.start()
        self.state = "idle"
        log.info("%s is ready (brain=%s, stt=%s, tts=%s, wake word=%s)",
                 self.config.get("general.name", "Toony"),
                 self.config.get("brain.provider"), self.stt.name, self.tts.name,
                 "on" if self.wakeword else "off")

    def _build_wakeword(self, config):
        """The wake word listener owns its own microphone stream."""
        if not config.get("wakeword.enabled", False):
            return None
        from .audio.wakeword import WakeWordListener

        return WakeWordListener(config, on_wake=self._on_wake)

    def _on_wake(self, name: str) -> None:
        """Called from the wake word thread; it has already paused itself."""
        self._turn_requested.set()

    def _warm(self) -> None:
        """Load models now so the first question is not the slow one."""
        for component in (self.stt, self.tts):
            try:
                component.warm()
            except Exception as exc:
                log.warning("%s warm-up failed: %s", type(component).__name__, exc)
                self.last_error = str(exc)
        if self.wakeword is not None:
            try:
                self.wakeword.start()
            except Exception as exc:
                log.error("wake word unavailable: %s", exc)
                self.last_error = str(exc)
                self.wakeword = None

    def run_forever(self) -> None:
        try:
            while self._running.is_set():
                time.sleep(0.25)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop()

    def stop(self) -> None:
        self._running.clear()
        self.agent.persist()   # never lose the last thing that was said
        self.voice.stop()
        if self.wakeword is not None:
            self.wakeword.stop()
        self._server.stop()
        log.info("stopped after %d turns", self.turns)

    # ---- the microphone thread -------------------------------------------
    def _audio_loop(self) -> None:
        """Wait for a turn request — from the hotkey or from the wake word."""
        while self._running.is_set():
            try:
                if not self._turn_requested.wait(0.5):
                    continue
                self._turn_requested.clear()
                if self.wakeword is not None:
                    self.wakeword.pause()  # do not let Toony hear itself
                try:
                    with Microphone(self.config) as mic:
                        self._run_turn(mic)
                finally:
                    if self.wakeword is not None:
                        self.wakeword.resume()
            except AudioUnavailable as exc:
                self.last_error = str(exc)
                log.error("%s — retrying in 5s", exc)
                time.sleep(5)
            except Exception:
                log.exception("audio loop crashed — continuing")
                time.sleep(1)

    # ---- one conversational turn -----------------------------------------
    def _run_turn(self, mic: Microphone) -> None:
        if not self._turn_lock.acquire(blocking=False):
            return
        try:
            self.state = "listening"
            self._stop_listening.clear()
            if self.config.get("audio.start_chime", True):
                self.player.chime("start")

            pcm = mic.record_utterance(stop_event=self._stop_listening)
            if not pcm:
                log.info("nothing was said")
                return

            self.state = "thinking"
            try:
                transcript = self.stt.transcribe(pcm, mic.sample_rate)
            except STTError as exc:
                self.last_error = str(exc)
                log.error("transcription failed: %s", exc)
                self.say("I could not understand the audio.")
                return
            if not transcript.usable:
                log.info("transcript was empty or noise: %r", transcript.text)
                return

            log.info("heard: %s", transcript.text)
            self.turns += 1
            self.publish("heard", text=transcript.text,
                         confidence=getattr(transcript, "confidence", None),
                         conversation=self.agent.conversation.id)
            self._answer(transcript.text)
        finally:
            self.state = "idle"
            self._turn_lock.release()

    def _answer(self, text: str) -> None:
        """Run the agent, speaking each sentence as soon as it is complete."""
        if not self.config.get("tts.stream", True):
            reply = self._run_agent(text)
            if reply:
                self.say(reply)
            return

        stream = self.voice.open_stream()
        try:
            reply = self._run_agent(text, on_text=stream.feed)
        finally:
            self.state = "speaking"
            stream.close()
        # Backends that cannot stream return everything at the end instead.
        if reply and not stream.spoke:
            self.say(reply)

    def _run_agent(self, text: str, on_text=None) -> str:
        try:
            reply = self.agent.ask(text, on_text=on_text)
        except BrainError as exc:
            self.last_error = str(exc)
            log.error("brain failed: %s", exc)
            reply = str(exc)
        self.publish("reply", text=reply,
                     conversation=self.agent.conversation.id,
                     title=self.agent.conversation.display_title())
        return reply

    def say(self, text: str) -> None:
        """Speak a message. Blocks until it is finished or interrupted."""
        if not text.strip():
            return
        previous, self.state = self.state, "speaking"
        try:
            self.voice.say(text)
        except TTSError as exc:
            self.last_error = str(exc)
            log.error("speech failed: %s", exc)
        finally:
            self.state = previous if previous != "speaking" else "idle"

    # ---- confirmation for "ask" policies ---------------------------------
    def _confirm(self, question: str) -> bool:
        """Get a yes or a no — from the GUI if it is open, otherwise by voice.

        A window with Allow and Deny buttons is a far better place to answer a
        permission question than a microphone, so it wins whenever one is
        attached. Voice remains the fallback for a headless session.
        """
        if self._server is not None and self._server.subscribers:
            answered = self._confirm_in_ui(question)
            if answered is not None:
                return answered
        return self._confirm_by_voice(question)

    def _confirm_in_ui(self, question: str) -> bool | None:
        """Returns True/False, or None if no window answered in time."""
        request_id = uuid.uuid4().hex[:8]
        inbox: queue.Queue = queue.Queue(maxsize=1)
        self._pending[request_id] = inbox
        timeout = float(self.config.get("tools.confirm_timeout_s", 20))
        try:
            self.publish("confirm", id=request_id, question=question,
                         timeout=timeout)
            try:
                return bool(inbox.get(timeout=timeout))
            except queue.Empty:
                log.info("no window answered the permission question")
                return None
        finally:
            self._pending.pop(request_id, None)

    def _confirm_by_voice(self, question: str) -> bool:
        """Ask out loud, then listen for a yes or a no."""
        self.say(question)
        try:
            with Microphone(self.config) as mic:
                pcm = mic.record_utterance(wait_for_speech=True)
                if not pcm:
                    log.info("no answer to the confirmation prompt")
                    return False
                answer = self.stt.transcribe(pcm, mic.sample_rate).text
        except (AudioUnavailable, STTError) as exc:
            log.warning("could not hear a confirmation: %s", exc)
            return False

        log.info("confirmation answer: %r", answer)
        words = {w.strip(" .,!?") for w in answer.lower().split()}
        if words & _NO:
            return False
        return bool(words & _YES)

    # ---- control socket ---------------------------------------------------
    def _handle(self, request: dict) -> dict:
        name = str(request.get("command", ""))
        handler = getattr(self, f"_cmd_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown command {name!r}"}
        return handler(request)

    def _cmd_ping(self, request: dict) -> dict:
        return {"ok": True, "pong": True, "state": self.state}

    def _cmd_status(self, request: dict) -> dict:
        provider = str(self.config.get("brain.provider"))
        return {"ok": True, "state": self.state, "turns": self.turns,
                "uptime_s": round(time.monotonic() - self.started_at, 1),
                "brain": f"{provider}:{self.config.get(f'brain.{provider}.model')}",
                "stt": self.stt.name, "tts": self.tts.name,
                "wakeword": bool(self.wakeword and self.wakeword.active),
                "history_messages": len(self.agent.history),
                "conversation": self.agent.conversation.id,
                "conversation_title": self.agent.conversation.display_title(),
                "windows": self._server.subscribers if self._server else 0,
                "sudo": bool(self.config.get("tools.sudo.enabled", False)),
                "tools": len(self.agent.tools()),
                "last_error": self.last_error}

    def _cmd_listen(self, request: dict) -> dict:
        """Push-to-talk. In toggle mode a second press ends the utterance."""
        mode = str(self.config.get("ptt.mode", "toggle"))
        edge = str(request.get("edge", "press"))

        if self.state == "speaking":
            self.voice.stop()  # barge-in
            return {"ok": True, "action": "interrupted"}
        if mode == "hold" and edge == "release":
            self._stop_listening.set()
            return {"ok": True, "action": "stopped listening"}
        if self.state == "listening" and mode == "toggle":
            self._stop_listening.set()
            return {"ok": True, "action": "stopped listening"}
        self._turn_requested.set()
        self.publish("listen_requested")
        return {"ok": True, "action": "listening"}

    def _cmd_cancel(self, request: dict) -> dict:
        self.voice.stop()
        self._stop_listening.set()
        self._turn_requested.clear()
        return {"ok": True, "action": "cancelled"}

    def _cmd_ask(self, request: dict) -> dict:
        """Text in, text out — and spoken too unless the caller says otherwise."""
        text = str(request.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "no text given"}
        wanted = str(request.get("conversation", ""))
        if request.get("new"):
            self.agent.persist()
            self.agent.start_new()
        elif wanted and wanted != self.agent.conversation.id:
            self.agent.persist()
            if self.agent.open(wanted) is None:
                return {"ok": False, "error": f"no conversation {wanted}"}
        with self._turn_lock:
            self.turns += 1
            self.publish("heard", text=text,
                         conversation=self.agent.conversation.id)
            self.state = "thinking"
            try:
                reply = self._run_agent(text)
            finally:
                self.state = "idle"
        if request.get("speak", True) and reply:
            threading.Thread(target=self.say, args=(reply,), daemon=True).start()
        return {"ok": True, "reply": reply,
                "conversation": self.agent.conversation.id,
                "title": self.agent.conversation.display_title()}

    def _cmd_say(self, request: dict) -> dict:
        text = str(request.get("text", "")).strip()
        if not text:
            return {"ok": False, "error": "no text given"}
        threading.Thread(target=self.say, args=(text,), daemon=True).start()
        return {"ok": True, "action": "speaking"}

    def _cmd_confirm(self, request: dict) -> dict:
        """A window answering a permission question raised by _confirm_in_ui."""
        inbox = self._pending.get(str(request.get("id", "")))
        if inbox is None:
            return {"ok": False, "error": "that question is no longer waiting"}
        try:
            inbox.put_nowait(bool(request.get("allow", False)))
        except queue.Full:
            return {"ok": False, "error": "already answered"}
        return {"ok": True}

    def _cmd_reset(self, request: dict) -> dict:
        self.agent.start_new()
        self.publish("conversation", id=self.agent.conversation.id,
                     title=self.agent.conversation.display_title())
        return {"ok": True, "action": "conversation reset",
                "conversation": self.agent.conversation.id}

    # ---- conversations ----------------------------------------------------
    def _cmd_conversations(self, request: dict) -> dict:
        limit = int(request.get("limit", 50))
        return {"ok": True, "current": self.agent.conversation.id,
                "conversations": self.store.list(limit)}

    def _cmd_conversation(self, request: dict) -> dict:
        """Open, start, rename or delete a conversation."""
        action = str(request.get("action", "open"))
        conversation_id = str(request.get("id", ""))

        if action == "new":
            self.agent.persist()
            conversation = self.agent.start_new(str(request.get("title", "")))
        elif action == "open":
            if conversation_id == self.agent.conversation.id:
                conversation = self.agent.conversation
            else:
                self.agent.persist()
                conversation = self.agent.open(conversation_id)
                if conversation is None:
                    return {"ok": False, "error": f"no conversation {conversation_id}"}
        elif action == "delete":
            if not self.store.delete(conversation_id):
                return {"ok": False, "error": f"no conversation {conversation_id}"}
            if conversation_id == self.agent.conversation.id:
                self.agent.start_new()
            conversation = self.agent.conversation
        elif action == "rename":
            target = (self.agent.conversation
                      if conversation_id == self.agent.conversation.id
                      else self.store.load(conversation_id))
            if target is None:
                return {"ok": False, "error": f"no conversation {conversation_id}"}
            target.title = str(request.get("title", "")).strip()
            self.store.save(target)
            conversation = target
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}

        self.publish("conversation", id=self.agent.conversation.id,
                     title=self.agent.conversation.display_title(),
                     action=action)
        return {"ok": True, "action": action, "id": conversation.id,
                "title": conversation.display_title(),
                "transcript": conversation.transcript()}

    def _cmd_transcript(self, request: dict) -> dict:
        conversation_id = str(request.get("id", "")) or self.agent.conversation.id
        conversation = (self.agent.conversation
                        if conversation_id == self.agent.conversation.id
                        else self.store.load(conversation_id))
        if conversation is None:
            return {"ok": False, "error": f"no conversation {conversation_id}"}
        return {"ok": True, "id": conversation.id,
                "title": conversation.display_title(),
                "transcript": conversation.transcript()}

    def _cmd_config(self, request: dict) -> dict:
        """Read and write settings, so the GUI never touches the file itself."""
        action = str(request.get("action", "list"))
        if action == "list":
            return {"ok": True, "settings": self.config.flatten(),
                    "path": str(self.config.path)}
        if action == "set":
            changes = dict(request.get("values") or {})
            key = request.get("key")
            if key is not None:
                changes[str(key)] = request.get("value")
            if not changes:
                return {"ok": False, "error": "nothing to set"}
            from .config import coerce
            for name, value in changes.items():
                current = self.config.get(name)
                try:
                    self.config.set(name, coerce(value, current), save=False)
                except ValueError as exc:
                    return {"ok": False, "error": f"{name}: {exc}"}
            self.config.save()
            return self._cmd_reload(request) | {"changed": sorted(changes)}
        return {"ok": False, "error": f"unknown action {action!r}"}

    def _cmd_reload(self, request: dict) -> dict:
        """Re-read the config file and rebuild whatever changed."""
        fresh = Config.load()
        previous = self.config
        try:
            if fresh.section("brain") != previous.section("brain"):
                self.brain = build_brain(fresh)
            if fresh.section("stt") != previous.section("stt"):
                self.stt = build_stt(fresh)
            if fresh.section("tts") != previous.section("tts"):
                self.tts = build_tts(fresh)
        except (BrainError, STTError, TTSError) as exc:
            return {"ok": False, "error": f"reload failed, keeping the old "
                                          f"configuration: {exc}"}

        self.config = fresh
        self.store = store_for(fresh)
        self.agent.store = self.store
        self.player = Player(fresh)
        self.voice = Speaker.from_config(self.tts, self.player, fresh)
        self.agent.config = fresh
        self.agent.brain = self.brain
        self.agent.ctx.config = fresh
        self.agent.ctx.brain = self.brain
        # The vision model is cached per-context; a config change must drop it.
        self.agent.ctx.state.pop("vision", None)

        if self.wakeword is not None:
            self.wakeword.stop()
        self.wakeword = self._build_wakeword(fresh)
        self._warm()
        return {"ok": True, "action": "reloaded"}

    def _cmd_quit(self, request: dict) -> dict:
        threading.Thread(target=self._delayed_stop, daemon=True).start()
        return {"ok": True, "action": "shutting down"}

    def _delayed_stop(self) -> None:
        time.sleep(0.2)
        self.stop()


def run(config: Config | None = None) -> int:
    from .log import setup

    config = config or Config.load()
    setup(str(config.get("general.log_level", "info")))
    assistant = Assistant(config)
    assistant.start()
    assistant.run_forever()
    return 0
