"""Software: what is installed, what has updates, and installing things.

Reading is unprivileged and cheap. Installing needs root, so it goes through
the sudo allowlist and is ``dangerous`` — the model can propose it, but nothing
happens until the user has both switched administrator access on and confirmed
the call.
"""

from __future__ import annotations

import re

from .proc import CommandError, any_of, run, sudo_enabled, sudo_run, which
from .registry import ToolContext, tool


def _manager() -> str:
    return any_of("dnf5", "dnf", "rpm-ostree", "apt", "pacman") or ""


@tool(description="Check whether a program is installed and which version.",
      params={"name": {"type": "string"}}, required=["name"])
def package_info(ctx: ToolContext, name: str) -> str:
    binary = which(name)
    lines = []
    if binary:
        lines.append(f"{name} is on the path at {binary}.")
    if which("rpm"):
        try:
            version = run(["rpm", "-q", "--qf", "%{NAME} %{VERSION}", name],
                          timeout=15)
            lines.append(f"The installed package is {version}.")
        except CommandError:
            if not binary:
                lines.append(f"No RPM package called {name} is installed.")
    if which("flatpak"):
        try:
            text = run(["flatpak", "list", "--app", "--columns=name,version"],
                       timeout=20, check=False)
            for line in text.splitlines():
                if name.lower() in line.lower():
                    lines.append(f"Installed as a Flatpak: {line.strip()}.")
                    break
        except CommandError:
            pass
    return " ".join(lines) or f"I could not find anything called {name}."


@tool(description="Search the software repositories for a package by keyword.",
      params={"query": {"type": "string"}, "limit": {"type": "integer"}},
      required=["query"])
def search_packages(ctx: ToolContext, query: str, limit: int = 8) -> str:
    manager = _manager()
    if not manager.endswith(("dnf", "dnf5")):
        return "Package search is only wired up for dnf on this machine."
    try:
        text = run([manager, "--quiet", "search", query], timeout=60, check=False)
    except CommandError as exc:
        return f"The search failed: {exc}"
    hits = []
    for line in text.splitlines():
        match = re.match(r"^([\w.+-]+)\s*:\s*(.+)$", line.strip())
        if match and not line.startswith("="):
            hits.append(f"{match.group(1)} — {match.group(2)[:70]}")
    if not hits:
        return f"Nothing in the repositories matches {query}."
    limit = max(1, min(20, int(limit or 8)))
    return f"{len(hits)} matches, the first few: " + "; ".join(hits[:limit])


@tool(description="Check how many system updates are waiting to be installed.")
def check_updates(ctx: ToolContext) -> str:
    manager = _manager()
    parts: list[str] = []
    if manager.endswith(("dnf", "dnf5")):
        try:
            if sudo_enabled(ctx.config):
                text = sudo_run(ctx.config, [manager, "--quiet", "check-update"],
                                timeout=120)
            else:
                text = run([manager, "--quiet", "check-update"], timeout=120,
                           check=False)
            names = [line.split()[0] for line in text.splitlines()
                     if line.strip() and not line.startswith(("Last metadata",
                                                              "Obsoleting", " "))
                     and len(line.split()) >= 3]
            parts.append(f"{len(names)} package updates are available."
                         if names else "The system packages are up to date.")
        except CommandError as exc:
            parts.append(f"I could not check system updates: {exc}")
    if which("flatpak"):
        try:
            text = run(["flatpak", "remote-ls", "--updates", "--columns=name"],
                       timeout=60, check=False)
            count = len([line for line in text.splitlines() if line.strip()])
            if count:
                parts.append(f"{count} Flatpak apps have updates.")
        except CommandError:
            pass
    return " ".join(parts) or "I have no way to check for updates here."


@tool(description="Install a package. This needs administrator access and "
                  "changes the system, so only do it when the user asks "
                  "directly and names the package.",
      risk="dangerous",
      params={"name": {"type": "string",
                       "description": "Exact package name, not a description."}},
      required=["name"])
def install_package(ctx: ToolContext, name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", name):
        return "That does not look like a package name, so I did not try."
    manager = _manager()
    if not manager.endswith(("dnf", "dnf5")):
        raise CommandError("only dnf installs are supported here")
    if not sudo_enabled(ctx.config):
        return ("Installing needs administrator access, which is switched off. "
                "Turn it on with: toony sudo enable")
    output = sudo_run(ctx.config, [manager, "-y", "install", name], timeout=900)
    tail = output.strip().splitlines()[-1:] or [""]
    return f"Installed {name}. {tail[0][:120]}"


@tool(description="List the largest installed packages, to find what is using "
                  "disk space.", params={"limit": {"type": "integer"}},
      requires=("rpm",))
def largest_packages(ctx: ToolContext, limit: int = 8) -> str:
    text = run(["rpm", "-qa", "--qf", "%{SIZE} %{NAME}\n"], timeout=60)
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1]))
    rows.sort(reverse=True)
    limit = max(1, min(20, int(limit or 8)))
    return "; ".join(f"{name} at {size / 1048576:.0f} megabytes"
                     for size, name in rows[:limit]) or "No packages found."
