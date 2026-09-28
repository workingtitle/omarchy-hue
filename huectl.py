#!/usr/bin/env python3
"""Tiny dependency-free Philips Hue v2 client for the Omarchy bar plugin."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omarchy" / "hue"
CONFIG_FILE = CONFIG_DIR / "config.json"

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_HELPER_OUTPUT_BYTES = 1024 * 1024
MAX_DISCOVERY_BYTES = 64 * 1024
MAX_ERROR_BYTES = 4 * 1024
MAX_ERROR_TEXT = 240
MAX_RESOURCES = 4096
MAX_LIGHTS = 1024
MAX_BRIDGES = 32
MAX_LIGHT_NAME = 128
WARM_WHITE_MIREK = 370
BRIDGE_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{16}$")
APP_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")

# Philips Hue's documented local-bridge trust roots. The older root-bridge
# CA covers the bridge currently in use; the Signify Hue Root CA covers newer
# firmware. Hostname checking is disabled because bridge certificates use the
# bridge ID as CN and normally do not contain an IP SAN. The CN is checked
# against the bridge identity returned by the same verified connection.
# Source: https://developers.meethue.com/develop/application-design-guidance/using-https/
HUE_BRIDGE_ROOT_CAS = """-----BEGIN CERTIFICATE-----
MIICMjCCAdigAwIBAgIUO7FSLbaxikuXAljzVaurLXWmFw4wCgYIKoZIzj0EAwIw
OTELMAkGA1UEBhMCTkwxFDASBgNVBAoMC1BoaWxpcHMgSHVlMRQwEgYDVQQDDAty
b290LWJyaWRnZTAiGA8yMDE3MDEwMTAwMDAwMFoYDzIwMzgwMTE5MDMxNDA3WjA5
MQswCQYDVQQGEwJOTDEUMBIGA1UECgwLUGhpbGlwcyBIdWUxFDASBgNVBAMMC3Jv
b3QtYnJpZGdlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjNw2tx2AplOf9x86
aTdvEcL1FU65QDxziKvBpW9XXSIcibAeQiKxegpq8Exbr9v6LBnYbna2VcaK0G22
jOKkTqOBuTCBtjAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBhjAdBgNV
HQ4EFgQUZ2ONTFrDT6o8ItRnKfqWKnHFGmQwdAYDVR0jBG0wa4AUZ2ONTFrDT6o8
ItRnKfqWKnHFGmShPaQ7MDkxCzAJBgNVBAYTAk5MMRQwEgYDVQQKDAtQaGlsaXBz
IEh1ZTEUMBIGA1UEAwwLcm9vdC1icmlkZ2WCFDuxUi22sYpLlwJY81Wrqy11phcO
MAoGCCqGSM49BAMCA0gAMEUCIEBYYEOsa07TH7E5MJnGw557lVkORgit2Rm1h3B2
sFgDAiEA1Fj/C3AN5psFMjo0//mrQebo0eKd3aWRx+pQY08mk48=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIBzDCCAXOgAwIBAgICEAAwCgYIKoZIzj0EAwIwPDELMAkGA1UEBhMCTkwxFDAS
BgNVBAoMC1NpZ25pZnkgSHVlMRcwFQYDVQQDDA5IdWUgUm9vdCBDQSAwMTAgFw0y
NTAyMjUwMDAwMDBaGA8yMDUwMTIzMTIzNTk1OVowPDELMAkGA1UEBhMCTkwxFDAS
BgNVBAoMC1NpZ25pZnkgSHVlMRcwFQYDVQQDDA5IdWUgUm9vdCBDQSAwMTBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABFfOO0jfSAUXGQ9kjEDzyBrcMQ3ItyA5krE+
cyvb1Y3xFti7KlAad8UOnAx0FBLn7HZrlmIwm1QnX0fK3LPM13mjYzBhMB0GA1Ud
DgQWBBTF1pSpsCASX/z0VHLigxU2CAaqoTAfBgNVHSMEGDAWgBTF1pSpsCASX/z0
VHLigxU2CAaqoTAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBBjAKBggq
hkjOPQQDAgNHADBEAiAk7duT+IHbOGO4UUuGLAEpyYejGZK9Z7V9oSfnvuQ5BQIg
IYSgwwxHXm73/JgcU9lAM6c8Bmu3UE3kBIUwBs1qXFw=
-----END CERTIFICATE-----
"""


def emit(value, code=0):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_HELPER_OUTPUT_BYTES:
        payload = b'{"ok":false,"error":"Hue client output exceeds its size limit"}'
        code = 1
    sys.stdout.write(payload.decode("utf-8") + "\n")
    raise SystemExit(code)


def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return {
            "bridge": str(data.get("bridge", "")).strip(),
            "app_key": str(data.get("app_key", "")).strip(),
            "bridge_id": str(data.get("bridge_id", "")).strip(),
        }
    except (OSError, ValueError, TypeError):
        return {"bridge": "", "app_key": "", "bridge_id": ""}


def save_config(bridge, app_key, bridge_id=""):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    temp = CONFIG_FILE.with_suffix(".tmp")
    data = {"bridge": bridge, "app_key": app_key}
    if bridge_id:
        data["bridge_id"] = bridge_id
    temp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(CONFIG_FILE)


def limited_text(value, limit=MAX_ERROR_TEXT):
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\x00", "")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def bounded_read(response, limit):
    headers = getattr(response, "headers", None)
    content_length = headers.get("Content-Length") if headers is not None else None
    if content_length is None and hasattr(response, "getheader"):
        content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            content_length = int(content_length)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Response has an invalid Content-Length") from error
        if content_length < 0 or content_length > limit:
            raise RuntimeError(f"Response exceeds the {limit}-byte limit")

    data = response.read(limit + 1)
    if not isinstance(data, bytes):
        raise RuntimeError("Response body is not bytes")
    if len(data) > limit:
        raise RuntimeError(f"Response exceeds the {limit}-byte limit")
    return data


def response_error_body(response):
    try:
        return limited_text(bounded_read(response, MAX_ERROR_BYTES).decode(errors="replace"), MAX_ERROR_TEXT)
    except RuntimeError:
        return "Response error body exceeded its size limit"


def bridge_ip(value):
    address = str(value).strip()
    try:
        ipaddress.ip_address(address)
    except ValueError as error:
        raise RuntimeError("Bridge address must be an IP address") from error
    return address


def normalize_bridge_id(value):
    bridge_id = str(value).strip() if value is not None else ""
    return bridge_id.upper() if BRIDGE_ID_PATTERN.fullmatch(bridge_id) else ""


def bridge_tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False
    context.load_verify_locations(cadata=HUE_BRIDGE_ROOT_CAS)
    return context


def peer_bridge_id(connection):
    certificate = connection.sock.getpeercert()
    subject = certificate.get("subject", ()) if certificate else ()
    for relative_name in subject:
        for name, value in relative_name:
            if name == "commonName":
                bridge_id = normalize_bridge_id(value)
                if bridge_id:
                    return bridge_id
    raise RuntimeError("Hue Bridge certificate has no valid Bridge ID")


def request_json(method, bridge, path, app_key="", body=None, expected_bridge_id=""):
    bridge = bridge_ip(bridge)
    if not path.startswith("/") or any(char in path for char in "\r\n"):
        raise RuntimeError("Invalid Hue API path")
    if app_key and not APP_KEY_PATTERN.fullmatch(app_key):
        raise RuntimeError("Invalid Hue application key")
    headers = {"Accept": "application/json"}
    if app_key:
        headers["hue-application-key"] = app_key
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPSConnection(bridge, timeout=4, context=bridge_tls_context())
    try:
        connection.connect()
        peer_id = peer_bridge_id(connection)
        expected_id = normalize_bridge_id(expected_bridge_id)
        if expected_bridge_id and not expected_id:
            raise RuntimeError("Configured Bridge ID is invalid")
        if expected_id and peer_id != expected_id:
            raise RuntimeError("Hue Bridge identity changed; re-pair is required")
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raw = bounded_read(response, MAX_ERROR_BYTES)
            detail = limited_text(raw.decode(errors="replace"), MAX_ERROR_TEXT)
            raise RuntimeError(f"Bridge returned HTTP {response.status}: {detail}")
        raw = bounded_read(response, MAX_JSON_BYTES)
        try:
            data = json.loads(raw.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("Bridge returned invalid JSON") from error
        if path == "/api/config":
            response_id = normalize_bridge_id(data.get("bridgeid") if isinstance(data, dict) else "")
            if not response_id or response_id != peer_id:
                raise RuntimeError("Hue Bridge certificate and API identity do not match")
        return data
    except RuntimeError:
        raise
    except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as error:
        raise RuntimeError(f"Bridge is unreachable: {limited_text(error)}") from error
    finally:
        connection.close()


def parse_ssdp_headers(payload):
    headers = {}
    for line in payload.decode(errors="replace").splitlines()[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def find_local_bridges():
    """Find Hue Bridges on the local network without using the cloud endpoint."""
    search_template = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        "MAN: \"ssdp:discover\"\r\n"
        "MX: 1\r\n"
        "ST: {target}\r\n"
        "\r\n"
    ).encode()
    candidates = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.25)
            for target in ("upnp:rootdevice", "ssdp:all"):
                sock.sendto(search_template.replace(b"{target}", target.encode()), ("239.255.255.250", 1900))

            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                try:
                    payload, _ = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                location = parse_ssdp_headers(payload).get("location", "")
                parsed = urlparse(location)
                host = parsed.hostname
                if parsed.scheme in ("http", "https") and host and ":" not in host:
                    candidates[host] = True
                    if len(candidates) >= MAX_BRIDGES:
                        break
    except OSError:
        return []

    bridges = []
    for bridge in sorted(candidates):
        try:
            info = request_json("GET", bridge, "/api/config")
        except RuntimeError:
            continue
        if isinstance(info, dict) and normalize_bridge_id(info.get("bridgeid")):
            bridges.append({
                "bridge": bridge,
                "name": limited_text(info.get("name", "Philips Hue Bridge"), MAX_LIGHT_NAME),
            })
    return bridges


def discover():
    # Hue's documented discovery endpoint is only used for initial setup.
    try:
        req = Request("https://discovery.meethue.com/", headers={"Accept": "application/json"})
        with urlopen(req, timeout=5) as response:
            items = json.loads(bounded_read(response, MAX_DISCOVERY_BYTES).decode())
        if not isinstance(items, list) or len(items) > MAX_BRIDGES:
            raise RuntimeError("Discovery returned too many bridges")
        bridges = [{"bridge": str(item.get("internalipaddress", "")), "id": str(item.get("id", ""))} for item in items]
        bridges = [item for item in bridges if item["bridge"]]
        emit({"ok": True, "bridges": bridges})
    except HTTPError as error:
        emit({"ok": False, "error": f"No bridge found: HTTP {error.code}: {response_error_body(error)}"}, 1)
    except Exception as error:
        emit({"ok": False, "error": f"No bridge found: {limited_text(error)}"}, 1)


def bridge_identity(bridge):
    info = request_json("GET", bridge, "/api/config")
    bridge_id = normalize_bridge_id(info.get("bridgeid") if isinstance(info, dict) else "")
    if not bridge_id:
        raise RuntimeError("Hue Bridge returned an invalid Bridge ID")
    return bridge_id


def ensure_bridge_identity(config):
    if not APP_KEY_PATTERN.fullmatch(config.get("app_key", "")):
        raise RuntimeError("Stored Hue application key is invalid")
    bridge_id = bridge_identity(config["bridge"])
    stored_id = normalize_bridge_id(config.get("bridge_id", ""))
    if stored_id and stored_id != bridge_id:
        raise RuntimeError("Configured Bridge ID changed; re-pair is required")
    if not stored_id:
        save_config(config["bridge"], config["app_key"], bridge_id)
    return bridge_id


def hue_error(response, fallback="Hue API error"):
    if isinstance(response, dict):
        errors = response.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            description = errors[0].get("description")
            if isinstance(description, str) and description:
                return limited_text(description)
    return fallback


def presence():
    config = load_config()
    if config["bridge"] and config["app_key"]:
        try:
            bridge_id = ensure_bridge_identity(config)
            response = request_json("GET", config["bridge"], "/clip/v2/resource", config["app_key"], expected_bridge_id=bridge_id)
            if response.get("errors"):
                emit({"ok": False, "configured": True, "present": False, "error": hue_error(response)}, 1)
            lights = light_rows(response)
            emit({
                "ok": True,
                "configured": True,
                "present": bool(lights),
                "bridge": config["bridge"],
                "lights": lights,
                "on_count": sum(1 for light in lights if light["on"]),
            })
        except RuntimeError as error:
            emit({"ok": False, "configured": True, "present": False, "bridge": config["bridge"], "error": limited_text(error)}, 1)

    bridges = find_local_bridges()
    emit({
        "ok": True,
        "configured": False,
        "present": bool(bridges),
        "bridges": bridges,
    })


def pair(bridge):
    bridge = bridge.strip()
    if not bridge:
        emit({"ok": False, "error": "Bridge address is missing"}, 2)
    try:
        bridge_id = bridge_identity(bridge)
        result = request_json("POST", bridge, "/api", body={"devicetype": "omarchy_hue#bar"}, expected_bridge_id=bridge_id)
        try:
            key = pairing_key(result)
        except RuntimeError:
            first = result[0] if isinstance(result, list) and result else {}
            error = "Pairing failed"
            if isinstance(first, dict) and isinstance(first.get("error"), dict):
                error = limited_text(first["error"].get("description", error))
            emit({"ok": False, "error": error}, 1)
        save_config(bridge_ip(bridge), key, bridge_id)
        emit({"ok": True, "bridge": bridge})
    except RuntimeError as error:
        emit({"ok": False, "error": limited_text(error)}, 1)


def pairing_key(result):
    first = result[0] if isinstance(result, list) and result else {}
    success = first.get("success") if isinstance(first, dict) else None
    key = success.get("username") if isinstance(success, dict) else None
    if not isinstance(key, str) or not APP_KEY_PATTERN.fullmatch(key):
        raise RuntimeError("Bridge returned an invalid application key")
    return key


def light_rows(data):
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RuntimeError("Hue response has an invalid resource list")
    resources = data["data"]
    if len(resources) > MAX_RESOURCES:
        raise RuntimeError("Hue response contains too many resources")
    connectivity = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise RuntimeError("Hue response contains an invalid resource")
        if resource.get("type") != "zigbee_connectivity":
            continue
        owner = resource.get("owner", {})
        connectivity[str(owner.get("rid", ""))] = str(resource.get("status", ""))

    rows = []
    light_count = 0
    for light in resources:
        if light.get("type") != "light":
            continue
        light_count += 1
        if light_count > MAX_LIGHTS:
            raise RuntimeError("Hue response contains too many lights")
        device_id = str(light.get("owner", {}).get("rid", ""))
        # A Hue light is controllable only while its device connectivity says
        # connected. Hiding all other states also avoids optimistic controls
        # for powered-off or unreachable bulbs.
        if connectivity.get(device_id) != "connected":
            continue
        metadata = light.get("metadata", {}) if isinstance(light.get("metadata", {}), dict) else {}
        on = bool(light.get("on", {}).get("on", False))
        brightness = round(float(light.get("dimming", {}).get("brightness", 0)))
        color = light.get("color") if isinstance(light.get("color"), dict) else None
        color_temperature = light.get("color_temperature") if isinstance(light.get("color_temperature"), dict) else None
        mirek_schema = color_temperature.get("mirek_schema", {}) if color_temperature else {}
        hue_saturation = xy_to_hue_saturation(color.get("xy")) if color else None
        rows.append({
            "id": str(light.get("id", "")),
            "name": limited_text(metadata.get("name", "Light"), MAX_LIGHT_NAME),
            "on": on,
            "brightness": brightness,
            "color_capable": color is not None,
            "gamut": color.get("gamut") if color else None,
            "xy": color.get("xy") if color else None,
            "hue": hue_saturation[0] if hue_saturation else None,
            "saturation": hue_saturation[1] if hue_saturation else None,
            "swatch": light_swatch(color.get("xy") if color else None,
                                   color_temperature.get("mirek") if color_temperature else None),
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
        bridge_id = ensure_bridge_identity(config)
        response = request_json("GET", config["bridge"], "/clip/v2/resource", config["app_key"], expected_bridge_id=bridge_id)
        if response.get("errors"):
            emit({"ok": False, "configured": True, "error": hue_error(response)}, 1)
        lights = light_rows(response)
        emit({
            "ok": True,
            "configured": True,
            "bridge": config["bridge"],
            "lights": lights,
            "on_count": sum(1 for light in lights if light["on"]),
        })
    except RuntimeError as error:
        emit({"ok": False, "configured": True, "bridge": config["bridge"], "error": limited_text(error)}, 1)


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


def xy_to_display_rgb(xy):
    """Invert srgb_to_xy to gamma-encoded sRGB at full intensity (max channel 1)."""
    if not isinstance(xy, dict):
        return None
    try:
        x_val = float(xy.get("x"))
        y_val = float(xy.get("y"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x_val) and math.isfinite(y_val)) or y_val <= 0:
        return None
    big_x = x_val / y_val
    big_z = (1.0 - x_val - y_val) / y_val
    linear = [
        big_x * 1.656492 - 0.354851 - big_z * 0.255038,
        -big_x * 0.707196 + 1.655397 + big_z * 0.036152,
        big_x * 0.051713 - 0.121364 + big_z * 1.011530,
    ]
    peak = max(linear)
    if peak <= 0:
        return None
    channels = [max(0.0, c / peak) for c in linear]
    return tuple(1.055 * c ** (1 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c for c in channels)


def xy_to_hue_saturation(xy):
    """Map a Hue xy point onto the panel's HSL sliders (lightness fixed at 50%)."""
    rgb = xy_to_display_rgb(xy)
    if rgb is None:
        return None
    red, green, blue = rgb
    high, low = max(rgb), min(rgb)
    delta = high - low
    # Treat matrix rounding noise on neutral whites as gray.
    if delta < 1e-3:
        return (0, 0)
    if high == red:
        hue = ((green - blue) / delta) % 6
    elif high == green:
        hue = (blue - red) / delta + 2
    else:
        hue = (red - green) / delta + 4
    # An HSL color at 50% lightness scaled to full intensity has
    # min/max = (1 - s) / (1 + s), so s = (max - min) / (max + min).
    saturation = delta / (high + low)
    return (round(hue * 60) % 360, round(saturation * 100))


