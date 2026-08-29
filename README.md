# Toony

A voice assistant that lives on your Linux desktop. You hold a key or say a wake
word, it listens, thinks, uses tools on your machine, and answers out loud.

Every layer is swappable. The brain can be Claude, any OpenAI-compatible
endpoint, or a local model through Ollama. Speech in and out can be fully local
(Whisper + Piper on your GPU), fully cloud, or one of each. Nothing but the
microphone and speaker plumbing is fixed.

```
   hotkey ─┐
           ├─► record ─► speech-to-text ─►  BRAIN  ─► tools ─► speech ─► 🔊
"hey Toony"─┘             whisper/cloud    claude      │       piper/cloud
   window ─┘                               openai      │
                                           ollama      ▼
                                                  permission gate
                                                  (allow / ask / deny)
```

There is a window too — a tray icon that is always there, conversations you can
scroll back through, every setting editable in place, and Allow/Deny buttons so
a permission question does not have to be answered by shouting "yes" at a laptop.

## Install

```bash
git clone <this repo> toony && cd toony
./install.sh                     # local ears and voice, add --all for everything
```

That creates a virtualenv in `~/.local/share/toony/venv`, links `~/.local/bin/toony`,
downloads a Piper voice, installs a `systemd --user` service so Toony starts when
you log in, and binds **Meta+Space** to push-to-talk.

Then check it:

```bash
toony doctor
```

`doctor` prints every dependency, backend and desktop command it can find, and
tells you exactly what to install for the parts that are missing.

On Fedora KDE the optional desktop commands are:

```bash
sudo dnf install wireplumber playerctl spectacle wl-clipboard libnotify \
    brightnessctl NetworkManager-tui bluez power-profiles-daemon
```

## Local or cloud, in one command

```bash
toony use                # what is running right now
toony use local          # everything on this laptop: no key, no network
toony use hybrid         # local ears and voice, cloud brain — the fast good one
toony use cloud          # best models everywhere, billed to your key
toony use claude --model claude-opus-5
```

Each preset sets the brain, the ears, the voice and the vision model together,
then reloads the running daemon. `toony use` on its own prints the stack and
tells you if the API key it needs is missing.

## Personality

```bash
toony personality spicy
```

| | |
|---|---|
| `plain` | answers, nothing else |
| `friendly` | warm and quick, a joke when something is actually funny (default) |
| `spicy` | funny, sarcastic, teases you about your own habits |
| `custom` | whatever you put in `general.personality_prompt` |

Spicy still answers first — the joke rides along with the answer, one line, and
it drops the act entirely when something is genuinely broken. It punches at the
situation, the machine and your forty open tabs, never at anybody's identity.

## Programming assistant

```bash
toony config set general.focus coding
toony config set tools.code.root ~/Projects
```

That adds programming guidance to the prompt and points the code tools at your
workspace. Everything they touch must live under that root — a path that
resolves outside it is refused, symlinks included.

| tool | |
|---|---|
| `list_projects`, `describe_project` | what you are working on, language, git state |
| `read_code`, `list_files`, `search_code` | look before answering; a bare filename is found for you |
| `write_code`, `edit_code` | make the change — the previous version is kept alongside |
| `run_in_project` | `pytest`, `cargo check`, `npm test` — allowlisted, no pipes or redirects |
| `git_status`, `git_diff`, `git_log`, `git_commit` | the usual |

Reading is safe and never asks. Writing, editing, running and committing are
`dangerous`, so with the shipped policy they are refused until you allow them:

```bash
toony config set tools.always_ask "write_code, edit_code, run_in_project"
```

## Vision

```bash
toony config set vision.provider auto        # the default
ollama pull qwen2.5vl:7b                     # if your brain is a text-only model
```

`look_at_screen` and `read_screen_text` show the screen to a model that can
actually see. That is usually the brain — but the default local brain is
text-only, and a text-only model handed a screenshot does not say so, it invents
a confident description of nothing. So `auto` checks whether the brain can read
images and routes to `vision.model` when it cannot. `toony doctor` says which
model will get the picture and whether it can see.

