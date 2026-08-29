"""Network and Bluetooth state, through NetworkManager and bluetoothctl."""

from __future__ import annotations

from .proc import CommandError, run, which
from .registry import ToolContext, tool


@tool(description="Report network status: whether the machine is online, which "
                  "Wi-Fi network it is on, signal strength and its IP address.")
def network_status(ctx: ToolContext) -> str:
    if not which("nmcli"):
        return _fallback_status()
    parts: list[str] = []
    try:
        state = run(["nmcli", "-t", "-f", "STATE", "general"], timeout=10)
        parts.append(f"NetworkManager reports {state or 'unknown'}.")
    except CommandError:
        pass
    try:
        text = run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi"],
                   timeout=10)
        for line in text.splitlines():
            fields = line.split(":")
            if fields and fields[0] == "yes" and len(fields) >= 3:
                parts.append(f"Connected to Wi-Fi network {fields[1]} "
                             f"at {fields[2]} percent signal.")
                break
    except CommandError:
        pass
    try:
        text = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                    "device", "status"], timeout=10)
        for line in text.splitlines():
            fields = line.split(":")
            if len(fields) >= 4 and fields[2] == "connected" and fields[1] != "loopback":
                parts.append(f"{fields[0]} is connected via {fields[3]}.")
    except CommandError:
        pass
    address = _ip_address()
    if address:
        parts.append(f"Local address {address}.")
    return " ".join(parts) or "I could not read the network state."


def _fallback_status() -> str:
    address = _ip_address()
    return (f"Local address {address}." if address
            else "No network tools are available to check with.")


def _ip_address() -> str:
    """The address of the interface that carries the default route."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.connect(("192.0.2.1", 9))  # reserved, never actually contacted
            return sock.getsockname()[0]
    except OSError:
        return ""


@tool(description="List nearby Wi-Fi networks with their signal strength.",
      requires=("nmcli",))
def list_wifi_networks(ctx: ToolContext, limit: int = 8) -> str:
    text = run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi",
                "list"], timeout=20)
    seen: dict[str, tuple[int, str]] = {}
    for line in text.splitlines():
        fields = line.split(":")
        if len(fields) < 2 or not fields[0]:
            continue
        try:
            signal = int(fields[1])
        except ValueError:
            continue
        if fields[0] not in seen or signal > seen[fields[0]][0]:
            seen[fields[0]] = (signal, fields[2] if len(fields) > 2 else "")
    if not seen:
        return "No Wi-Fi networks are visible."
    best = sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)
    limit = max(1, min(20, int(limit or 8)))
    return "; ".join(f"{name} at {signal} percent"
                     for name, (signal, _) in best[:limit])


@tool(description="Turn Wi-Fi on or off.", risk="sensitive",
      params={"state": {"type": "string", "enum": ["on", "off"]}},
      required=["state"], requires=("nmcli",))
def set_wifi(ctx: ToolContext, state: str) -> str:
    run(["nmcli", "radio", "wifi", state], timeout=15)
    return f"Wi-Fi is {state}."


@tool(description="Connect to a saved Wi-Fi network by name.", risk="sensitive",
      params={"name": {"type": "string", "description": "The network name."}},
      required=["name"], requires=("nmcli",))
def connect_wifi(ctx: ToolContext, name: str) -> str:
    # Only saved connections: Toony never handles a Wi-Fi password.
    saved = run(["nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=10)
    names = [line for line in saved.splitlines() if line.strip()]
    match = next((n for n in names if n.lower() == name.lower()), None)
    if match is None:
        match = next((n for n in names if name.lower() in n.lower()), None)
    if match is None:
        return (f"There is no saved connection called {name}. "
                "Saved networks are: " + ", ".join(names[:10]) + ".")
    run(["nmcli", "connection", "up", match], timeout=45)
    return f"Connected to {match}."


@tool(description="Report Bluetooth state and connected devices.",
      requires=("bluetoothctl",))
def bluetooth_status(ctx: ToolContext) -> str:
    show = run(["bluetoothctl", "show"], timeout=10, check=False)
    powered = "yes" if "Powered: yes" in show else "no"
    devices = run(["bluetoothctl", "devices", "Connected"], timeout=10, check=False)
    names = [line.split(" ", 2)[-1] for line in devices.splitlines() if line.strip()]
    if powered == "no":
        return "Bluetooth is switched off."
    if not names:
        return "Bluetooth is on, with nothing connected."
    return "Bluetooth is on, connected to " + ", ".join(names) + "."


@tool(description="Turn Bluetooth on or off.", risk="sensitive",
      params={"state": {"type": "string", "enum": ["on", "off"]}},
      required=["state"], requires=("bluetoothctl",))
def set_bluetooth(ctx: ToolContext, state: str) -> str:
    run(["bluetoothctl", "power", state], timeout=15, check=False)
    return f"Bluetooth is {state}."


@tool(description="Connect to a paired Bluetooth device by name, such as a "
                  "headset or speaker.", risk="sensitive",
      params={"name": {"type": "string"}}, required=["name"],
      requires=("bluetoothctl",))
def connect_bluetooth(ctx: ToolContext, name: str) -> str:
    text = run(["bluetoothctl", "devices", "Paired"], timeout=10, check=False)
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and name.lower() in parts[2].lower():
            run(["bluetoothctl", "connect", parts[1]], timeout=30, check=False)
            return f"Connecting to {parts[2]}."
    return f"No paired Bluetooth device matches {name}."
