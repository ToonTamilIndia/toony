"""Working on code by voice.

Everything here is confined to one workspace directory (``tools.code.root``,
``~/Projects`` by default). A path is resolved before it is used and refused if
it lands outside, which is what stops "read my config" from turning into "read
my SSH key" when the model guesses a path.

Reading is safe and needs no permission. Writing and running are ``dangerous``,
so with the shipped policy they are refused until you deliberately allow them.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .proc import CommandError, any_of, run, which
from .registry import ToolContext, tool

# Never worth reading, and enormous.
_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "target",
         "dist", "build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache",
         ".idea", ".tox", "vendor", ".gradle", "Pods"}
_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".xz",
           ".so", ".o", ".a", ".bin", ".onnx", ".pt", ".pyc", ".woff", ".woff2",
           ".mp3", ".mp4", ".wav", ".ico", ".jar", ".class", ".wasm"}

# Commands that may be run inside a project. Prefix-matched on the whole line;
# nothing outside this list is attempted, whatever the model asks for.
_DEFAULT_COMMANDS = [
    "pytest", "python -m pytest", "python -m unittest", "python -c",
    "npm test", "npm run", "npm ci", "yarn test", "pnpm test",
    "cargo test", "cargo check", "cargo build", "cargo clippy",
    "go test", "go build", "go vet",
    "make", "ruff", "ruff check", "mypy", "eslint", "tsc --noEmit",
    "git status", "git diff", "git log", "git branch", "git show",
]


def workspace(config) -> Path:
    root = str(config.get("tools.code.root", "~/Projects") if config else "~/Projects")
    return Path(root).expanduser().resolve()


def resolve_in_workspace(config, path: str) -> Path:
    """Turn a model-supplied path into a real one inside the workspace.

    Accepts a project name, a path relative to the workspace, or an absolute
    path — and refuses anything that resolves outside it, symlinks included.
    """
    root = workspace(config)
    if not path or path in (".", "./"):
        return root
    candidate = Path(path).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise CommandError(
            f"{path} is outside your workspace ({root}). "
            "Move it there, or change tools.code.root.")
    return resolved


def _relative(config, path: Path) -> str:
    try:
        return str(path.relative_to(workspace(config)))
    except ValueError:
        return str(path)


def _readable(path: Path) -> bool:
    return path.suffix.lower() not in _BINARY


# ----------------------------------------------------------------- projects
@tool(description="List the code projects in the user's workspace, newest "
                  "first. Use this when they mention 'my project' without "
                  "naming one.")
def list_projects(ctx: ToolContext, limit: int = 10) -> str:
    root = workspace(ctx.config)
    if not root.is_dir():
        return (f"There is no workspace at {root}. "
                "Point Toony at yours with: toony config set tools.code.root ~/code")
    projects = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith(".") or child.name in _SKIP:
            continue
        kind = "git repo" if (child / ".git").exists() else "folder"
        projects.append((child.stat().st_mtime, child.name, kind))
    if not projects:
        return f"Your workspace at {root} is empty."
    projects.sort(reverse=True)
    limit = max(1, min(30, int(limit or 10)))
    return f"{len(projects)} projects, most recently touched first: " + ", ".join(
        f"{name} ({kind})" for _, name, kind in projects[:limit])


@tool(description="Describe one project: its layout, language, and whether it "
                  "has uncommitted changes. Use this before answering "
                  "questions about a project you have not looked at yet.",
      params={"project": {"type": "string",
                          "description": "Project name or path. Empty means "
                                         "the most recently touched one."}})
def describe_project(ctx: ToolContext, project: str = "") -> str:
    path = _project_root(ctx, project)
    if path is None:
        return "I could not work out which project you mean."

    parts = [f"{path.name} at {_relative(ctx.config, path)}."]
    counts: dict[str, int] = {}
    total = 0
    for file in _walk(path, limit=4000):
        suffix = file.suffix.lower()
        if suffix:
            counts[suffix] = counts.get(suffix, 0) + 1
        total += 1
    if counts:
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        parts.append(f"{total} files, mostly "
                     + ", ".join(f"{n} {s}" for s, n in top) + ".")
    for marker, description in (("pyproject.toml", "a Python project"),
                                ("package.json", "a Node project"),
                                ("Cargo.toml", "a Rust project"),
                                ("go.mod", "a Go project"),
                                ("CMakeLists.txt", "a CMake project"),
                                ("pom.xml", "a Maven project")):
        if (path / marker).exists():
            parts.append(f"It is {description}.")
            break
    if (path / ".git").exists():
        parts.append(_git(ctx, path, ["status", "--short", "--branch"], summary=True))
    readme = next((p for p in path.glob("README*") if p.is_file()), None)
    if readme and _readable(readme):
        first = readme.read_text(encoding="utf-8", errors="replace")[:300]
        parts.append("The readme starts: " + re.sub(r"\s+", " ", first).strip())
    return " ".join(parts)


def _project_root(ctx: ToolContext, project: str) -> Path | None:
    if project:
        path = resolve_in_workspace(ctx.config, project)
        return path if path.is_dir() else path.parent
    root = workspace(ctx.config)
    if not root.is_dir():
        return None
    candidates = [c for c in root.iterdir()
                  if c.is_dir() and not c.name.startswith(".")]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.stat().st_mtime)


def _walk(root: Path, limit: int = 2000):
    """Every interesting file under a project, skipping the noise."""
    count = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            path = Path(base) / name
            if not _readable(path):
                continue
            yield path
            count += 1
            if count >= limit:
                return


# -------------------------------------------------------------------- files
@tool(description="Show what is in a project folder: its files and "
                  "subfolders. Use this to find your way around before "
                  "reading anything.",
      params={"path": {"type": "string", "description": "Project name or "
                                                        "folder inside one."}})
def list_files(ctx: ToolContext, path: str = "") -> str:
    target = resolve_in_workspace(ctx.config, path)
    if target.is_file():
        return f"{target.name} is a file, not a folder."
    if not target.is_dir():
        return f"There is nothing at {_relative(ctx.config, target)}."
    folders, files = [], []
    for child in sorted(target.iterdir()):
        if child.name in _SKIP or child.name.startswith("."):
            continue
        (folders if child.is_dir() else files).append(child.name)
    lines = []
    if folders:
        lines.append("folders: " + ", ".join(folders[:30]))
    if files:
        lines.append("files: " + ", ".join(files[:40]))
    return "\n".join(lines) or "That folder is empty."


@tool(description="Read a source file, or part of one. Always read a file "
                  "before saying what is in it or changing it.",
      params={"path": {"type": "string",
                       "description": "Path inside the workspace, e.g. "
                                      "'toony/app.py' or 'myproject/src/main.rs'."},
              "start": {"type": "integer", "description": "First line, 1-based."},
              "end": {"type": "integer", "description": "Last line."}},
      required=["path"])
def read_code(ctx: ToolContext, path: str, start: int = 0, end: int = 0) -> str:
    target = resolve_in_workspace(ctx.config, path)
    if not target.is_file():
        found = _find_file(ctx, path)
        if found is None:
            return f"There is no file at {path}."
        target = found
    if not _readable(target):
        return f"{target.name} is a binary file."
    max_bytes = int(ctx.config.get("tools.code.max_read_bytes", 60000)
                    if ctx.config else 60000)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")[:max_bytes]
    except OSError as exc:
        raise CommandError(str(exc)) from exc

    lines = text.splitlines()
    first = max(1, int(start or 1))
    last = min(len(lines), int(end) if end else len(lines))
    if first > len(lines):
        return f"{target.name} only has {len(lines)} lines."
    window = lines[first - 1:last]
    header = f"{_relative(ctx.config, target)}, lines {first} to {last} of {len(lines)}:"
    numbered = "\n".join(f"{first + i:>5}  {line}" for i, line in enumerate(window))
    return f"{header}\n{numbered}"


def _find_file(ctx: ToolContext, name: str) -> Path | None:
    """The model often says a bare filename. Look for it rather than failing."""
    wanted = Path(name).name.lower()
    for candidate in _walk(workspace(ctx.config), limit=4000):
        if candidate.name.lower() == wanted:
            return candidate
    return None


@tool(description="Search the code in the workspace for a word, symbol or "
                  "phrase, and report where it appears.",
      params={"query": {"type": "string"},
              "path": {"type": "string", "description": "Limit to one project."},
              "limit": {"type": "integer"}},
      required=["query"])
def search_code(ctx: ToolContext, query: str, path: str = "", limit: int = 15) -> str:
    target = resolve_in_workspace(ctx.config, path)
    limit = max(1, min(60, int(limit or 15)))
    searcher = any_of("rg", "grep")
    if not searcher:
        return "Neither ripgrep nor grep is installed."
    if searcher.endswith("rg"):
        argv = ["rg", "--line-number", "--no-heading", "--color=never",
                "--max-count", "3", "-m", str(limit), "--", query, str(target)]
    else:
        argv = ["grep", "-rniI", "--line-number",
                *(f"--exclude-dir={d}" for d in sorted(_SKIP)),
                "-m", "3", "-e", query, str(target)]
    text = run(argv, timeout=45, check=False)
    hits = [line for line in text.splitlines() if line.strip()][:limit]
    if not hits:
        return f"No code in the workspace mentions {query!r}."
    root = str(workspace(ctx.config)) + "/"
    return (f"{len(hits)} matches for {query!r}:\n"
            + "\n".join(h.replace(root, "") for h in hits))


@tool(description="Create or overwrite a file in the workspace. Only use this "
                  "when the user has clearly asked for the change.",
      risk="dangerous",
      params={"path": {"type": "string"},
              "content": {"type": "string", "description": "The whole file."}},
      required=["path", "content"])
def write_code(ctx: ToolContext, path: str, content: str) -> str:
    target = resolve_in_workspace(ctx.config, path)
    existed = target.is_file()
    if existed:
        backup = target.with_suffix(target.suffix + ".toony.bak")
        backup.write_bytes(target.read_bytes())   # never lose the previous version
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    lines = content.count("\n") + 1
    return (f"{'Rewrote' if existed else 'Created'} "
            f"{_relative(ctx.config, target)}, {lines} lines."
            + (" The previous version is saved alongside it." if existed else ""))


@tool(description="Replace an exact piece of text in a file. Use this for a "
                  "small edit instead of rewriting the whole file.",
      risk="dangerous",
      params={"path": {"type": "string"},
              "find": {"type": "string", "description": "Exact text to replace."},
              "replace": {"type": "string"}},
      required=["path", "find", "replace"])
def edit_code(ctx: ToolContext, path: str, find: str, replace: str) -> str:
    target = resolve_in_workspace(ctx.config, path)
    if not target.is_file():
        return f"There is no file at {path}."
    text = target.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(find)
    if occurrences == 0:
        return "That text is not in the file, so I changed nothing."
    if occurrences > 1:
        return (f"That text appears {occurrences} times. Give me more "
                "surrounding lines so the edit is unambiguous.")
    target.with_suffix(target.suffix + ".toony.bak").write_bytes(target.read_bytes())
    target.write_text(text.replace(find, replace), encoding="utf-8")
    return f"Edited {_relative(ctx.config, target)}."


# ---------------------------------------------------------------------- git
def _git(ctx: ToolContext, path: Path, args: list[str], summary: bool = False) -> str:
    if not which("git"):
        return "git is not installed."
    try:
        text = run(["git", "-C", str(path), *args], timeout=30, check=False)
    except CommandError as exc:
        return str(exc)
    if not summary:
        return text
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return "The working tree is clean."
    return f"{len(lines) - 1} files have uncommitted changes."


@tool(description="Report git status for a project: the branch and what has "
                  "changed but is not committed.",
      params={"project": {"type": "string"}}, requires=("git",))
def git_status(ctx: ToolContext, project: str = "") -> str:
    path = _project_root(ctx, project)
    if path is None:
        return "I could not work out which project you mean."
    text = _git(ctx, path, ["status", "--short", "--branch"])
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return f"{path.name} is not a git repository."
    branch = lines[0].lstrip("# ").strip()
    changed = lines[1:]
    if not changed:
        return f"{path.name} is on {branch} with a clean working tree."
    return (f"{path.name} is on {branch} with {len(changed)} changed files: "
            + ", ".join(line[3:] for line in changed[:12]))


@tool(description="Show what has changed in a project but is not committed.",
      params={"project": {"type": "string"},
              "path": {"type": "string", "description": "One file only."}},
      requires=("git",))
def git_diff(ctx: ToolContext, project: str = "", path: str = "") -> str:
    root = _project_root(ctx, project)
    if root is None:
        return "I could not work out which project you mean."
    args = ["diff", "--stat" if not path else "--unified=3"]
    if path:
        args += ["--", path]
    text = _git(ctx, root, args)
    return text[:4000] or "Nothing has changed since the last commit."


@tool(description="Show the recent commits in a project.",
      params={"project": {"type": "string"}, "limit": {"type": "integer"}},
      requires=("git",))
def git_log(ctx: ToolContext, project: str = "", limit: int = 8) -> str:
    root = _project_root(ctx, project)
    if root is None:
        return "I could not work out which project you mean."
    limit = max(1, min(30, int(limit or 8)))
    text = _git(ctx, root, ["log", f"-{limit}", "--pretty=%h %ar by %an: %s"])
    return text[:2500] or "There are no commits yet."


@tool(description="Commit the current changes in a project with a message.",
      risk="dangerous",
      params={"message": {"type": "string"}, "project": {"type": "string"}},
      required=["message"], requires=("git",))
def git_commit(ctx: ToolContext, message: str, project: str = "") -> str:
    root = _project_root(ctx, project)
    if root is None:
        return "I could not work out which project you mean."
    run(["git", "-C", str(root), "add", "-A"], timeout=60)
    output = run(["git", "-C", str(root), "commit", "-m", message],
                 timeout=60, check=False)
    if "nothing to commit" in output:
        return "There was nothing to commit."
    return f"Committed to {root.name}: {message}"


# ------------------------------------------------------------------ running
def _allowed_commands(config) -> list[str]:
    listed = config.get("tools.code.commands", []) if config else []
    return list(listed) or _DEFAULT_COMMANDS


@tool(description="Run a build, test or lint command inside a project and "
                  "report what it said. Use this to actually check whether "
                  "something works, rather than guessing.",
      risk="dangerous",
      params={"command": {"type": "string",
                          "description": "e.g. 'pytest -q', 'cargo check', "
                                         "'npm test'."},
              "project": {"type": "string"}},
      required=["command"])
def run_in_project(ctx: ToolContext, command: str, project: str = "") -> str:
    root = _project_root(ctx, project)
    if root is None:
        return "I could not work out which project you mean."
    command = command.strip()
    if re.search(r"[|;&><`$]|\\n", command):
        return ("I only run plain commands — no pipes, redirects or "
                "substitutions. Ask for one command at a time.")
    allowed = _allowed_commands(ctx.config)
    if not any(command == entry or command.startswith(entry + " ")
               for entry in allowed):
        return (f"{command!r} is not on the list of commands I may run. "
                "Add it with: toony config set tools.code.commands "
                f"\"{', '.join(allowed[:4])}, {command.split()[0]}\"")

    timeout = float(ctx.config.get("tools.code.timeout_s", 180) if ctx.config else 180)
    try:
        proc = subprocess.run(command.split(), cwd=str(root), capture_output=True,
                              timeout=timeout)
    except FileNotFoundError:
        return f"{command.split()[0]} is not installed."
    except subprocess.TimeoutExpired:
        return f"{command} was still running after {timeout:g} seconds, so I stopped it."
    out = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", "replace").strip()
    tail = "\n".join(out.splitlines()[-40:])
    verdict = "passed" if proc.returncode == 0 else f"failed with code {proc.returncode}"
    return f"{command} {verdict} in {root.name}.\n{tail[:3000]}"
