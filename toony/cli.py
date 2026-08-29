"""The ``toony`` command line: run it, configure it, install it, diagnose it."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap

from . import DISPLAY_NAME, __version__
from .config import Config, default_config
from .paths import CONFIG_FILE, LOG_FILE, PIPER_DIR, ensure_dirs

# ANSI, but only when someone is actually looking at a terminal.
_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def ok(text: str) -> str:
    return _c(text, "32")


def warn(text: str) -> str:
    return _c(text, "33")


def bad(text: str) -> str:
    return _c(text, "31")


def dim(text: str) -> str:
    return _c(text, "2")


def bold(text: str) -> str:
    return _c(text, "1")


# ---------------------------------------------------------------- run/daemon
def cmd_run(args) -> int:
    from .app import run

    return run(Config.load())


def _offline(reply: dict) -> bool:
    """True when the failure was 'no daemon', rather than a real error."""
    error = str(reply.get("error", "")).lower()
    return "not running" in error or "stale" in error


def cmd_listen(args) -> int:
    from . import ipc

    reply = ipc.send("listen", edge=args.edge, timeout=10)
    if reply.get("ok"):
        print(reply.get("action", "ok"))
        return 0
    print(bad(str(reply.get("error", "failed"))), file=sys.stderr)
    return 1


def cmd_simple(command: str):
    def handler(args) -> int:
        from . import ipc

        reply = ipc.send(command, timeout=60)
        message = reply.get("action") or reply.get("error") or "done"
        print(message if reply.get("ok") else bad(str(message)),
              file=sys.stdout if reply.get("ok") else sys.stderr)
        return 0 if reply.get("ok") else 1

    return handler


def cmd_status(args) -> int:
    from . import ipc

    reply = ipc.send("status", timeout=10)
    if not reply.get("ok"):
        if args.json:
            print(json.dumps(reply, indent=2))
        else:
            print(f"{bold(DISPLAY_NAME)} is {bad('not running')}")
            print(dim(f"  {reply.get('error', '')}"))
        return 1
    if args.json:
        print(json.dumps(reply, indent=2))
        return 0
    print(f"{bold(DISPLAY_NAME)} is {ok(reply['state'])}")
    for key in ("brain", "stt", "tts", "wakeword", "turns", "uptime_s",
                "history_messages"):
        print(f"  {key:18} {reply.get(key)}")
    if reply.get("last_error"):
        print(f"  {'last_error':18} {warn(str(reply['last_error']))}")
    return 0


def cmd_ask(args) -> int:
    from . import ipc

    text = " ".join(args.text).strip()
    if not text:
        print("Nothing to ask.", file=sys.stderr)
        return 2
    reply = ipc.send("ask", text=text, speak=not args.quiet, timeout=300,
                     new=bool(args.new))
    if reply.get("ok"):
        print(reply.get("reply", ""))
        return 0
    if _offline(reply):
        return _ask_locally(text, speak=not args.quiet, fresh=bool(args.new))
    print(bad(str(reply.get("error", "failed"))), file=sys.stderr)
    return 1


def _ask_locally(text: str, speak: bool, fresh: bool = False) -> int:
    """No daemon: build just enough of the assistant to answer once.

    The conversation is still loaded from and saved to disk, so two `toony ask`
    calls in a row follow on from each other even with nothing running.
    """
    from .agent import Agent
    from .brain import build_brain
    from .brain.base import BrainError
    from .log import setup

    config = Config.load()
    setup(str(config.get("general.log_level", "warning")), to_file=False)
    try:
        agent = Agent(config, build_brain(config), confirm=_terminal_confirm)
    except BrainError as exc:
        print(bad(str(exc)), file=sys.stderr)
        return 1
    if not fresh:
        agent.resume()
    reply = agent.ask(text)
    print(reply)
    if speak:
        _speak_locally(config, reply)
    return 0


def _speak_locally(config, text: str) -> bool:
    """Speak without a daemon — used by `toony ask` and `toony say`."""
    try:
        from .audio.playback import Player
        from .tts import build_tts
        from .tts.speaker import Speaker

        Speaker.from_config(build_tts(config), Player(config), config).say(text)
        return True
    except Exception as exc:
        print(dim(f"(could not speak it: {exc})"), file=sys.stderr)
        return False


def _terminal_confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_say(args) -> int:
    from . import ipc

    text = " ".join(args.text)
    reply = ipc.send("say", text=text, timeout=120)
    if reply.get("ok"):
        return 0
    if _offline(reply):
        return 0 if _speak_locally(Config.load(), text) else 1
    print(bad(str(reply.get("error", "failed"))), file=sys.stderr)
    return 1


# -------------------------------------------------------------------- config
def cmd_config(args) -> int:
    config = Config.load()

    if args.action == "path":
        print(CONFIG_FILE)
        return 0

    if args.action == "list":
        defaults = Config().flatten()
        for key, value in sorted(config.flatten().items()):
            if args.key and not key.startswith(args.key):
                continue
            changed = defaults.get(key) != value
            rendered = json.dumps(value)
            line = f"{key:38} {rendered}"
            print(bold(line) + dim("  (changed)") if changed else line)
        return 0

    if args.action == "get":
        if not args.key:
            print("Which setting? Try: toony config list", file=sys.stderr)
            return 2
        if not config.has(args.key):
            print(bad(f"no such setting: {args.key}"), file=sys.stderr)
            return 1
        value = config.get(args.key)
        print(json.dumps(value) if not isinstance(value, str) else value)
        return 0

    if args.action == "set":
        if not args.key or args.value is None:
            print("Usage: toony config set <key> <value>", file=sys.stderr)
            return 2
        if not config.has(args.key):
            print(bad(f"no such setting: {args.key}"), file=sys.stderr)
            print(dim("See all settings with: toony config list"), file=sys.stderr)
            return 1
        try:
            value = config.set(args.key, args.value)
        except ValueError as exc:
            print(bad(f"{args.key}: {exc}"), file=sys.stderr)
            return 1
        print(f"{args.key} = {json.dumps(value)}")
        _nudge_reload()
        return 0

    if args.action == "unset":
        try:
            config.unset(args.key)
        except KeyError:
            print(bad(f"no such setting: {args.key}"), file=sys.stderr)
            return 1
        print(f"{args.key} reset to {json.dumps(config.get(args.key))}")
        _nudge_reload()
        return 0

    if args.action == "edit":
        ensure_dirs()
        if not CONFIG_FILE.exists():
            config.save()
        editor = os.environ.get("EDITOR") or shutil.which("nano") or "vi"
        subprocess.call([editor, str(CONFIG_FILE)])
        try:
            Config.load()
        except Exception as exc:
            print(bad(f"the file no longer parses: {exc}"), file=sys.stderr)
            return 1
        _nudge_reload()
        return 0

    if args.action == "reset":
        if CONFIG_FILE.exists():
            backup = CONFIG_FILE.with_suffix(".toml.bak")
            shutil.copy2(CONFIG_FILE, backup)
            print(dim(f"previous config saved to {backup}"))
        Config(default_config()).save()
        print(f"{CONFIG_FILE} reset to defaults")
        _nudge_reload()
        return 0

    if args.action == "init":
        ensure_dirs()
        if CONFIG_FILE.exists() and not args.value:
            print(f"{CONFIG_FILE} already exists. Use --force to overwrite.")
            return 1
        config.save()
        print(f"wrote {CONFIG_FILE}")
        return 0

    return 2


def _nudge_reload() -> None:
    """Apply configuration changes to a running daemon, quietly."""
    from . import ipc

    if not ipc.is_running():
        return
    reply = ipc.send("reload", timeout=120)
    print(dim("reloaded the running assistant" if reply.get("ok")
              else f"reload failed: {reply.get('error')}"))


# ------------------------------------------------------------------ hardware
def cmd_devices(args) -> int:
    from .audio.devices import AudioUnavailable, list_devices

    try:
        devices = list_devices()
    except AudioUnavailable as exc:
        print(bad(str(exc)), file=sys.stderr)
        return 1
    print(bold(f"{'#':>3} {'in':>3} {'out':>3} {'rate':>6}  name"))
    for device in devices:
        default = ""
        if device["default_in"]:
            default += ok(" [default in]")
        if device["default_out"]:
            default += ok(" [default out]")
        print(f"{device['index']:>3} {device['inputs']:>3} {device['outputs']:>3} "
              f"{device['rate']:>6}  {device['name']}{default}")
    print(dim("\nChoose one by number or by part of its name:"))
    print(dim("  toony config set audio.input_device 'HD Audio'"))
    return 0


def cmd_tools(args) -> int:
    from .safety import decision_for
    from .tools import REGISTRY

    config = Config.load()
    enabled = {t.name for t in REGISTRY.enabled(config)}
    risk_colour = {"safe": ok, "sensitive": warn, "dangerous": bad}
    gate_colour = {"allow": ok, "ask": warn, "deny": bad}
    counts = {"allow": 0, "ask": 0, "deny": 0}

    for tool in REGISTRY.all():
        available = tool.name in enabled
        gate = decision_for(config, tool)
        if available:
            counts[gate] += 1
        if args.risk and tool.risk != args.risk:
            continue
        state = ok("on ") if available else dim("off")
        missing = f"  needs {', '.join(tool.missing())}" if tool.missing() else ""
        print(f"{state} {tool.name:22} {risk_colour[tool.risk](tool.risk):<20}"
              f"{gate_colour[gate](gate):<18}"
              f"{dim(tool.description.split('.')[0][:52])}{warn(missing)}")

    print(dim(f"\n{len(enabled)} of {len(REGISTRY.all())} tools available: "
              f"{counts['allow']} run freely, {counts['ask']} ask first, "
              f"{counts['deny']} refused."))
    print(dim("Change a class with tools.policy_safe / _sensitive / _dangerous, "
              "or one tool with tools.always_allow / always_ask / never."))
    return 0


def cmd_transcribe(args) -> int:
    from .audio.wav import wav_to_pcm
    from .stt import build_stt

    with open(args.file, "rb") as handle:
        pcm, rate = wav_to_pcm(handle.read())
    transcript = build_stt(Config.load()).transcribe(pcm, rate)
    print(transcript.text)
    if transcript.confidence < 0.5:
        print(dim(f"(low confidence: {transcript.confidence:.2f})"), file=sys.stderr)
    return 0


# -------------------------------------------------------------------- doctor
def cmd_doctor(args) -> int:
    from pathlib import Path

    from . import ipc
    from .tools import REGISTRY
    from .tools.proc import which

    config = Config.load()
    problems = 0

    def check(label: str, good: bool, detail: str = "", fatal: bool = True) -> None:
        nonlocal problems
        if good:
            print(f"  {ok('ok')}    {label:26} {dim(detail)}")
        else:
            print(f"  {bad('FAIL') if fatal else warn('warn')}  {label:26} {detail}")
            if fatal:
                problems += 1

    print(bold(f"\n{DISPLAY_NAME} {__version__} — system check\n"))

    print(bold("python packages"))
    for module, why, needed in [
        ("sounddevice", "microphone and speaker", True),
        ("numpy", "audio processing", True),
        ("anthropic", "Claude brain", config.get("brain.provider") == "claude"),
        ("openai", "OpenAI-compatible brain, cloud speech",
         config.get("brain.provider") in ("openai", "ollama")),
        ("faster_whisper", "local speech recognition",
         config.get("stt.provider") == "local"),
        ("openwakeword", "wake word", bool(config.get("wakeword.enabled"))),
        ("webrtcvad", "voice activity detection", False),
        ("PySide6", "the Toony window", bool(config.get("ui.enabled", True))),
    ]:
        found = _importable(module)
        check(module, found, why if found else f"pip install {module}  ({why})",
              fatal=needed)

    print(bold("\nsystem commands"))
    for binary, why, needed in [
        ("piper", "speech synthesis", config.get("tts.provider") == "piper"),
        ("wpctl", "volume control (PipeWire)", False),
        ("pactl", "volume control (PulseAudio)", False),
        ("spectacle", "screenshots on KDE", False),
        ("grim", "screenshots on wlroots", False),
        ("playerctl", "media control", False),
        ("wl-copy", "clipboard on Wayland", False),
        ("notify-send", "desktop notifications", False),
        ("kdotool", "window control on Wayland", False),
        ("brightnessctl", "screen brightness", False),
        ("journalctl", "reading system logs", False),
        ("systemd-run", "timers and reminders", False),
        ("nmcli", "network status", False),
        ("bluetoothctl", "Bluetooth control", False),
        ("kwriteconfig6", "binding the push-to-talk key on KDE", False),
    ]:
        found = bool(which(binary))
        check(binary, found, why if found else f"not installed  ({why})", fatal=needed)

    print(bold("\naudio"))
    try:
        from .audio.devices import list_devices

        devices = list_devices()
        inputs = [d for d in devices if d["inputs"]]
        check("input devices", bool(inputs),
              f"{len(inputs)} available" if inputs else "no microphone found")
    except Exception as exc:
        check("input devices", False, str(exc))

    print(bold("\nbackends"))
    from .brain import vision_summary

    summary = vision_summary(config)
    check("vision", "cannot read images" not in summary, summary, fatal=False)
    if config.get("wakeword.enabled"):
        engine = str(config.get("wakeword.engine"))
        phrase = (config.get("wakeword.phrase") if engine == "whisper"
                  else config.get("wakeword.model"))
        needed = "faster_whisper" if engine == "whisper" else "openwakeword"
        check(f"wake word {phrase!r}", _importable(needed),
              f"{engine} engine" if _importable(needed)
              else f"needs {needed} — pip install 'toony[{'local' if engine == 'whisper' else 'wake'}]'",
              fatal=False)
    else:
        check("wake word", True, "off (toony wakeword \"hey toony\")", fatal=False)
    workspace = Path(str(config.get("tools.code.root", "~/Projects"))).expanduser()
    check("code workspace", workspace.is_dir(),
          str(workspace) if workspace.is_dir()
          else f"{workspace} does not exist — toony config set tools.code.root ~/code",
          fatal=False)
    for label, builder in (("brain", "brain"), ("speech recognition", "stt"),
                           ("speech synthesis", "tts")):
        try:
            if builder == "brain":
                from .brain import build_brain
                component = build_brain(config)
            elif builder == "stt":
                from .stt import build_stt
                component = build_stt(config)
            else:
                from .tts import build_tts
                component = build_tts(config)
            status = component.check()
            check(label, not _looks_broken(status), status)
        except Exception as exc:
            check(label, False, f"{exc.__class__.__name__}: {exc}")

    print(bold("\nservice"))
    check("config file", CONFIG_FILE.exists(),
          str(CONFIG_FILE) if CONFIG_FILE.exists()
          else "not written yet — run: toony config init", fatal=False)
    unit = _unit_path()
    check("systemd unit", unit.exists(),
          str(unit) if unit.exists() else "run: toony install", fatal=False)
    running = ipc.is_running()
    check("daemon", running, "listening on the control socket"
          if running else "not running — start with: systemctl --user start toony",
          fatal=False)
    check("tools", True, f"{len(REGISTRY.enabled(config))} of "
          f"{len(REGISTRY.all())} available")

    print(bold("\ndesktop"))
    entry = _desktop_path()
    bound = [c for group, action, c in _read_bindings()
             if "toony" in group.lower() and action == "_launch"]
    wanted = str(config.get("ptt.shortcut", "Meta+Space"))
    check("launcher entry", entry.exists()
          and "X-KDE-GlobalAccel-CommandShortcut=true"
          in entry.read_text(encoding="utf-8", errors="replace"),
          str(entry) if entry.exists() else "run: toony install", fatal=False)
    check(f"hotkey {wanted}", bool(bound)
          and any(_normalise(c) == _normalise(wanted) for c in bound),
          ", ".join(bound) if bound else "not bound — run: toony shortcut",
          fatal=False)
    clashes = conflicts(wanted)
    check("hotkey is unique", not clashes,
          "nothing else claims it" if not clashes
          else "also used by " + ", ".join(a for _, a in clashes[:3]),
          fatal=False)
    check("window autostart", _autostart_path().exists(),
          str(_autostart_path()) if _autostart_path().exists()
          else "the tray icon will not appear at login", fatal=False)

    print(bold("\nconversations and access"))
    from .history import store_for

    saved = store_for(config).list(limit=1000)
    check("saved conversations", True, f"{len(saved)} stored", fatal=False)
    sudo_on = bool(config.get("tools.sudo.enabled", False))
    if sudo_on:
        from .tools.proc import sudo_ready

        check("administrator access", sudo_ready(),
              "passwordless sudo works" if sudo_ready()
              else "enabled but sudo asks for a password — see: toony sudo status",
              fatal=False)
    else:
        check("administrator access", True, "off (toony sudo enable)", fatal=False)

    if problems:
        print(bad(f"\n{problems} problem(s) need attention.\n"))
        return 1
    print(ok("\nEverything essential is in place.\n"))
    return 0


def _looks_broken(status: str) -> bool:
    lowered = status.lower()
    return any(word in lowered for word in
               ("unreachable", "unavailable", "not installed", "failed",
                "is not in", "no check"))


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# -------------------------------------------------------------------- voices
PIPER_BASE = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
              "{lang}/{region}/{name}/{quality}/{voice}")

SUGGESTED_VOICES = ["en_US-amy-medium", "en_US-lessac-medium",
                    "en_US-ryan-high", "en_GB-alba-medium",
                    "en_GB-northern_english_male-medium", "en_US-libritts_r-medium"]


def cmd_voices(args) -> int:
    if args.action == "list":
        installed = sorted(p.stem for p in PIPER_DIR.glob("*.onnx"))
        print(bold("installed"))
        for name in installed or ["  (none)"]:
            print(f"  {name}")
        print(bold("\nsuggested"))
        for name in SUGGESTED_VOICES:
            mark = ok(" installed") if name in installed else ""
            print(f"  {name}{mark}")
        print(dim("\nInstall one with: toony voices install en_US-amy-medium"))
        print(dim("Browse them all at https://rhasspy.github.io/piper-samples/"))
        return 0

    if args.action == "install":
        return _install_voice(args.name)
    return 2


def _install_voice(voice: str) -> int:
    import urllib.request

    try:
        language, name, quality = voice.split("-")
        region = language.split("_")[0]
    except ValueError:
        print(bad("Voice names look like en_US-amy-medium"), file=sys.stderr)
        return 2

    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    for suffix in (".onnx", ".onnx.json"):
        url = PIPER_BASE.format(lang=region, region=language, name=name,
                                quality=quality, voice=voice + suffix)
        target = PIPER_DIR / (voice + suffix)
        if target.exists() and target.stat().st_size > 0:
            print(dim(f"already have {target.name}"))
            continue
        print(f"downloading {target.name} ...")
        try:
            urllib.request.urlretrieve(url, target)
        except Exception as exc:
            target.unlink(missing_ok=True)
            print(bad(f"download failed: {exc}"), file=sys.stderr)
            print(dim(f"url was {url}"), file=sys.stderr)
            return 1
    print(ok(f"installed {voice} into {PIPER_DIR}"))
    config = Config.load()
    config.set("tts.piper.voice", voice)
    print(f"tts.piper.voice = {voice}")
    _nudge_reload()
    return 0


# ------------------------------------------------------------------- install
SERVICE_UNIT = """\
[Unit]
Description={display} voice assistant
Documentation=https://github.com/you/toony
After=graphical-session.target pipewire.service
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={executable} run
Restart=on-failure
RestartSec=3
# Give the model time to load before systemd decides it hung.
TimeoutStartSec=120
Slice=session.slice