## Wake word

```bash
toony wakeword "hey toony"
toony wakeword --off
```

There are two engines and the command picks the right one for your phrase.

**whisper** (the default for "hey Toony") transcribes short bursts of speech and
matches the phrase fuzzily — "hey tunie", "hey tony" and "a tooney" all count,
because that is what a two-word burst actually comes back as. Any phrase works
with nothing to train. It needs `faster-whisper` and a little CPU; silence is
never transcribed, so an idle desktop costs almost nothing.

**openwakeword** is cheaper and sharper but only knows phrases somebody has
trained a model for — `hey_jarvis`, `alexa`, `hey_mycroft`. Point
`wakeword.model` at a `.onnx` file if you train your own.

Too jumpy, or too deaf?

```bash
toony config set wakeword.similarity 0.8     # stricter
toony config set wakeword.similarity 0.65    # looser
```

## Talking to it

| | |
|---|---|
| `Meta+Space` | push to talk (press again to stop, or just stop talking) |
| `Meta+Space` while it talks | interrupt it |
| `toony ask "what time is it"` | same assistant, typed instead of spoken |
| `toony say "hello"` | make it speak |
| `toony status` | what it is doing right now |
| `toony logs -f` | follow the log |
| `toony gui` | open the window |
| `Escape`, `Ctrl+.`, the ■ button | shut it up mid-sentence |
| `toony cancel` | the same, from a terminal |

`toony ask` works whether or not the daemon is running — without it, it builds a
one-shot assistant in the foreground. Either way it continues the conversation
you were already having, so follow-up questions work.

## The window

```bash
toony gui             # or click Toony in the launcher, or the tray icon
```

It lives in the system tray and stays there. Closing it hides it; the assistant
carries on listening either way.

|  |  |
|---|---|
| avatar | your GitHub picture, fetched once and cached, cropped to a circle |
| `☰` | conversations — click one to carry on where it left off |
| `＋` / `Ctrl+N` | start a fresh conversation |
| `⚙` / `Ctrl+,` | every setting, grouped and typed |
| 🎙 / `Ctrl+L` | push to talk — and the same button stops it mid-sentence |
| `Escape` / `Ctrl+.` | stop talking now |
| Allow / Deny | answer a permission question with a click instead of your voice |

The window follows the daemon rather than driving it, so what you said into the
microphone appears in it, and a permission question raised by a spoken request
gets buttons.

Set how see-through it is in settings, or from the terminal:

```bash
toony config set ui.opacity 0.85       # 0.35 to 1.0, applied live
toony config set ui.theme dark         # auto follows Plasma
toony config set ui.accent "#7c5cff"
toony config set ui.always_on_top true
toony config set ui.start_minimised false
toony config set ui.avatar_url "https://example.com/me.png"
```

The window needs PySide6 — `./install.sh` includes it, or `pip install
'toony[gui]'`, or `sudo dnf install python3-pyside6`.

## Conversations

Conversations are files under `~/.local/share/toony/conversations`, so they
survive restarts and are the same threads the window's sidebar lists.

```bash
toony conversations             # list them, newest first
toony new                       # start a fresh one
toony ask --new "hello"         # ask in a fresh one
toony reset                     # same as `new`
```

When Toony starts it carries on the last conversation if it is recent, and
begins a new one if it is not — a two-day-old thread confuses a model more than
it helps you. Change the cut-off with `conversation.resume_window_min`.

## Choosing a brain

The default is **Ollama**, so a fresh install works offline with no API key:

```bash
ollama pull qwen3:4b
toony config set brain.provider ollama
toony config set brain.ollama.model qwen3:4b
```

