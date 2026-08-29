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
    reply = ipc.send("ask", text=text, speak=not args.quiet, timeout=300)
    if reply.get("ok"):
        print(reply.get("reply", ""))
        return 0
    if _offline(reply):
        return _ask_locally(text, speak=not args.quiet)
    print(bad(str(reply.get("error", "failed"))), file=sys.stderr)
    return 1


def _ask_locally(text: str, speak: bool) -> int:
    """No daemon: build just enough of the assistant to answer once."""
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

        Speaker(build_tts(config), Player(config),
                stream=bool(config.get("tts.stream", True))).say(text)
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
    from .tools import REGISTRY

    config = Config.load()
    enabled = {t.name for t in REGISTRY.enabled(config)}
    colour = {"safe": ok, "sensitive": warn, "dangerous": bad}
    for tool in REGISTRY.all():
        state = ok("on ") if tool.name in enabled else dim("off")
        missing = f"  needs {', '.join(tool.missing())}" if tool.missing() else ""
        print(f"{state} {tool.name:22} {colour[tool.risk](tool.risk):<20}"
              f"{dim(tool.description.split('.')[0][:60])}{warn(missing)}")
    print(dim(f"\n{len(enabled)} of {len(REGISTRY.all())} tools available. "
              "Policies: tools.policy_safe / _sensitive / _dangerous"))
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

DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name={display}: talk
Comment=Start or stop talking to {display}
Exec={executable} listen
Icon=audio-input-microphone
Terminal=false
NoDisplay=false
Categories=Utility;
"""


def _unit_path():
    from pathlib import Path

    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "systemd" / "user" / "toony.service"


def _desktop_path():
    from pathlib import Path

    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(base) / "applications" / "toony-listen.desktop"


def _executable() -> str:
    """The absolute path to this CLI, so systemd does not depend on PATH."""
    found = shutil.which("toony")
    if found:
        return found
    return f"{sys.executable} -m toony"


def cmd_install(args) -> int:
    ensure_dirs()
    executable = _executable()

    if not CONFIG_FILE.exists():
        Config.load().save()
        print(f"wrote {CONFIG_FILE}")

    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(SERVICE_UNIT.format(display=DISPLAY_NAME, executable=executable),
                    encoding="utf-8")
    print(f"wrote {unit}")

    desktop = _desktop_path()
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(DESKTOP_ENTRY.format(display=DISPLAY_NAME,
                                            executable=executable), encoding="utf-8")
    print(f"wrote {desktop}")

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
        _install_shortcut(Config.load().get("ptt.shortcut", "Meta+Space"))

    print(dim("\nCheck everything with: toony doctor"))
    return 0


def _install_shortcut(shortcut: str) -> None:
    """Register the push-to-talk key with KDE's global shortcut daemon."""
    writer = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    if not writer:
        print(warn("kwriteconfig6 not found — bind the shortcut yourself:"))
        print(dim(f"  System Settings > Shortcuts > add '{_executable()} listen' "
                  f"and assign {shortcut}"))
        return
    subprocess.call([writer, "--file", "kglobalshortcutsrc",
                     "--group", "services", "--group", "toony-listen.desktop",
                     "--key", "_launch", f"{shortcut},none,{DISPLAY_NAME}: talk"])
    for restarter in (["systemctl", "--user", "restart", "plasma-kglobalaccel.service"],
                      ["kquitapp6", "kglobalaccel"]):
        if shutil.which(restarter[0]):
            subprocess.call(restarter, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
            break
    print(ok(f"bound {shortcut} to push-to-talk"))
    print(dim("If it does not respond, check System Settings > Shortcuts."))


def cmd_shortcut(args) -> int:
    config = Config.load()
    shortcut = args.keys or str(config.get("ptt.shortcut", "Meta+Space"))
    if args.keys:
        config.set("ptt.shortcut", shortcut)
    _install_shortcut(shortcut)
    return 0


def cmd_uninstall(args) -> int:
    if shutil.which("systemctl"):
        subprocess.call(["systemctl", "--user", "disable", "--now", "toony.service"])
    for path in (_unit_path(), _desktop_path()):
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    if shutil.which("systemctl"):
        subprocess.call(["systemctl", "--user", "daemon-reload"])
    print(dim(f"Configuration in {CONFIG_FILE.parent} was left alone."))
    return 0


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
              toony config list brain       show the brain settings
              toony config set brain.provider claude
              toony config set brain.claude.api_key_env ANTHROPIC_API_KEY
              toony config set wakeword.enabled true
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
    install_parser.set_defaults(func=cmd_install)

    shortcut_parser = subparsers.add_parser("shortcut",
                                            help="(re)bind the push-to-talk key")
    shortcut_parser.add_argument("keys", nargs="?", help="e.g. Meta+Space")
    shortcut_parser.set_defaults(func=cmd_shortcut)

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