[Install]
WantedBy=graphical-session.target default.target
"""

# X-KDE-GlobalAccel-CommandShortcut is the line that makes this work. Without
# it kglobalaccel ignores the entry, the binding in kglobalshortcutsrc points at
# nothing, and the key does nothing at all — which is exactly the symptom.
DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name={display}: talk
Comment=Start or stop talking to {display}
Exec={executable} listen
Icon={icon}
Terminal=false
NoDisplay=true
StartupNotify=false
X-KDE-GlobalAccel-CommandShortcut=true
Categories=Utility;
"""

GUI_ENTRY = """\
[Desktop Entry]
Type=Application
Name={display}
GenericName=Voice assistant
Comment=Talk to {display}
Exec={executable} gui
Icon={icon}
Terminal=false
StartupNotify=true
StartupWMClass=toony
Categories=Utility;Audio;
Keywords=voice;assistant;speech;ai;
"""

AUTOSTART_ENTRY = """\
[Desktop Entry]
Type=Application
Name={display} window
Comment=Keep {display} in the system tray
Exec={executable} gui --hidden
Icon={icon}
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
"""


def _unit_path():
    from pathlib import Path

    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "systemd" / "user" / "toony.service"


def _desktop_path():
    from .paths import APPLICATIONS_DIR

    return APPLICATIONS_DIR / "toony-listen.desktop"