**Claude** — the best tool use and the only one that reads your screen well:

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # put this in ~/.bashrc
toony config set brain.provider claude
toony config set brain.claude.model claude-opus-5
toony config set brain.claude.effort low     # low keeps voice latency down
```

**Any OpenAI-compatible endpoint** — OpenAI, Groq, OpenRouter, vLLM, llama.cpp,
LM Studio:

```bash
toony config set brain.provider openai
toony config set brain.openai.base_url https://api.groq.com/openai/v1
toony config set brain.openai.api_key_env GROQ_API_KEY
toony config set brain.openai.model llama-3.3-70b-versatile
```

Keys are read from an environment variable by default (`brain.*.api_key_env`) so
they never end up in the config file. Set `api_key` directly if you prefer.

## Choosing ears and a voice

Local is the default: `faster-whisper` for listening, Piper for speaking. On an
RTX 3050 the `small` Whisper model transcribes a sentence in well under a second.

```bash
toony config set stt.provider local
toony config set stt.local.model small        # tiny | base | small | medium | large-v3
toony config set stt.local.device auto        # auto picks CUDA when it is there

toony voices list
toony voices install en_US-amy-medium
toony config set tts.speed 1.1
```

Cloud, or a mix of the two:

```bash
toony config set stt.provider openai          # cloud ears
toony config set tts.provider openai          # cloud voice
toony config set tts.openai.voice alloy
```

`espeak` is always available as a fallback voice if Piper is not installed.

## Wake word

Push-to-talk needs no model and never mishears, so it is the default. To go
hands-free:

```bash
pip install 'toony[wake]'
toony config set wakeword.enabled true
toony config set wakeword.model hey_jarvis    # a bundled openWakeWord model
```

A real *"hey Toony"* needs a model trained for that phrase — train one with
openWakeWord's notebook, drop the `.onnx` in `~/.local/share/toony/wakeword/`,
and point `wakeword.model` at its name.

The wake word listener holds its own microphone stream and releases it while
Toony is recording or speaking, so it never triggers on its own voice.

## What it can do

`toony tools` lists every tool, whether it is available, and how risky it is.

Sixty-one tools, in eleven groups:

| group | what it covers |
|---|---|
| applications | find and launch anything installed, list what is there |
| windows | list, focus, close, switch virtual desktop |
| system | volume, mute, brightness, battery, load, memory, disk, time |
| **logs** | read the journal, and `diagnose_system` — one call that gathers failed services, errors since boot, memory and swap pressure, disk, crashes, OOM kills, temperature and kernel errors into a briefing |
| **services** | list, inspect, start/stop/restart systemd units |
| **network** | online state, Wi-Fi network and signal, scan, connect to a saved one, Bluetooth |
| **power** | suspend, hibernate, reboot, shut down, log out, power profile, night colour |
| **timers** | "remind me in ten minutes" — handed to systemd, so it survives a restart |
| **packages** | is it installed, search the repos, count pending updates, install |
| media & clipboard | play/pause/next, what is playing, read and write the clipboard, type text |
| screen, files, web, memory | screenshots and reading your screen, file search, web search, remembered facts |

Tools whose backing command is not installed are never offered to the model, so
it cannot promise something your machine cannot do.

Asking "what's going on with my system?" calls `diagnose_system` and answers
from what it actually found.

Tools whose backing command is not installed are never offered to the model, so
it cannot promise something your machine cannot do.

### The permission gate

The model never runs anything directly. It emits a structured tool call, which
passes through a gate keyed on how risky the tool is:

| risk | examples | default |
|---|---|---|
| `safe` | read the time, volume, battery, search the web | **allow** |
| `sensitive` | launch an app, read the clipboard, screenshot, open a file | **ask** |
| `dangerous` | close a window, type keystrokes, shut down, install a package | **deny** |

"ask" means Toony asks — with buttons if the window is open, out loud if it is
not. Individual tools can override their class, which is how "open Firefox" just
opens Firefox instead of asking every time:

```bash
toony config set tools.always_allow "open_application, open_url, set_timer"
toony config set tools.always_ask "write_clipboard"
toony config set tools.never "type_text"          # refused whatever the class says
```

Change the classes themselves:

```bash
toony config set tools.policy_sensitive allow
toony config set tools.policy_dangerous ask
toony config set tools.disabled "type_text, close_window"
```

Shell access is off entirely until you enable it, and even then only matches an
allowlist, with pipes, redirects and command substitution refused outright:

```bash
toony config set tools.shell.enabled true
toony config set tools.shell.allowlist "ls, df, free, uptime, systemctl status"
```

### Administrator access

Off by default. When you turn it on, Toony runs root commands only through
`sudo -n` — which never prompts — and only ones whose prefix is on its own
allowlist. Anything else is refused before sudo is even reached.

```bash
toony sudo status                        # what is allowed, and whether it works
toony sudo enable                        # prints the sudoers snippet you need
toony sudo allow "dmesg"                 # widen the list
toony sudo forbid "dnf info"             # narrow it
toony sudo disable
```

`toony sudo enable` will not silently work: it checks whether passwordless sudo
is actually set up and shows you the `/etc/sudoers.d/toony` line to add if not.
Because the daemon has no terminal, a command that would ask for a password
fails immediately rather than hanging.

## Not being talked at

Two things stop a four-paragraph answer being read out in full.

```bash
toony config set general.reply_word_target 40    # ask for shorter answers
toony config set tts.max_spoken_chars 500        # stop after this much, 0 for no cap
```

Past the cap it says "the rest is on screen" and stops. The window still shows
everything. And whatever it is mid-way through, `Escape` ends it immediately.

Written text is also rewritten before it is spoken, because a model writing for
a screen produces things a synthesiser reads out letter by letter:

| written | spoken |
|---|---|
| `/home/you/Projects/toony/app.py` | "app, the python file" |
| `https://github.com/you/toony` | "github dot com, toony" |
| a fenced code block | "I have put the code on screen" |
| `**bold**`, bullets, emoji | gone |