def mirek_to_xy(mirek):
    """Planckian locus approximation (Kim et al.) for a white tone in mirek."""
    try:
        kelvin = 1_000_000 / float(mirek)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(kelvin):
        return None
    kelvin = max(1667.0, min(25000.0, kelvin))
    if kelvin <= 4000:
        x_val = -0.2661239e9 / kelvin ** 3 - 0.2343589e6 / kelvin ** 2 + 0.8776956e3 / kelvin + 0.179910
    else:
        x_val = -3.0258469e9 / kelvin ** 3 + 2.1070379e6 / kelvin ** 2 + 0.2226347e3 / kelvin + 0.240390
    if kelvin <= 2222:
        y_val = -1.1063814 * x_val ** 3 - 1.34811020 * x_val ** 2 + 2.18555832 * x_val - 0.20219683
    elif kelvin <= 4000:
        y_val = -0.9549476 * x_val ** 3 - 1.37418593 * x_val ** 2 + 2.09137015 * x_val - 0.16748867
    else:
        y_val = 3.0817580 * x_val ** 3 - 5.87338670 * x_val ** 2 + 3.75112997 * x_val - 0.37001483
    return {"x": x_val, "y": y_val}


def light_swatch(xy, mirek):
    """Hex color approximating what a light emits, for the overview."""
    # White-only bulbs report neither; assume the common 2700 K warm white.
    rgb = xy_to_display_rgb(xy) or xy_to_display_rgb(mirek_to_xy(mirek if mirek else WARM_WHITE_MIREK))
    if rgb is None:
        return None
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


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
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(light_id)):
        emit({"ok": False, "error": "Invalid light ID"}, 2)
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
        bridge_id = ensure_bridge_identity(config)
        response = request_json("PUT", config["bridge"], f"/clip/v2/resource/light/{light_id}", config["app_key"], body, expected_bridge_id=bridge_id)
        if response.get("errors"):
            emit({"ok": False, "error": hue_error(response)}, 1)
        emit({"ok": True})
    except RuntimeError as error:
        emit({"ok": False, "error": limited_text(error)}, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("presence")
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
    elif args.command == "presence": presence()
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