def _gui_desktop_path():
    from .paths import APPLICATIONS_DIR

    return APPLICATIONS_DIR / "toony.desktop"


def _autostart_path():
    from .paths import AUTOSTART_DIR

    return AUTOSTART_DIR / "toony-window.desktop"


def _icon_path():
    from .paths import ICON_DIR

    return ICON_DIR / "toony.png"


def _install_icon(config) -> str:
    """Render the avatar into the icon theme. Returns the name to reference.

    A themed icon name beats an absolute path: KDE caches it, the tray finds it,
    and it survives the file moving. If Qt is not installed the entries fall
    back to a stock icon rather than showing a broken image.
    """
    url = str(config.get("ui.avatar_url", ""))
    try:
        from .ui import avatar

        if url and avatar.save_icon_file(url, _icon_path()):
            for updater in (["gtk-update-icon-cache", "-qtf",
                             str(_icon_path().parents[2])],
                            ["kbuildsycoca6"], ["kbuildsycoca5"]):
                if shutil.which(updater[0]):
                    subprocess.call(updater, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
                    break
            return "toony"
    except Exception as exc:
        print(dim(f"(could not render the avatar icon: {exc})"))
    return "audio-input-microphone"


def _executable() -> str:
    """The absolute path to this CLI, so systemd does not depend on PATH."""
    found = shutil.which("toony")
    if found:
        return found
    return f"{sys.executable} -m toony"


def cmd_install(args) -> int:
    ensure_dirs()
    executable = _executable()
    config = Config.load()

    if not CONFIG_FILE.exists():
        config.save()
        print(f"wrote {CONFIG_FILE}")

    icon = _install_icon(config)
    if icon == "toony":
        print(f"wrote {_icon_path()}")

    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(SERVICE_UNIT.format(display=DISPLAY_NAME, executable=executable),
                    encoding="utf-8")
    print(f"wrote {unit}")

    for path, template in ((_desktop_path(), DESKTOP_ENTRY),
                           (_gui_desktop_path(), GUI_ENTRY)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(display=DISPLAY_NAME, executable=executable,
                                        icon=icon), encoding="utf-8")
        print(f"wrote {path}")

    autostart = _autostart_path()
    if config.get("ui.autostart", True) and not args.no_autostart:
        autostart.parent.mkdir(parents=True, exist_ok=True)
        autostart.write_text(AUTOSTART_ENTRY.format(
            display=DISPLAY_NAME, executable=executable, icon=icon), encoding="utf-8")
        print(f"wrote {autostart}  {dim('(tray icon at login)')}")
    elif autostart.exists():
        autostart.unlink()

    # The .desktop cache must know about the new entry before kglobalaccel can
    # bind a key to it, so refresh it before touching the shortcut.
    for builder in ("kbuildsycoca6", "kbuildsycoca5"):
        if shutil.which(builder):
            subprocess.call([builder], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            break

    if shutil.which("systemctl"):
        subprocess.call(["systemctl", "--user", "daemon-reload"])
        subprocess.call(["systemctl", "--user", "enable", "toony.service"])
        print(ok("enabled toony.service — it will start when you log in"))
        if not args.no_start:
            subprocess.call(["systemctl", "--user", "restart", "toony.service"])
            print(ok("started toony.service"))
    else:
        print(warn("systemctl not found — skipped service installation"))

    if not args.no_shortcut:
        _install_shortcut(str(config.get("ptt.shortcut", "Meta+Space")))

    print(dim("\nCheck everything with: toony doctor"))
    return 0


# ------------------------------------------------------------------ shortcut
def _shortcuts_file():
    from pathlib import Path

    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "kglobalshortcutsrc"


def _normalise(combo: str) -> str:
    """Compare shortcuts the way a person means them, not byte for byte."""
    text = combo.strip().replace("Super", "Meta").replace("Win", "Meta")
    parts = [p.strip().title() for p in text.split("+") if p.strip()]
    modifiers = sorted(p for p in parts if p in ("Meta", "Ctrl", "Alt", "Shift"))
    keys = [p for p in parts if p not in ("Meta", "Ctrl", "Alt", "Shift")]
    return "+".join(modifiers + keys)


def _read_bindings() -> list[tuple[str, str, str]]:
    """Every (group, action, shortcut) KDE currently has bound."""
    path = _shortcuts_file()
    if not path.exists():
        return []
    out: list[tuple[str, str, str]] = []
    group = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            group = line.strip("[]").replace("][", " · ")
            continue
        if not line or line.startswith("#") or "=" not in line:
            continue
        action, _, value = line.partition("=")
        # value is "active,default,display"; active may hold several, tab-separated.
        active = value.split(",")[0]
        for combo in active.split("\t"):
            if combo and combo.lower() not in ("none", ""):
                out.append((group, action.strip(), combo.strip()))
    return out


def conflicts(shortcut: str) -> list[tuple[str, str]]:
    """Who else has this key, ignoring Toony's own binding."""
    wanted = _normalise(shortcut)
    found = []
    for group, action, combo in _read_bindings():
        if _normalise(combo) == wanted and "toony" not in group.lower():
            found.append((group, action))
    return found


def _restart_kglobalaccel() -> bool:
    """Make kglobalaccel re-read the file it does not watch."""
    for argv in (["systemctl", "--user", "restart", "plasma-kglobalaccel.service"],
                 ["systemctl", "--user", "restart", "kglobalacceld.service"]):
        if shutil.which(argv[0]):
            if subprocess.call(argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0:
                return True
    for argv in (["kquitapp6", "kglobalaccel"], ["kquitapp5", "kglobalaccel"]):
        if shutil.which(argv[0]):
            subprocess.call(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            return True
    return False


def _install_shortcut(shortcut: str) -> None:
    """Register the push-to-talk key with KDE's global shortcut daemon."""
    if not _desktop_path().exists():
        print(warn("the launcher entry is missing — run: toony install"))
        return

    clashes = conflicts(shortcut)
    if clashes:
        print(warn(f"{shortcut} is already taken by "
                   + ", ".join(f"{action} ({group})" for group, action in clashes[:3])))
        print(dim("  KDE gives the key to whoever grabbed it first, so the "
                  "existing binding usually wins."))
        print(dim(f"  Pick another with: toony shortcut \"Meta+Alt+Space\""))

    writer = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    if not writer:
        print(warn("kwriteconfig6 not found — bind the shortcut yourself:"))
        print(dim("  System Settings > Shortcuts > Add > Command, "
                  f"set it to '{_executable()} listen' and assign {shortcut}"))
        return

    subprocess.call([writer, "--file", "kglobalshortcutsrc",
                     "--group", "services", "--group", "toony-listen.desktop",
                     "--key", "_launch", f"{shortcut},none,{DISPLAY_NAME}: talk"])
    # kglobalaccel needs the friendly name too, or the entry looks orphaned in
    # System Settings and can be pruned on the next login.
    subprocess.call([writer, "--file", "kglobalshortcutsrc",
                     "--group", "services", "--group", "toony-listen.desktop",
                     "--key", "_k_friendly_name", f"{DISPLAY_NAME}: talk"])

    if _restart_kglobalaccel():
        print(ok(f"bound {shortcut} to push-to-talk"))
    else:
        print(ok(f"wrote the {shortcut} binding"))
        print(warn("could not restart kglobalaccel — log out and back in"))
    print(dim("Test it with: toony shortcut --status"))


def cmd_shortcut(args) -> int:
    config = Config.load()
    shortcut = args.keys or str(config.get("ptt.shortcut", "Meta+Space"))

    if args.status:
        return _shortcut_status(shortcut)
    if args.clear:
        writer = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
        if writer:
            subprocess.call([writer, "--file", "kglobalshortcutsrc",
                             "--group", "services", "--group",
                             "toony-listen.desktop", "--key", "_launch", "none"])
            _restart_kglobalaccel()
            print(ok("cleared the push-to-talk binding"))
        return 0
    if args.keys:
        config.set("ptt.shortcut", shortcut)
    _install_shortcut(shortcut)
    return 0


def _shortcut_status(shortcut: str) -> int:
    """Explain, line by line, why the hotkey does or does not work."""
    from . import ipc

    print(f"{bold('push-to-talk')} {shortcut}\n")
    healthy = True

    entry = _desktop_path()
    if entry.exists():
        body = entry.read_text(encoding="utf-8", errors="replace")
        if "X-KDE-GlobalAccel-CommandShortcut=true" in body:
            print(f"  {ok('ok')}      launcher entry {entry}")
        else:
            healthy = False
            print(f"  {bad('broken')}  {entry} is missing "
                  f"X-KDE-GlobalAccel-CommandShortcut — run: toony install")
    else:
        healthy = False
        print(f"  {bad('missing')} {entry} — run: toony install")

    bound = [combo for group, action, combo in _read_bindings()
             if "toony" in group.lower() and action == "_launch"]
    if bound:
        matches = any(_normalise(c) == _normalise(shortcut) for c in bound)
        print(f"  {ok('ok') if matches else warn('differs')}      "
              f"kglobalshortcutsrc has {', '.join(bound)}")
        healthy = healthy and matches
    else:
        healthy = False
        print(f"  {bad('missing')} no binding in {_shortcuts_file()}"
              f" — run: toony shortcut")

    clashes = conflicts(shortcut)
    if clashes:
        healthy = False
        print(f"  {warn('clash')}   also bound to "
              + ", ".join(f"{a} ({g})" for g, a in clashes[:4]))
    else:
        print(f"  {ok('ok')}      nothing else claims {shortcut}")

    running = subprocess.call(["pgrep", "-x", "-u", str(os.getuid()),
                               "kglobalacceld"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) == 0 \
        if shutil.which("pgrep") else None
    if running is False:
        healthy = False
        print(f"  {bad('stopped')} kglobalacceld is not running")
    elif running:
        print(f"  {ok('ok')}      kglobalacceld is running")

    if ipc.is_running():
        print(f"  {ok('ok')}      the daemon is up and answering")
    else:
        healthy = False
        print(f"  {bad('down')}    the daemon is not running — "
              f"start it: systemctl --user start toony")

    session = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
    print(dim(f"\n  session {session}, desktop {desktop}"))
    if "kde" not in desktop.lower():
        print(warn("  This binding is KDE-specific. On another desktop, add a "
                   "custom shortcut"))
        print(dim(f"  that runs: {_executable()} listen"))

    print()
    print(ok("The shortcut looks correctly set up.") if healthy
          else warn("Fix the lines marked above, then try again."))
    return 0 if healthy else 1


def cmd_uninstall(args) -> int:
    if shutil.which("systemctl"):
        subprocess.call(["systemctl", "--user", "disable", "--now", "toony.service"])
    for path in (_unit_path(), _desktop_path(), _gui_desktop_path(),
                 _autostart_path(), _icon_path()):
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    writer = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    if writer:
        subprocess.call([writer, "--file", "kglobalshortcutsrc", "--group",
                         "services", "--group", "toony-listen.desktop",
                         "--key", "_launch", "--delete"])
        _restart_kglobalaccel()
        print("removed the push-to-talk binding")
    if shutil.which("systemctl"):
        subprocess.call(["systemctl", "--user", "daemon-reload"])
    print(dim(f"Configuration in {CONFIG_FILE.parent} was left alone."))
    return 0


# ------------------------------------------------------------------ presets
# One command for "run everything here" versus "use the good models", because
# flipping five settings by hand is how people end up with a cloud brain and a
# local voice that no longer agree about anything.
PRESETS = {
    "local": {
        "brain.provider": "ollama",
        "stt.provider": "local",
        "tts.provider": "piper",
        "vision.provider": "auto",
    },
    "cloud": {
        "brain.provider": "claude",
        "stt.provider": "openai",
        "tts.provider": "openai",
        "vision.provider": "brain",
    },
    "hybrid": {
        "brain.provider": "claude",
        "stt.provider": "local",
        "tts.provider": "piper",
        "vision.provider": "brain",
    },
    "claude": {"brain.provider": "claude", "vision.provider": "brain"},
    "openai": {"brain.provider": "openai", "vision.provider": "brain"},
    "ollama": {"brain.provider": "ollama", "vision.provider": "auto"},
}

_PRESET_BLURB = {
    "local": "everything on this laptop — no API key, no network, nothing leaves",
    "cloud": "the best models, over the network, billed to your API key",
    "hybrid": "local ears and voice, cloud brain (the fastest good setup)",
    "claude": "Claude answers; speech stays as it is",
    "openai": "an OpenAI-compatible endpoint answers; speech stays as it is",
    "ollama": "a local Ollama model answers; speech stays as it is",
}


def cmd_use(args) -> int:
    config = Config.load()
    preset = (args.preset or "").lower()
    if not preset:
        return _show_stack(config)
    if preset not in PRESETS:
        print(bad(f"There is no {preset!r} preset."), file=sys.stderr)
        print(dim("  " + ", ".join(PRESETS)), file=sys.stderr)
        return 2

    changes = dict(PRESETS[preset])
    if args.model:
        provider = changes.get("brain.provider", config.get("brain.provider"))
        changes[f"brain.{provider}.model"] = args.model
    for key, value in changes.items():
        config.set(key, value, save=False)
    config.save()

    print(ok(f"switched to {preset} — {_PRESET_BLURB[preset]}"))
    print()
    _show_stack(config)
    _warn_about_keys(config)
    _reload_daemon()
    return 0


def _show_stack(config) -> int:
    from .brain import vision_summary

    brain = str(config.get("brain.provider"))
    rows = [
        ("brain", f"{brain}:{config.get(f'brain.{brain}.model')}"),
        ("vision", vision_summary(config)),
        ("ears", _stack_line(config, "stt")),
        ("voice", _stack_line(config, "tts")),
        ("personality", str(config.get("general.personality"))),
        ("focus", str(config.get("general.focus"))),
    ]
    for label, value in rows:
        print(f"  {label:12} {value}")
    print(dim("\n  change it with: toony use local | cloud | hybrid | "
              "claude | openai | ollama"))
    return 0


def _stack_line(config, section: str) -> str:
    provider = str(config.get(f"{section}.provider"))
    model = (config.get(f"{section}.{provider}.model")
             or config.get(f"{section}.{provider}.voice") or "")
    return f"{provider}:{model}" if model else provider


def _warn_about_keys(config) -> None:
    provider = str(config.get("brain.provider"))
    if provider == "ollama":
        return
    if not config.api_key(f"brain.{provider}"):
        env = config.get(f"brain.{provider}.api_key_env", "")
        print()
        print(warn(f"No API key for {provider} yet."))
        print(dim(f"  export {env}=...   (or: toony config set "
                  f"brain.{provider}.api_key sk-...)"))


def cmd_personality(args) -> int:
    from .brain.prompts import PERSONALITIES

    config = Config.load()
    if not args.style:
        current = str(config.get("general.personality"))
        print(f"{bold('personality')} {current}")
        for name in list(PERSONALITIES) + ["custom"]:
            mark = ok(" ←") if name == current else ""
            print(f"  {name}{mark}")
        print(dim("\n  toony personality spicy"))
        return 0
    style = args.style.lower()
    if style not in PERSONALITIES and style != "custom":
        print(bad(f"unknown personality {style!r}"), file=sys.stderr)
        return 2
    config.set("general.personality", style)
    print(ok(f"personality is now {style}"))
    _reload_daemon()
    return 0


# --------------------------------------------------------------- wake word
def cmd_wakeword(args) -> int:
    from .audio.wakeword import suggest_engine

    config = Config.load()
    if args.off:
        config.set("wakeword.enabled", False)
        print(ok("wake word off"))
        _reload_daemon()
        return 0

    phrase = " ".join(args.phrase or []).strip()
    if not phrase:
        enabled = bool(config.get("wakeword.enabled"))
        engine = str(config.get("wakeword.engine"))
        listening = (config.get("wakeword.phrase") if engine == "whisper"
                     else config.get("wakeword.model"))
        print(f"{bold('wake word')} {ok('on') if enabled else dim('off')}")
        print(f"  phrase   {listening}")
        print(f"  engine   {engine}")
        print(dim("\n  toony wakeword \"hey toony\"     set it and switch it on"))
        print(dim("  toony wakeword --off"))
        return 0

    engine = suggest_engine(phrase)
    config.set("wakeword.enabled", True, save=False)
    config.set("wakeword.engine", engine, save=False)
    if engine == "whisper":
        config.set("wakeword.phrase", phrase, save=False)
    else:
        config.set("wakeword.model", phrase.lower().replace(" ", "_"), save=False)
    config.save()

    print(ok(f"listening for {phrase!r} using the {engine} engine"))
    if engine == "whisper":
        print(dim("  It transcribes short bursts of speech to spot the phrase, so "
                  "it costs\n  a little CPU and needs faster-whisper installed. "
                  "Any phrase works."))
        print(dim("  Too jumpy or too deaf? toony config set wakeword.similarity "
                  "0.8 / 0.65"))
    else:
        print(dim("  A trained openWakeWord model — cheap and accurate."))
    _reload_daemon()
    return 0


# ----------------------------------------------------------------- the window
def cmd_gui(args) -> int:
    from . import ui

    if not ui.available():
        print(bad("The Toony window needs PySide6."))
        print(dim("  pip install 'toony[gui]'"))
        print(dim("  or on Fedora: sudo dnf install python3-pyside6"))
        return 1
    hidden = True if args.hidden else (False if args.show else None)
    return ui.run(start_hidden=hidden)


# --------------------------------------------------------------- conversations
def cmd_conversations(args) -> int:
    from . import ipc
    from .history import store_for

    reply = ipc.send("conversations", timeout=15, limit=args.limit)
    if reply.get("ok"):
        rows, current = reply["conversations"], reply.get("current", "")
    elif _offline(reply):
        config = Config.load()
        rows, current = store_for(config).list(args.limit), ""
    else:
        print(bad(str(reply.get("error"))), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(dim("No conversations yet."))
        return 0
    import datetime

    for row in rows:
        when = datetime.datetime.fromtimestamp(row["updated"]).strftime("%d %b %H:%M")
        marker = ok("*") if row["id"] == current else " "
        print(f"{marker} {dim(row['id'])}  {when}  "
              f"{row['turns']:>3} turns  {row['title']}")
    if current:
        print(dim("\n* is the conversation Toony is in now."))
    return 0


def cmd_new(args) -> int:
    from . import ipc

    reply = ipc.send("conversation", timeout=15, action="new",
                     title=" ".join(args.title or []))
    if reply.get("ok"):
        print(ok(f"started a new conversation ({reply['id']})"))
        return 0
    if _offline(reply):
        print(dim("Toony is not running; the next question starts fresh anyway."))
        return 0
    print(bad(str(reply.get("error"))), file=sys.stderr)
    return 1


def cmd_remind(args) -> int:
    """Fired by a systemd timer that `set_timer` created."""
    from . import ipc
    from .tools.proc import which

    text = " ".join(args.text).strip() or "Your timer is up."
    if which("notify-send"):
        subprocess.call(["notify-send", "--app-name=Toony", "--icon=toony",
                         "--urgency=normal", DISPLAY_NAME, text])
    reply = ipc.send("say", text=text, timeout=60)
    if not reply.get("ok") and _offline(reply):
        _speak_locally(Config.load(), text)
    return 0


# ------------------------------------------------------------ administrator
_SUDOERS_HINT = """\
Toony only ever runs commands as root through `sudo -n`, which never prompts.
For that to work your user needs passwordless sudo for the commands you allow.

The narrow way — allow just what Toony's list holds:

  sudo tee /etc/sudoers.d/toony >/dev/null <<'EOF'
  {user} ALL=(root) NOPASSWD: /usr/bin/journalctl, /usr/bin/dnf, /usr/bin/systemctl
  EOF
  sudo chmod 440 /etc/sudoers.d/toony

Then check it with: toony sudo status
"""


def cmd_sudo(args) -> int:
    import getpass

    from .tools.proc import sudo_ready

    config = Config.load()
    action = args.action

    if action == "status":
        enabled = bool(config.get("tools.sudo.enabled", False))
        ready = sudo_ready()
        print(f"{bold('administrator access')} "
              f"{ok('on') if enabled else dim('off')}")
        print(f"  passwordless sudo   "
              f"{ok('working') if ready else bad('not set up')}")
        print(f"  allowlist ({len(config.get('tools.sudo.allowlist', []))} entries)")
        for entry in config.get("tools.sudo.allowlist", []):
            print(dim(f"    {entry}"))
        if enabled and not ready:
            print()
            print(_SUDOERS_HINT.format(user=getpass.getuser()))
        return 0 if (not enabled or ready) else 1

    if action == "enable":
        config.set("tools.sudo.enabled", True)
        print(ok("administrator access is on"))
        if not sudo_ready():
            print(warn("but passwordless sudo is not set up yet:"))
            print()
            print(_SUDOERS_HINT.format(user=getpass.getuser()))
        _reload_daemon()
        return 0

    if action == "disable":
        config.set("tools.sudo.enabled", False)
        print(ok("administrator access is off"))
        _reload_daemon()
        return 0

    if action == "allow":
        if not args.command:
            print(bad("give a command prefix, e.g. toony sudo allow \"dmesg\""),
                  file=sys.stderr)
            return 2
        entry = " ".join(args.command).strip()
        allowlist = list(config.get("tools.sudo.allowlist", []))
        if entry in allowlist:
            print(dim(f"{entry!r} is already allowed"))
            return 0
        allowlist.append(entry)
        config.set("tools.sudo.allowlist", allowlist)
        print(ok(f"allowed: {entry}"))
        _reload_daemon()
        return 0

    if action == "forbid":
        entry = " ".join(args.command or []).strip()
        allowlist = [e for e in config.get("tools.sudo.allowlist", []) if e != entry]
        config.set("tools.sudo.allowlist", allowlist)
        print(ok(f"removed: {entry}"))
        _reload_daemon()
        return 0

    print(bad(f"unknown action {action}"), file=sys.stderr)
    return 2


def _reload_daemon() -> None:
    from . import ipc

    if ipc.is_running():
        ipc.send("reload", timeout=60)


def cmd_logs(args) -> int:
    if args.follow and shutil.which("journalctl"):
        return subprocess.call(["journalctl", "--user", "-u", "toony.service",
                                "-f", "-n", str(args.lines)])
    if LOG_FILE.exists():
        return subprocess.call(["tail", f"-n{args.lines}"] +
                               (["-f"] if args.follow else []) + [str(LOG_FILE)])
    print(f"no log file at {LOG_FILE}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toony",
        description=f"{DISPLAY_NAME} — a voice assistant for the Linux desktop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            getting started
              toony install                 install the service and the hotkey
              toony doctor                  check what is missing
              toony voices install en_US-amy-medium
              toony ask "what time is it"   talk to it without the microphone
              toony listen                  the push-to-talk trigger itself

            configuring
              toony use hybrid              local ears and voice, cloud brain
              toony use local               everything on this laptop
              toony personality spicy       let it be funny about it
              toony wakeword "hey toony"    set the wake phrase
              toony config list brain       show the brain settings
              toony config set brain.provider claude
              toony config set brain.claude.api_key_env ANTHROPIC_API_KEY
              toony config set wakeword.enabled true
              toony gui                     open the window (settings live there too)

            when something is not working
              toony doctor                  check the whole stack
              toony shortcut --status       why Meta+Space is not firing
              toony sudo status             administrator access
        """))
    parser.add_argument("--version", action="version",
                        version=f"{DISPLAY_NAME} {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the assistant in the foreground")
    run_parser.set_defaults(func=cmd_run)

    listen_parser = subparsers.add_parser(
        "listen", help="start or stop listening (bind this to a hotkey)")
    listen_parser.add_argument("--edge", choices=["press", "release"],
                               default="press", help="for hold-to-talk mode")
    listen_parser.set_defaults(func=cmd_listen)

    ask_parser = subparsers.add_parser("ask", help="ask a question as text")
    ask_parser.add_argument("text", nargs="+")
    ask_parser.add_argument("--new", action="store_true",
                            help="start a fresh conversation first")
    ask_parser.add_argument("-q", "--quiet", action="store_true",
                            help="print the answer without speaking it")
    ask_parser.set_defaults(func=cmd_ask)

    say_parser = subparsers.add_parser("say", help="speak some text")
    say_parser.add_argument("text", nargs="+")
    say_parser.set_defaults(func=cmd_say)

    status_parser = subparsers.add_parser("status", help="what the daemon is doing")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=cmd_status)

    for name, help_text in (("cancel", "stop listening or speaking"),
                            ("reset", "forget the current conversation"),
                            ("reload", "re-read the configuration"),
                            ("stop", "shut the daemon down")):
        simple = subparsers.add_parser(name, help=help_text)
        simple.set_defaults(func=cmd_simple("quit" if name == "stop" else name))

    config_parser = subparsers.add_parser("config", help="read and change settings")
    config_parser.add_argument(
        "action", choices=["list", "get", "set", "unset", "edit", "path", "reset",
                           "init"])
    config_parser.add_argument("key", nargs="?")
    config_parser.add_argument("value", nargs="?")
    config_parser.set_defaults(func=cmd_config)

    devices_parser = subparsers.add_parser("devices", help="list audio devices")
    devices_parser.set_defaults(func=cmd_devices)

    tools_parser = subparsers.add_parser("tools", help="list the tools Toony can use")
    tools_parser.add_argument("--risk", choices=["safe", "sensitive", "dangerous"],
                              help="only show tools in this class")
    tools_parser.set_defaults(func=cmd_tools)

    voices_parser = subparsers.add_parser("voices", help="manage Piper voices")
    voices_parser.add_argument("action", choices=["list", "install"], nargs="?",
                               default="list")
    voices_parser.add_argument("name", nargs="?")
    voices_parser.set_defaults(func=cmd_voices)

    transcribe_parser = subparsers.add_parser("transcribe",
                                              help="transcribe a WAV file")
    transcribe_parser.add_argument("file")
    transcribe_parser.set_defaults(func=cmd_transcribe)

    doctor_parser = subparsers.add_parser("doctor", help="check the installation")
    doctor_parser.set_defaults(func=cmd_doctor)

    install_parser = subparsers.add_parser(
        "install", help="install the user service and the push-to-talk hotkey")
    install_parser.add_argument("--no-start", action="store_true")
    install_parser.add_argument("--no-shortcut", action="store_true")
    install_parser.add_argument("--no-autostart", action="store_true",
                                help="do not start the tray window at login")
    install_parser.set_defaults(func=cmd_install)

    shortcut_parser = subparsers.add_parser(
        "shortcut", help="(re)bind the push-to-talk key, or diagnose it")
    shortcut_parser.add_argument("keys", nargs="?", help="e.g. Meta+Space")
    shortcut_parser.add_argument("--status", action="store_true",
                                 help="explain why the hotkey does or does not work")
    shortcut_parser.add_argument("--clear", action="store_true",
                                 help="remove the binding")
    shortcut_parser.set_defaults(func=cmd_shortcut)

    use_parser = subparsers.add_parser(
        "use", help="switch the whole stack between local and cloud")
    use_parser.add_argument("preset", nargs="?",
                            choices=sorted(PRESETS),
                            help="omit to show what is in use now")
    use_parser.add_argument("--model", help="also set that provider's model")
    use_parser.set_defaults(func=cmd_use)

    personality_parser = subparsers.add_parser(
        "personality", help="how Toony talks to you")
    personality_parser.add_argument("style", nargs="?",
                                    help="plain | friendly | spicy | custom")
    personality_parser.set_defaults(func=cmd_personality)

    wakeword_parser = subparsers.add_parser(
        "wakeword", aliases=["wake"], help="set the wake phrase, e.g. 'hey toony'")
    wakeword_parser.add_argument("phrase", nargs="*")
    wakeword_parser.add_argument("--off", action="store_true")
    wakeword_parser.set_defaults(func=cmd_wakeword)

    gui_parser = subparsers.add_parser("gui", help="open the Toony window")
    gui_parser.add_argument("--hidden", action="store_true",
                            help="start in the tray only")
    gui_parser.add_argument("--show", action="store_true",
                            help="start with the window open")
    gui_parser.set_defaults(func=cmd_gui)

    conversations_parser = subparsers.add_parser(
        "conversations", aliases=["convos"], help="list saved conversations")
    conversations_parser.add_argument("-n", "--limit", type=int, default=20)
    conversations_parser.add_argument("--json", action="store_true")
    conversations_parser.set_defaults(func=cmd_conversations)

    new_parser = subparsers.add_parser("new", help="start a fresh conversation")
    new_parser.add_argument("title", nargs="*", help="optional name")
    new_parser.set_defaults(func=cmd_new)

    remind_parser = subparsers.add_parser(
        "remind", help="deliver a reminder (used by the timers Toony sets)")
    remind_parser.add_argument("text", nargs="+")
    remind_parser.set_defaults(func=cmd_remind)

    sudo_parser = subparsers.add_parser(
        "sudo", help="grant, list or revoke administrator access")
    sudo_parser.add_argument("action",
                             choices=["status", "enable", "disable",
                                      "allow", "forbid"],
                             nargs="?", default="status")
    sudo_parser.add_argument("command", nargs="*",
                             help="command prefix for allow/forbid")
    sudo_parser.set_defaults(func=cmd_sudo)

    uninstall_parser = subparsers.add_parser("uninstall",
                                             help="remove the service and hotkey")
    uninstall_parser.set_defaults(func=cmd_uninstall)

    logs_parser = subparsers.add_parser("logs", help="show the log")
    logs_parser.add_argument("-f", "--follow", action="store_true")
    logs_parser.add_argument("-n", "--lines", type=int, default=50)
    logs_parser.set_defaults(func=cmd_logs)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