`toony config set tts.speakable false` turns it off.

## Configuration

One file: `~/.config/toony/config.toml`. Everything in it has a default, so it
only holds what you changed.

```bash
toony config list             # everything, with changes highlighted
toony config list audio       # just one section
toony config get brain.provider
toony config set audio.silence_ms 600
toony config edit             # open it in $EDITOR
toony config reset            # back to defaults, keeping a .bak
```

Changes are pushed to the running daemon immediately — no restart. Any setting
can also be overridden by an environment variable: `TOONY_BRAIN__PROVIDER=claude`.

Worth knowing:

| setting | what it does |
|---|---|
| `audio.silence_ms` | how long a pause ends your sentence (lower = snappier) |
| `audio.input_device` | device number or part of its name; see `toony devices` |
| `audio.vad` | `energy` (no dependency) or `webrtc` (sharper) |
| `general.reply_word_target` | how long spoken answers are allowed to be |
| `tts.stream` | speak each sentence as it is generated instead of waiting |
| `brain.max_tool_iterations` | how many tool rounds before it must answer |

## Service and hotkey

```bash
systemctl --user status toony        # or restart / stop
toony shortcut --status              # why the hotkey does or does not work
toony shortcut "Meta+Shift+Space"    # rebind push-to-talk
toony install --no-start             # reinstall the unit without starting
toony uninstall                      # remove the service, window and hotkey
```

The hotkey works by running `toony listen`, which pokes the daemon over a unix
socket in `$XDG_RUNTIME_DIR`. That indirection is deliberate: on Wayland an
application cannot grab a global hotkey for itself, but the compositor can run a
command. Any hotkey daemon works — it does not have to be KDE's.

On KDE, three things must all line up, and `toony shortcut --status` checks each
of them by name:

1. `~/.local/share/applications/toony-listen.desktop` must carry
   `X-KDE-GlobalAccel-CommandShortcut=true`. Without that line kglobalaccel
   ignores the entry and the key does nothing at all.
2. `~/.config/kglobalshortcutsrc` must bind `_launch` under
   `[services][toony-listen.desktop]`, and kglobalaccel must be restarted to
   notice, because it does not watch the file.
