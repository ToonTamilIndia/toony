"""Reading the journal, and turning it into an answer to "what's going on?".

Two very different jobs live here. :func:`read_system_logs` is a thin window
onto journalctl for when the user knows what they are looking for. :func:`
diagnose_system` is the one people actually ask for out loud — it gathers the
handful of signals that explain almost every "my laptop is being weird" and
hands the model a short briefing instead of a thousand log lines.
"""

from __future__ import annotations

import re
from collections import Counter

from .proc import CommandError, run, sudo_enabled, sudo_run, which
from .registry import ToolContext, tool

_PRIORITY = {"emergency": "0", "alert": "1", "critical": "2", "error": "3",
             "warning": "4", "notice": "5", "info": "6", "debug": "7"}

# Journal noise that is loud, harmless, and would otherwise dominate a summary.
_BORING = re.compile(
    r"(gkr-pam|pam_kwallet|Failed to connect to bus|GLib-GObject|"
    r"Unable to autolaunch|gnome-keyring|dbus-daemon.*Activating|"
    r"kwin_.*: Xwayland|systemd-coredump.*Process \d+ .* dumped core)",
    re.IGNORECASE)

# journalctl prints this instead of staying quiet when a query matches nothing.
_EMPTY = re.compile(r"^\s*--\s*No entries\s*--\s*$", re.IGNORECASE)


def _journal(args: list[str], config=None, timeout: float = 20.0) -> str:
    """Read the journal, escalating to sudo only if the user allowed it.

    A plain user sees their own journal on most distributions but not the
    system's, so the unprivileged read is tried first and only its failure —
    or an empty result on a system-wide query — is worth escalating.
    """
    if not which("journalctl"):
        raise CommandError("journalctl is not available on this system")
    argv = ["journalctl", "--no-pager", "--no-hostname", *args]
    try:
        out = _drop_placeholders(run(argv, timeout=timeout))
        if out.strip() and "No journal files were found" not in out:
            return out
    except CommandError as exc:
        out = ""
        if not sudo_enabled(config):
            raise CommandError(
                f"{exc} — for system-wide logs, either add yourself to the "
                "systemd-journal group or run: toony sudo enable") from exc
    if sudo_enabled(config):
        try:
            return _drop_placeholders(sudo_run(config, argv, timeout=timeout))
        except CommandError:
            pass
    return out


def _drop_placeholders(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not _EMPTY.match(line))


def _lines(text: str, limit: int) -> list[str]:
    kept = [line for line in text.splitlines()
            if line.strip() and not _BORING.search(line)]
    return kept[-limit:]


@tool(description="Read recent system or application log entries from the "
                  "journal. Use this when the user asks about errors, crashes, "
                  "or what a particular program has been doing.",
      params={
          "since": {"type": "string",
                    "description": "Time window, e.g. '30 min ago', "
                                   "'today', 'yesterday', 'boot'."},
          "priority": {"type": "string",
                       "enum": list(_PRIORITY),
                       "description": "Only entries at this level or worse."},
          "unit": {"type": "string",
                   "description": "Limit to one systemd unit, e.g. 'sshd'."},
          "search": {"type": "string",
                     "description": "Only lines containing this text."},
          "limit": {"type": "integer", "description": "Max lines to return."},
      },
      requires=("journalctl",))
def read_system_logs(ctx: ToolContext, since: str = "", priority: str = "",
                     unit: str = "", search: str = "", limit: int = 40) -> str:
    config = ctx.config
    limit = max(1, min(200, int(limit or 40)))
    window = since.strip() or str(
        config.get("tools.logs.default_window", "1 hour ago") if config else "1 hour ago")

    args: list[str] = []
    if window.lower() in ("boot", "this boot", "since boot"):
        args += ["-b"]
    else:
        args += ["--since", window]
    if priority:
        args += ["-p", _PRIORITY.get(priority.lower(), "4")]
    if unit:
        args += ["-u", unit if unit.endswith(".service") else unit]
    args += ["-n", str(limit * 3)]

    text = _journal(args, config)
    lines = _lines(text, limit)
    if search:
        needle = search.lower()
        lines = [line for line in lines if needle in line.lower()][-limit:]
    if not lines:
        return (f"Nothing in the journal for that: since {window}"
                + (f", priority {priority}" if priority else "")
                + (f", unit {unit}" if unit else "") + ".")
    return f"{len(lines)} entries since {window}:\n" + "\n".join(lines)


