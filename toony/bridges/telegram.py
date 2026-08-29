"""Talking to Toony from your phone.

A long-polling Telegram client on the standard library alone — no extra
dependency for something most people will never switch on. It runs inside the
daemon, so a message from your phone reaches the same assistant, the same
conversation and the same tools as your voice does.

Two things get careful treatment:

**Who is allowed.** A bot token is a URL anybody can use. Until a chat is
paired, every message is refused, and pairing needs a code that only appears on
your own machine. Without that, leaking the token would hand a stranger your
laptop.

**Being offline.** Telegram holds updates for us for 24 hours, so a message sent
while the laptop is asleep arrives later, not never. But a backlog delivered all
at once would be answered as if it were a conversation, so anything past the
limit gets an apology instead of an answer — as does any single message too
large to be worth sending to a model.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from ..log import get

log = get("telegram")

API = "https://api.telegram.org"
# Telegram's own ceiling on a single message.
WIRE_LIMIT = 4096


class TelegramError(RuntimeError):
    pass


def online(timeout: float = 3.0) -> bool:
    """Can we reach Telegram right now?"""
    try:
        with socket.create_connection(("api.telegram.org", 443), timeout=timeout):
            return True
    except OSError:
        return False


def call(token: str, method: str, http_timeout: float = 15.0, **params) -> Any:
    """One Bot API call. Raises :class:`TelegramError` for anything but success.

    ``http_timeout`` is the socket timeout. The Bot API has its own ``timeout``
    parameter for long polling, which goes through ``params`` like any other.
    """
    if not token:
        raise TelegramError("No bot token is set. Run: toony telegram setup")
    url = f"{API}/bot{token}/{method}"
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v
         for k, v in params.items() if v is not None}).encode()
    request = urllib.request.Request(url, data=data,
                                     headers={"User-Agent": "Toony"})
    try:
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        try:
            described = json.loads(body).get("description", body)
        except json.JSONDecodeError:
            described = body
        if exc.code == 401:
            raise TelegramError("That bot token was rejected. Get a new one from "
                                "@BotFather and run: toony telegram setup") from exc
        raise TelegramError(f"{method} failed: {described}") from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise TelegramError(f"could not reach Telegram: {exc}") from exc
    if not payload.get("ok"):
        raise TelegramError(payload.get("description", "unknown error"))
    return payload.get("result")


def describe_bot(token: str) -> str:
    me = call(token, "getMe")
    return f"@{me.get('username', '?')} ({me.get('first_name', '')})".strip()


def split_message(text: str, limit: int = WIRE_LIMIT) -> list[str]:
    """Break a long reply into messages Telegram will accept, at line breaks."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            parts.append((current + line[:limit - len(current)]).rstrip())
            line = line[limit - len(current):]
            current = ""
        if len(current) + len(line) > limit:
            parts.append(current.rstrip())
            current = line
        else:
            current += line
    if current.strip():
        parts.append(current.rstrip())
    return parts


