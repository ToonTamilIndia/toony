"""Turning written text into text worth listening to.

A model writing for a screen produces things a speech synthesiser reads out
letter by letter: `/home/you/Projects/toony/app.py` becomes "slash you slash
projects slash toony slash app dot p y", which is unbearable. Everything here
runs between the model and the voice only — the window still shows the full
text, links, paths, code and all.
"""

from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]{1,60})`")
_MARKDOWN = re.compile(r"(\*\*|__|\*|_|~~|^#{1,6}\s+|^\s*[-*+]\s+|^\s*\d+\.\s+)",
                       re.MULTILINE)
_URL = re.compile(r"https?://(?:www\.)?([^\s/]+)(/\S*)?")
_BARE_DOMAIN = re.compile(r"\b(?:www\.)([^\s/]+\.[a-z]{2,})(/\S*)?")
# A path with at least one separator, so "3/4" and dates are left alone. The
# surrounding article and noun are swallowed too, or "the ~/Documents folder"
# comes back as "the the Documents folder folder".
_PATH = re.compile(
    r"(?:\b(?:the|your)\s+)?"
    r"(?<![\w.])(?:~|/|\.{1,2}/)[\w.@+-]*(?:/[\w.@+-]+)+/?"
    r"(?:\s+(?:folder|directory|dir|file|script))?")
# A relative path: it must end in an extension, so "16/9" and "12/08/2026"
# are left alone.
_RELATIVE_PATH = re.compile(
    r"(?:\b(?:the|your)\s+)?"
    r"\b[\w@+-]+(?:/[\w.@+-]+)+\.[A-Za-z]{1,6}\b"
    r"(?:\s+(?:file|script))?")
# A bare home directory with nothing below it.
_HOME = re.compile(r"(?:\b(?:the|your)\s+)?(?<![\w.])(?:/home/[\w.-]+|~)/?"
                   r"(?:\s+(?:folder|directory|dir))?")
_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿️]")
_MULTISPACE = re.compile(r"[ \t]{2,}")

# Read aloud, these are words rather than symbols.
_SYMBOLS = {
    "&": " and ", "@": " at ", "%": " percent", "#": " number ",
    "=>": " becomes ", "->": " to ", "…": ". ", "—": ", ", "–": ", ",
    " ": " ",
}

_EXTENSIONS = {
    ".py": "python file", ".js": "javascript file", ".ts": "typescript file",
    ".rs": "rust file", ".go": "go file", ".c": "C file", ".h": "header file",
    ".cpp": "C++ file", ".java": "java file", ".sh": "shell script",
    ".md": "markdown file", ".txt": "text file", ".json": "json file",
    ".toml": "settings file", ".yaml": "yaml file", ".yml": "yaml file",
    ".html": "html file", ".css": "stylesheet", ".log": "log file",
    ".png": "image", ".jpg": "image", ".pdf": "P D F",
}


_ARTICLE = re.compile(r"^(?:the|your)\s+", re.IGNORECASE)
_NOUN = re.compile(r"\s+(?:folder|directory|dir|file|script)$", re.IGNORECASE)


def _strip_words(text: str) -> str:
    r"""Drop the article, the noun, and the sentence's own full stop.

    `[\w.@+-]` has to allow dots for extensions, so a path at the end of a
    sentence arrives as "app.py." and stops looking like a Python file.
    """
    cleaned = _NOUN.sub("", _ARTICLE.sub("", text.strip()))
    return cleaned.rstrip(".,;:!?") or cleaned


def say_path(path: str) -> str:
    """Name a path the way a person would say it out loud.

    Nobody reads a path aloud in full. They name the thing at the end of it and
    trust you to know where it lives.
    """
    cleaned = path.rstrip("/")
    if not cleaned or cleaned in ("~", "/"):
        return "your home folder" if cleaned == "~" else "the root folder"
    name = cleaned.rsplit("/", 1)[-1]
    if not name:
        return "that folder"
    if re.fullmatch(r"/home/[\w.-]+", cleaned):
        return "your home folder"
    for suffix, description in _EXTENSIONS.items():
        if name.lower().endswith(suffix):
            return f"{name[: -len(suffix)]}, the {description}"
    if "." in name and not name.startswith("."):
        return name
    return f"the {name} folder"


def say_url(match: re.Match) -> str:
    host = match.group(1).rstrip(".")
    rest = (match.group(2) or "").strip("/")
    tail = rest.split("/")[-1] if rest else ""
    host_spoken = host.replace(".", " dot ")
    if tail and len(tail) < 30:
        return f"{host_spoken}, {tail.replace('-', ' ').replace('_', ' ')}"
    return host_spoken


def speakable(text: str, keep_code: bool = False) -> str:
    """Rewrite one piece of text so a synthesiser reads it the way you'd say it."""
    if not text:
        return ""

    out = _CODE_FENCE.sub(" " if keep_code else " I have put the code on screen. ",
                          text)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _URL.sub(say_url, out)
    out = _BARE_DOMAIN.sub(lambda m: m.group(1).replace(".", " dot "), out)
    # Longest first: a full path must be matched before the /home prefix in it.
    out = _PATH.sub(lambda m: " " + say_path(_strip_words(m.group(0))) + " ", out)
    out = _RELATIVE_PATH.sub(lambda m: " " + say_path(_strip_words(m.group(0)))
                             + " ", out)
    out = _HOME.sub(" your home folder ", out)
    out = _MARKDOWN.sub(" ", out)
    out = _EMOJI.sub(" ", out)
    out = _HEX.sub("an identifier", out)
    for symbol, spoken in _SYMBOLS.items():
        out = out.replace(symbol, spoken)

    out = out.replace("\n", " ")
    out = _MULTISPACE.sub(" ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    out = re.sub(r"([,.!?;:]){2,}", r"\1", out)
    return out.strip()


def clip_for_speech(text: str, limit: int) -> tuple[str, bool]:
    """Cut a reply to something worth listening to. Returns (spoken, was_cut).

    A model that produces four paragraphs has produced a document, not an
    answer. Speaking whole sentences up to the limit is far better than
    stopping mid-word, and the window still shows all of it.
    """
    if limit <= 0 or len(text) <= limit:
        return text, False
    kept: list[str] = []
    used = 0
    for sentence in re.findall(r"[^.!?]+[.!?]*", text):
        if used + len(sentence) > limit:
            break
        kept.append(sentence)
        used += len(sentence)
    if kept:
        return "".join(kept).strip(), True
    # One sentence longer than the whole budget: cut it at a word boundary.
    head = text[:limit]
    return (head.rsplit(" ", 1)[0] if " " in head else head).strip(), True
