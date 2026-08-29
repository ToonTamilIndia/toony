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
                                           openai      │
                                           ollama      ▼
                                                  permission gate
                                                  (allow / ask / deny)
```

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
sudo dnf install wireplumber playerctl spectacle wl-clipboard libnotify brightnessctl
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

`toony ask` works whether or not the daemon is running — without it, it builds a
one-shot assistant in the foreground.

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

Applications and windows, volume and brightness, media playback, the clipboard,
screenshots and reading your screen, web search, file search, remembering facts,
notifications, and — if you turn it on — a small allowlist of shell commands.

Tools whose backing command is not installed are never offered to the model, so
it cannot promise something your machine cannot do.

### The permission gate

The model never runs anything directly. It emits a structured tool call, which
passes through a gate keyed on how risky the tool is:

| risk | examples | default |
|---|---|---|
| `safe` | read the time, volume, battery, search the web | **allow** |
| `sensitive` | launch an app, read the clipboard, screenshot, open a file | **ask** |
| `dangerous` | close a window, type keystrokes, run a shell command | **deny** |

"ask" means Toony asks you out loud and waits for a yes. Change any of them:

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
toony shortcut "Meta+Shift+Space"    # rebind push-to-talk
toony install --no-start             # reinstall the unit without starting
toony uninstall                      # remove the service and hotkey
```

The hotkey works by running `toony listen`, which pokes the daemon over a unix
socket in `$XDG_RUNTIME_DIR`. That indirection is deliberate: on Wayland an
application cannot grab a global hotkey for itself, but the compositor can run a
command. Any hotkey daemon works — it does not have to be KDE's.

## Layout

```
toony/
├── app.py            the daemon: state machine, turn loop, control commands
├── agent.py          the conversation: transcript, tool loop, history
├── safety.py         the permission gate
├── config.py         defaults, TOML persistence, dotted access
├── ipc.py            the control socket
├── cli.py            every `toony` subcommand
├── audio/            devices, capture with endpointing, VAD, playback, wake word
├── brain/            claude · openai_compat (also Ollama) · prompts
├── stt/              local_whisper · cloud_whisper
├── tts/              piper · espeak · cloud · the sentence-streaming speaker
└── tools/            applications, system, media, clipboard, files, screen,
                      desktop, web, shell, memory — all on one registry
```

Audio is 16-bit PCM everywhere: it is what the VAD, the wake word model and
every speech backend take, so nothing converts on the hot path.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests cover the agent loop, the permission gate, config typing, audio
conversion and endpointing, sentence chunking, and the whole control socket —
all with fakes, so they need no microphone, no GPU and no network.

## Troubleshooting

**It does not hear me.** `toony devices`, then set `audio.input_device`. If it
cuts you off, raise `audio.silence_ms`. If it never stops, lower
`audio.energy_threshold` or switch `audio.vad` to `webrtc`.

**It answers slowly.** `brain.claude.effort low`, a smaller Whisper model, and
`tts.stream true`. Check where the time goes with `toony logs -f` — every stage
logs its duration.

**The hotkey does nothing.** Check `toony status` first. If the daemon is up,
the binding did not take: System Settings → Shortcuts, look for "Toony: talk".

**Ollama is unreachable.** `systemctl --user status ollama`, and confirm
`brain.ollama.base_url` ends in `/v1`.