@tool(description="Check the health of this computer and summarise anything "
                  "wrong: failed services, recent errors, crashes, memory "
                  "pressure, disk space and uptime. Use this whenever the user "
                  "asks what is going on with their system, why it is slow, or "
                  "whether anything is broken.")
def diagnose_system(ctx: ToolContext) -> str:
    config = ctx.config
    report: list[str] = []

    report.append(_uptime_line())
    report += _failed_units(config)
    report += _resource_lines()
    report += _error_summary(config)
    report += _crash_lines(config)
    report += _hardware_lines(config)

    body = "\n".join(line for line in report if line)
    return body or "Nothing looks wrong: no failed services and no recent errors."


def _uptime_line() -> str:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            seconds = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return ""
    hours, minutes = divmod(int(seconds) // 60, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"UPTIME: {days} days, {hours} hours."
    if hours:
        return f"UPTIME: {hours} hours, {minutes} minutes."
    return f"UPTIME: {minutes} minutes."


def _failed_units(config) -> list[str]:
    if not which("systemctl"):
        return []
    out: list[str] = []
    for scope, label in (("--user", "user"), ("--system", "system")):
        try:
            text = run(["systemctl", scope, "--failed", "--no-legend",
                        "--plain", "--no-pager"], timeout=10)
        except CommandError:
            continue
        names = [line.split()[0] for line in text.splitlines() if line.strip()]
        if names:
            out.append(f"FAILED {label} SERVICES ({len(names)}): "
                       + ", ".join(names[:8]))
    if not out:
        out.append("SERVICES: none failed.")
    return out


def _resource_lines() -> list[str]:
    out: list[str] = []
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            load = fh.read().split()
        cores = _cpu_count()
        one = float(load[0])
        note = " (heavily loaded)" if one > cores else ""
        out.append(f"LOAD: {one:.2f} over {cores} cores{note}. "
                   f"Running processes: {load[3]}.")
    except (OSError, ValueError, IndexError):
        pass

    memory = _meminfo()
    if memory:
        total, available, swap_total, swap_free = memory
        used_pct = 100 * (total - available) / total if total else 0
        line = f"MEMORY: {used_pct:.0f}% of {total / 1048576:.1f} GB in use"
        if swap_total:
            swap_pct = 100 * (swap_total - swap_free) / swap_total
            line += f", swap {swap_pct:.0f}% used"
            if swap_pct > 50:
                line += " (heavy swapping — this is why it feels slow)"
        out.append(line + ".")

    try:
        text = run(["df", "-h", "--output=pcent,avail,target", "-x", "tmpfs",
                    "-x", "devtmpfs"], timeout=10)
        tight = []
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[0].rstrip("%").isdigit():
                if int(parts[0].rstrip("%")) >= 85:
                    tight.append(f"{parts[2]} at {parts[0]} ({parts[1]} free)")
        out.append("DISK: " + ("; ".join(tight) if tight
                               else "all filesystems below 85% full."))
    except CommandError:
        pass
    return out


def _cpu_count() -> int:
    try:
        import os
        return os.cpu_count() or 1
    except Exception:
        return 1


def _meminfo() -> tuple[int, int, int, int] | None:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    values[key] = int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if "MemTotal" not in values:
        return None
    return (values.get("MemTotal", 0), values.get("MemAvailable", 0),
            values.get("SwapTotal", 0), values.get("SwapFree", 0))


def _error_summary(config) -> list[str]:
    """Group this boot's errors by the program that logged them."""
    try:
        text = _journal(["-b", "-p", "3", "-n", "400", "-o", "short"], config)
    except CommandError as exc:
        return [f"LOGS: could not be read ({exc})."]
    lines = [line for line in text.splitlines()
             if line.strip() and not _BORING.search(line)]
    if not lines:
        return ["ERRORS: none logged since boot."]

    sources = Counter()
    for line in lines:
        match = re.search(r"\s([\w.@\-]+)\[\d+\]:", line)
        sources[match.group(1) if match else "system"] += 1
    top = ", ".join(f"{name} ({count})" for name, count in sources.most_common(4))
    recent = "; ".join(_message(line) for line in lines[-3:])
    return [f"ERRORS: {len(lines)} since boot, mostly from {top}.",
            f"MOST RECENT: {recent}"]


def _message(line: str) -> str:
    """Strip the timestamp and process prefix off a journal line."""
    stripped = re.sub(r"^\w{3} \d{2} \d{2}:\d{2}:\d{2} ", "", line)
    stripped = re.sub(r"^[\w.@\-]+\[\d+\]:\s*", "", stripped)
    return stripped[:160].strip()


def _crash_lines(config) -> list[str]:
    out: list[str] = []
    if which("coredumpctl"):
        try:
            text = run(["coredumpctl", "list", "--no-pager", "--no-legend",
                        "-n", "5"], timeout=10, check=False)
            crashes = [line.split()[-2] for line in text.splitlines()
                       if len(line.split()) > 2]
            if crashes:
                out.append(f"CRASHES: {len(crashes)} recent core dumps from "
                           + ", ".join(sorted(set(crashes))[:5]) + ".")
        except CommandError:
            pass
    try:
        text = _journal(["-b", "-g", "Out of memory|oom-kill", "-n", "5"], config)
        if text.strip():
            out.append("OUT OF MEMORY: the kernel killed at least one process "
                       "this boot because memory ran out.")
    except CommandError:
        pass
    return out


def _hardware_lines(config) -> list[str]:
    out: list[str] = []
    temperature = _cpu_temperature()
    if temperature is not None:
        note = " (hot — expect throttling)" if temperature >= 85 else ""
        out.append(f"TEMPERATURE: CPU around {temperature:.0f} degrees{note}.")
    try:
        text = _journal(["-b", "-k", "-p", "3", "-n", "10"], config)
        lines = [line for line in text.splitlines() if line.strip()]
        if lines:
            out.append(f"KERNEL: {len(lines)} error-level kernel messages, "
                       f"latest: {_message(lines[-1])}")
    except CommandError:
        pass
    return out


def _cpu_temperature() -> float | None:
    from pathlib import Path

    best: float | None = None
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            kind = (zone / "type").read_text(encoding="utf-8").strip()
            if kind not in ("x86_pkg_temp", "acpitz", "cpu-thermal", "TCPU"):
                continue
            value = int((zone / "temp").read_text(encoding="utf-8").strip()) / 1000
        except (OSError, ValueError):
            continue
        if 0 < value < 150 and (best is None or value > best):
            best = value
    return best


@tool(description="List systemd services, optionally only the failed or "
                  "running ones. Use this to check whether something is up.",
      params={"state": {"type": "string",
                        "enum": ["failed", "running", "enabled", "all"]},
              "scope": {"type": "string", "enum": ["system", "user"]},
              "search": {"type": "string", "description": "Filter by name."}},
      requires=("systemctl",))
def list_services(ctx: ToolContext, state: str = "failed",
                  scope: str = "system", search: str = "") -> str:
    flag = "--user" if scope == "user" else "--system"
    argv = ["systemctl", flag, "--no-pager", "--no-legend", "--plain"]
    if state == "failed":
        argv += ["--failed"]
    elif state == "enabled":
        argv += ["list-unit-files", "--state=enabled", "--type=service"]
    else:
        argv += ["list-units", "--type=service"]
        if state == "running":
            argv += ["--state=running"]
    text = run(argv, timeout=15, check=False)
    names = [line.split()[0] for line in text.splitlines() if line.strip()]
    if search:
        names = [n for n in names if search.lower() in n.lower()]
    if not names:
        return f"No {state} {scope} services."
    shown = names[:20]
    more = f" and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
    return f"{len(names)} {state} {scope} services: " + ", ".join(shown) + more + "."


@tool(description="Show the status of one systemd service, including whether it "
                  "is running and its last few log lines.",
      params={"name": {"type": "string"},
              "scope": {"type": "string", "enum": ["system", "user"]}},
      required=["name"], requires=("systemctl",))
def service_status(ctx: ToolContext, name: str, scope: str = "system") -> str:
    flag = "--user" if scope == "user" else "--system"
    text = run(["systemctl", flag, "status", name, "--no-pager", "-n", "8"],
               timeout=15, check=False)
    return text[:2000] or f"No service called {name}."


@tool(description="Start, stop or restart a systemd service. Only use this when "
                  "the user clearly asks for it by name.",
      risk="dangerous",
      params={"name": {"type": "string"},
              "action": {"type": "string",
                         "enum": ["start", "stop", "restart", "reload"]},
              "scope": {"type": "string", "enum": ["system", "user"]}},
      required=["name", "action"], requires=("systemctl",))
def control_service(ctx: ToolContext, name: str, action: str,
                    scope: str = "system") -> str:
    if scope == "user":
        run(["systemctl", "--user", action, name], timeout=30)
        return f"{action.capitalize()}ed the user service {name}."
    sudo_run(ctx.config, ["systemctl", action, name], timeout=30)
    return f"{action.capitalize()}ed {name}."
