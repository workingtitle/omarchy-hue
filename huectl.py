#!/usr/bin/env python3
"""Tiny dependency-free Philips Hue v2 client for the Omarchy bar plugin."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy" / "hue"
CONFIG_FILE = CONFIG_DIR / "config.json"


def emit(value, code=0):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(code)


def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return {"bridge": str(data.get("bridge", "")).strip(), "app_key": str(data.get("app_key", "")).strip()}
    except (OSError, ValueError, TypeError):
        return {"bridge": "", "app_key": ""}


def save_config(bridge, app_key):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps({"bridge": bridge, "app_key": app_key}, indent=2) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(CONFIG_FILE)


def request_json(method, bridge, path, app_key="", body=None, verify=False):
    headers = {"Accept": "application/json"}
    if app_key:
        headers["hue-application-key"] = app_key
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    scheme = "https" if path.startswith("/clip/") else "http"
    context = ssl.create_default_context() if verify else ssl._create_unverified_context()
    req = Request(f"{scheme}://{bridge}{path}", data=payload, headers=headers, method=method)
    try:
        with urlopen(req, timeout=4, context=context if scheme == "https" else None) as response:
            return json.loads(response.read().decode() or "{}")
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Bridge returned HTTP {error.code}: {detail[:160]}") from error
    except (URLError, TimeoutError, socket.timeout, http.client.HTTPException) as error:
        raise RuntimeError(f"Bridge is unreachable: {error}") from error


def discover():
    # Hue's documented discovery endpoint is only used for initial setup.
    try:
        req = Request("https://discovery.meethue.com/", headers={"Accept": "application/json"})
        with urlopen(req, timeout=5) as response:
            items = json.loads(response.read().decode())
        bridges = [{"bridge": str(item.get("internalipaddress", "")), "id": str(item.get("id", ""))} for item in items]
        bridges = [item for item in bridges if item["bridge"]]
        emit({"ok": True, "bridges": bridges})
    except Exception as error:
        emit({"ok": False, "error": f"No bridge found: {error}"}, 1)


def pair(bridge):
    bridge = bridge.strip()
    if not bridge:
        emit({"ok": False, "error": "Bridge address is missing"}, 2)
    try:
        result = request_json("POST", bridge, "/api", body={"devicetype": "omarchy_hue#bar"})
        first = result[0] if isinstance(result, list) and result else {}
        if "success" in first:
            key = first["success"].get("username", "")
            save_config(bridge, key)
            emit({"ok": True, "bridge": bridge})
        error = first.get("error", {}).get("description", "Pairing failed")
        emit({"ok": False, "error": error}, 1)
    except RuntimeError as error:
        emit({"ok": False, "error": str(error)}, 1)


def light_rows(data):
    resources = data.get("data", [])
    connectivity = {}
    for resource in resources:
        if resource.get("type") != "zigbee_connectivity":
            continue
        owner = resource.get("owner", {})
        connectivity[str(owner.get("rid", ""))] = str(resource.get("status", ""))

    rows = []
    for light in resources:
        if light.get("type") != "light":
            continue
        device_id = str(light.get("owner", {}).get("rid", ""))
        # A Hue light is controllable only while its device connectivity says
        # connected. Hiding all other states also avoids optimistic controls
        # for powered-off or unreachable bulbs.
        if connectivity.get(device_id) != "connected":
            continue
        metadata = light.get("metadata", {})
        on = bool(light.get("on", {}).get("on", False))
        brightness = round(float(light.get("dimming", {}).get("brightness", 0)))
        color = light.get("color") if isinstance(light.get("color"), dict) else None
        color_temperature = light.get("color_temperature") if isinstance(light.get("color_temperature"), dict) else None
        mirek_schema = color_temperature.get("mirek_schema", {}) if color_temperature else {}
        rows.append({
            "id": str(light.get("id", "")),
            "name": str(metadata.get("name", "Light")),
            "on": on,
            "brightness": brightness,
            "color_capable": color is not None,
            "gamut": color.get("gamut") if color else None,
            "xy": color.get("xy") if color else None,
            "temperature_capable": color_temperature is not None,
            "mirek": color_temperature.get("mirek") if color_temperature else None,
            "mirek_min": mirek_schema.get("mirek_minimum", 153),
            "mirek_max": mirek_schema.get("mirek_maximum", 500),
        })
    rows.sort(key=lambda row: row["name"].casefold())
    return rows


def status():
    config = load_config()
    if not config["bridge"] or not config["app_key"]:
        emit({"ok": False, "configured": False, "error": "Not paired with a Hue Bridge yet"})
    try:
        response = request_json("GET", config["bridge"], "/clip/v2/resource", config["app_key"])
        if response.get("errors"):
            emit({"ok": False, "configured": True, "error": str(response["errors"][0].get("description", "Hue API error"))}, 1)
        lights = light_rows(response)
        emit({
            "ok": True,
            "configured": True,
            "bridge": config["bridge"],
            "lights": lights,
            "on_count": sum(1 for light in lights if light["on"]),
        })
    except RuntimeError as error:
        emit({"ok": False, "configured": True, "bridge": config["bridge"], "error": str(error)}, 1)


def srgb_to_xy(hex_color, gamut=None):
    value = hex_color.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("Color must use the #RRGGBB format")
    try:
        channels = [int(value[index:index + 2], 16) / 255.0 for index in (0, 2, 4)]
    except ValueError as error:
        raise ValueError("Invalid hexadecimal color") from error
    linear = [((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in channels]
    red, green, blue = linear
    x_val = red * 0.664511 + green * 0.154324 + blue * 0.162028
    y_val = red * 0.283881 + green * 0.668433 + blue * 0.047685
    z_val = red * 0.000088 + green * 0.072310 + blue * 0.986039
    total = x_val + y_val + z_val
    point = (x_val / total, y_val / total) if total else (0.0, 0.0)
    return clip_to_gamut(point, gamut) if gamut else point


def clip_to_gamut(point, gamut):
    try:
        triangle = [(float(gamut[name]["x"]), float(gamut[name]["y"])) for name in ("red", "green", "blue")]
    except (KeyError, TypeError, ValueError):
        return point

    def cross(a, b, p):
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])

    signs = [cross(triangle[i], triangle[(i + 1) % 3], point) for i in range(3)]
    if all(value >= 0 for value in signs) or all(value <= 0 for value in signs):
        return point

    def closest(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = dx * dx + dy * dy
        t = 0 if length == 0 else max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length))
        return (a[0] + t * dx, a[1] + t * dy)

    candidates = [closest(triangle[i], triangle[(i + 1) % 3]) for i in range(3)]
    return min(candidates, key=lambda candidate: math.dist(candidate, point))


def update(light_id, on=None, brightness=None, color=None, gamut=None, xy=None, mirek=None):
    config = load_config()
    if not config["bridge"] or not config["app_key"]:
        emit({"ok": False, "error": "Plugin is not paired"}, 2)
    body = {}
    if on is not None:
        body["on"] = {"on": on}
    if brightness is not None:
        body["dimming"] = {"brightness": max(1.0, min(100.0, brightness))}
        if brightness > 0:
            body.setdefault("on", {"on": True})
    if color is not None:
        x_val, y_val = srgb_to_xy(color, gamut)
        body["color"] = {"xy": {"x": round(x_val, 6), "y": round(y_val, 6)}}
        body.setdefault("on", {"on": True})
    if xy is not None:
        body["color"] = {"xy": {"x": xy[0], "y": xy[1]}}
    if mirek is not None:
        body["color_temperature"] = {"mirek": max(153, min(500, int(mirek)))}
        body.setdefault("on", {"on": True})
    try:
        response = request_json("PUT", config["bridge"], f"/clip/v2/resource/light/{light_id}", config["app_key"], body)
        if response.get("errors"):
            emit({"ok": False, "error": str(response["errors"][0].get("description", "Hue API error"))}, 1)
        emit({"ok": True})
    except RuntimeError as error:
        emit({"ok": False, "error": str(error)}, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("discover")
    pair_parser = sub.add_parser("pair")
    pair_parser.add_argument("bridge")
    set_parser = sub.add_parser("set")
    set_parser.add_argument("light_id")
    set_parser.add_argument("--on", choices=("true", "false"))
    set_parser.add_argument("--brightness", type=float)
    set_parser.add_argument("--color")
    set_parser.add_argument("--gamut", help="Hue gamut JSON from the light resource")
    set_parser.add_argument("--xy", nargs=2, type=float, metavar=("X", "Y"), help=argparse.SUPPRESS)
    set_parser.add_argument("--mirek", type=int)
    args = parser.parse_args()
    if args.command == "status": status()
    elif args.command == "discover": discover()
    elif args.command == "pair": pair(args.bridge)
    elif args.command == "set":
        try:
            gamut = json.loads(args.gamut) if args.gamut else None
            update(args.light_id, None if args.on is None else args.on == "true", args.brightness, args.color, gamut, args.xy, args.mirek)
        except (ValueError, json.JSONDecodeError) as error:
            emit({"ok": False, "error": str(error)}, 2)


if __name__ == "__main__":
    main()