class TelegramBridge:
    """Polls for messages and hands each one to the assistant."""

    def __init__(self, config, answer: Callable[[str, dict], str],
                 publish: Callable[..., None] | None = None,
                 save: Callable[[str, Any], None] | None = None):
        self.config = config
        self.answer = answer
        self.publish = publish or (lambda *a, **k: None)
        self.save = save
        self.token = config.api_key("telegram", "token")
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        # Separate from _running so a batch can be processed by a caller that
        # never started the polling thread, while stop() still cuts one short.
        self._stopping = threading.Event()
        self._offset = 0
        self._was_online = True
        self.last_error = ""
        self.messages = 0
        self.rejected = 0

    # ---- lifecycle --------------------------------------------------------
    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.active:
            return
        if not self.token:
            raise TelegramError("No bot token is set. Run: toony telegram setup")
        self._stopping.clear()
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="toony-telegram",
                                        daemon=True)
        self._thread.start()
        log.info("telegram bridge started")

    def stop(self) -> None:
        self._stopping.set()
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def status(self) -> dict:
        return {"running": self.active, "online": self._was_online,
                "messages": self.messages, "rejected": self.rejected,
                "chats": list(self._allowed()), "error": self.last_error}

    # ---- who may talk to it ----------------------------------------------
    def _allowed(self) -> set[str]:
        return {str(c) for c in (self.config.get("telegram.allowed_chats", []) or [])}

    def _allow(self, chat_id: str) -> None:
        chats = sorted(self._allowed() | {str(chat_id)})
        self.config.set("telegram.allowed_chats", chats, save=False)
        if self.save:
            self.save("telegram.allowed_chats", chats)
        else:
            self.config.save()
        log.info("paired telegram chat %s", chat_id)

    def _pairing_code(self) -> str:
        return str(self.config.get("telegram.pairing_code", "") or "").strip()

    # ---- the polling loop -------------------------------------------------
    def _loop(self) -> None:
        backoff = 2.0
        while self._running.is_set():
            try:
                poll = max(5, int(self.config.get("telegram.poll_seconds", 25)))
                updates = call(self.token, "getUpdates", http_timeout=poll + 10,
                               offset=self._offset or None, timeout=poll,
                               allowed_updates=["message"])
            except TelegramError as exc:
                self._go_offline(str(exc))
                slept = 0.0
                while self._running.is_set() and slept < backoff:
                    time.sleep(0.5)
                    slept += 0.5
                backoff = min(backoff * 1.7, 60.0)
                continue

            backoff = 2.0
            self._come_online()
            if updates:
                self._handle_batch(updates)

    def _go_offline(self, error: str) -> None:
        self.last_error = error
        if self._was_online:
            log.info("telegram unreachable (%s) — messages will arrive when it "
                     "comes back", error)
            self._was_online = False
            self.publish("telegram", online=False, error=error)

    def _come_online(self) -> None:
        if not self._was_online:
            log.info("telegram reachable again")
            self.last_error = ""
            self.publish("telegram", online=True)
        self._was_online = True

    # ---- messages ---------------------------------------------------------
    def _handle_batch(self, updates: list[dict]) -> None:
        """Answer what arrived; apologise for anything past the backlog limit.

        A pile of messages that queued up while the laptop was off is not a
        conversation. Answering the newest few and being honest about the rest
        beats replaying an hour of context at somebody.
        """
        for update in updates:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)

        messages = [u["message"] for u in updates
                    if isinstance(u.get("message"), dict)]
        limit = max(1, int(self.config.get("telegram.max_backlog", 20)))
        overflow = messages[:-limit] if len(messages) > limit else []
        current = messages[-limit:] if len(messages) > limit else messages

        if overflow:
            log.info("%d queued messages past the backlog limit — apologising",
                     len(overflow))
            self._apologise_for_backlog(overflow)
        for message in current:
            if self._stopping.is_set():
                return
            try:
                self._handle(message)
            except Exception:
                log.exception("telegram message failed")

    def _apologise_for_backlog(self, messages: list[dict]) -> None:
        by_chat: dict[str, int] = {}
        for message in messages:
            chat = str(message.get("chat", {}).get("id", ""))
            if chat in self._allowed():
                by_chat[chat] = by_chat.get(chat, 0) + 1
        self.rejected += sum(by_chat.values())
        for chat, count in by_chat.items():
            self._send(chat, f"Sorry — {count} earlier messages did not reach "
                             f"Toony. They piled up while it was offline, and "
                             f"that is too much to answer at once. Send the one "
                             f"that still matters.")

    def _handle(self, message: dict) -> None:
        chat = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()
        who = message.get("from", {}).get("first_name", "someone")
        if not chat or not text:
            return

        if chat not in self._allowed():
            self._handle_stranger(chat, text, who)
            return

        limit = int(self.config.get("telegram.max_message_chars", 4000))
        if len(text) > limit:
            self.rejected += 1
            self._send(chat, f"Sorry — your message did not reach Toony because "
                             f"it is too large. It was {len(text):,} characters "
                             f"and the limit is {limit:,}. Send a shorter one.")
            return

        if text.startswith("/"):
            if self._handle_command(chat, text):
                return

        self.messages += 1
        log.info("telegram message from %s: %s", who, text[:80])
        self.publish("telegram_message", chat=chat, who=who, text=text)

        placeholder = self._send(chat, "…")
        try:
            reply = self.answer(text, {"source": "telegram", "chat": chat,
                                       "who": who})
        except Exception as exc:
            log.exception("answering a telegram message failed")
            reply = f"Something went wrong here: {exc}"
        self._deliver(chat, placeholder, reply or "I had nothing to say.")

    def _handle_stranger(self, chat: str, text: str, who: str) -> None:
        code = self._pairing_code()
        if code and text.strip() == code:
            self._allow(chat)
            self._send(chat, "Paired. You can talk to Toony from here now — "
                             "try asking what is on the screen, or what is "
                             "wrong with the machine.")
            return
        log.warning("refused a telegram message from an unpaired chat %s (%s)",
                    chat, who)
        self.rejected += 1
        self._send(chat, "This bot is not paired with you. On the machine "
                         "running Toony, run `toony telegram pair` and send me "
                         "the code it prints.")

    def _handle_command(self, chat: str, text: str) -> bool:
        command = text.split()[0].lstrip("/").split("@")[0].lower()
        if command in ("start", "help"):
            self._send(chat, "I am Toony, on your Linux machine. Ask me "
                             "anything you would say out loud — what is playing, "
                             "what is wrong with the system, open something, "
                             "read the screen. /new starts a fresh conversation.")
            return True
        if command in ("new", "reset"):
            self.answer("", {"source": "telegram", "chat": chat, "new": True})
            self._send(chat, "Started a fresh conversation.")
            return True
        if command == "stop":
            self._send(chat, "Nothing to stop from here — say it out loud, or "
                             "press Escape in the window.")
            return True
        return False

    # ---- sending ----------------------------------------------------------
    def _send(self, chat: str, text: str) -> int | None:
        """Send a message, splitting it if needed. Returns the first id."""
        first: int | None = None
        for index, part in enumerate(split_message(text)):
            try:
                result = call(self.token, "sendMessage", chat_id=chat, text=part,
                              disable_notification=index > 0)
            except TelegramError as exc:
                log.warning("could not send to %s: %s", chat, exc)
                return first
            if index == 0 and isinstance(result, dict):
                first = result.get("message_id")
        return first

    def _deliver(self, chat: str, placeholder: int | None, reply: str) -> None:
        """Replace the "…" with the answer, or send it fresh if that fails."""
        parts = split_message(reply) or ["I had nothing to say."]
        if placeholder is not None:
            try:
                call(self.token, "editMessageText", chat_id=chat,
                     message_id=placeholder, text=parts[0])
                parts = parts[1:]
            except TelegramError as exc:
                log.debug("could not edit the placeholder: %s", exc)
        for part in parts:
            try:
                call(self.token, "sendMessage", chat_id=chat, text=part)
            except TelegramError as exc:
                log.warning("could not send to %s: %s", chat, exc)
                return