3. Nothing else may already own the key. KDE gives a combination to whoever
   grabbed it first, and on some layouts **Meta+Space is already taken by the
   keyboard-layout switcher** — `--status` names whatever is holding it.

## Layout

```
toony/
├── app.py            the daemon: state machine, turn loop, control commands
├── agent.py          the conversation: transcript, tool loop, history
├── safety.py         the permission gate
├── config.py         defaults, TOML persistence, dotted access
├── ipc.py            the control socket
├── cli.py            every `toony` subcommand
├── history.py        conversations on disk: save, list, reopen, prune
├── text.py           making written text worth listening to
├── ui/               the window: tray, chat, conversations, settings, avatar
├── audio/            devices, capture with endpointing, VAD, playback, wake word
├── brain/            claude · openai_compat (also Ollama) · prompts
├── stt/              local_whisper · cloud_whisper
├── tts/              piper · espeak · cloud · the sentence-streaming speaker
└── tools/            applications, system, logs, power, network, packages,
                      timers, code, media, clipboard, files, screen, desktop,
                      web, shell, memory — all on one registry
```

Audio is 16-bit PCM everywhere: it is what the VAD, the wake word model and
every speech backend take, so nothing converts on the hot path.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests cover the agent loop, the permission gate and its per-tool overrides,
config typing, audio conversion and endpointing, sentence chunking, conversation
storage, spoken-duration parsing, the sudo allowlist, shortcut matching, and the
whole control socket including the event stream and click-to-confirm — all with
fakes, so they need no microphone, no GPU and no network.

## Troubleshooting

**It does not hear me.** `toony devices`, then set `audio.input_device`. If it
cuts you off, raise `audio.silence_ms`. If it never stops, lower
`audio.energy_threshold` or switch `audio.vad` to `webrtc`.

**It answers slowly.** `brain.claude.effort low`, a smaller Whisper model, and
`tts.stream true`. Check where the time goes with `toony logs -f` — every stage
logs its duration.

**The hotkey does nothing.** `toony shortcut --status` — it checks the launcher
entry, the binding, conflicts, kglobalacceld and the daemon, and names whichever
one is wrong. The usual answer is that something else already owns Meta+Space;
pick another with `toony shortcut "Meta+Alt+Space"`.

**It says "I'm sorry, I can't assist with that."** That is a small local model
declining a request it should have answered. Toony retries once with a nudge
(`brain.retry_refusals`), but the real fix is a bigger model:
`toony config set brain.ollama.model qwen2.5:7b`, or `toony use hybrid`.

**It talks for a minute straight.** `Escape` stops it now. To stop it happening:
`toony config set tts.max_spoken_chars 500` and
`toony config set general.reply_word_target 40`.

**It reads file paths out slash by slash.** That should not happen any more —
check `tts.speakable` is still `true`.

**It describes a screen it cannot see.** Your brain is a text-only model.
`toony doctor` says so under `vision`; fix it with `ollama pull qwen2.5vl:7b`,
or `toony use hybrid`.

**"CUDAExecutionProvider is not in available provider names."** onnxruntime
without CUDA, which openWakeWord asks for regardless. Harmless — the wake-word
model is 80 milliseconds of audio at a time and runs fine on the CPU. Toony now
silences it.

**The wake word never fires.** `toony wakeword` shows the phrase and engine.
openWakeWord only knows its own trained phrases, so if you asked for "hey toony"
make sure the engine is `whisper`. Then lower `wakeword.similarity`.

**It does not remember what I just said.** Check `toony status` shows a daemon
and a conversation. `toony conversations` lists what has been saved.

**The window will not open.** `toony gui` prints why. Almost always PySide6:
`pip install 'toony[gui]'` or `sudo dnf install python3-pyside6`.

**The tray icon is missing.** `toony install` writes the autostart entry; check
`~/.config/autostart/toony-window.desktop` exists, and that the Plasma system
tray is not hiding it under the "..." arrow.

**Ollama is unreachable.** `systemctl --user status ollama`, and confirm
`brain.ollama.base_url` ends in `/v1`.
