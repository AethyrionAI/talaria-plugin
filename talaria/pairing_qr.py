"""`hermes talaria pair-qr` — the host half of the app's QR pairing arm.

#309 Lane D. The QR carries the credentials the phone needs to talk to
this host's gateway directly: the gateway URL and the `API_SERVER_KEY`.
Both are READ at print time from the host's own configuration; neither
is minted, persisted, or logged. There is no new state here — the QR is
sugar on the typed arm the profile editor has always had.

**The payload is a CROSS-REPO CONTRACT.** `tests/fixtures/pair_payload.json`
holds the pinned bytes; the Talaria iOS app's scanner pins the same
fixture. Changing the shape means changing both repositories together,
which is why `talaria` is a mandatory integer version field rather than a
convention.

Why the terminal and not the dashboard: the plugin's dashboard half is
deliberately read-only status, and a web page that renders the root key on
demand is a strictly worse exposure than the terminal of the operator who
can already `cat` the `.env` the key is read from.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

# Bumping this is a BREAKING change for every already-scanned phone. The
# app pins the same integer; see the module docstring.
PAYLOAD_VERSION = 1

# Mirrors gateway/platforms/api_server.py's own DEFAULT_PORT.
DEFAULT_GATEWAY_PORT = 8642

# Tailscale's CGNAT range. The app's ATS exception is keyed to exactly this
# block (Talaria-27 #166b), so an address outside it would be blocked
# app-wide even if the phone could route to it.
_TAILNET = ipaddress.ip_network("100.64.0.0/10")

# Bind addresses that are NOT reachable from a phone. `API_SERVER_HOST` is
# a bind address, not an advertised one; advertising these would mint a QR
# that can never work.
_UNROUTABLE_BIND_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]", "127.0.0.1", "::1", "localhost"})

_QRCODE_MISSING = (
    "the `qrcode` package is not importable in this interpreter. It ships "
    "with Hermes's `messaging`, `dingtalk`, and `feishu` extras but is not a "
    "core dependency, so a minimal install lacks it. Install it into the "
    "Hermes venv with: pip install 'qrcode==7.4.2'"
)


class PairingQRError(RuntimeError):
    """A named failure. Never a guess — the QR either carries something the
    phone can use, or the operator hears why it does not."""


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------

def build_payload(*, gateway_url: str, api_key: str, name: str) -> dict:
    """Assemble the versioned payload, or raise on anything unusable."""
    url = (gateway_url or "").strip()
    key = (api_key or "").strip()
    label = (name or "").strip()

    if not key:
        raise PairingQRError(
            "no API_SERVER_KEY resolved — a QR without the key pairs nothing. "
            "Set it in HERMES_HOME's .env (the same value the gateway serves)."
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PairingQRError(
            f"gateway URL {url!r} is not an absolute http(s) URL "
            "(expected e.g. http://100.79.222.100:8642)"
        )
    if not label:
        raise PairingQRError("no host name resolved for the payload")

    return {"talaria": PAYLOAD_VERSION, "gateway": url, "key": key, "name": label}


def encode_payload(payload: dict) -> str:
    """Compact JSON. Smaller payload, smaller QR, fewer scan failures."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Reading the host's own configuration
# ---------------------------------------------------------------------------

def _dotenv_api_key() -> str | None:
    """HERMES_HOME's `.env`, through Hermes's own managed-credential read.

    `get_env_value_prefer_dotenv` deliberately prefers the `.env` over a
    value inherited from the parent shell: a key rotated in `.env` must not
    be shadowed by a stale export, because the phone would then be handed a
    credential that 401s. Returns None when `hermes_cli` is not importable,
    which drops the caller onto the plain `os.environ` read below — the very
    read `platform_adapter._api_key()` uses inside the gateway.
    """
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv
    except Exception:
        return None
    try:
        value = get_env_value_prefer_dotenv("API_SERVER_KEY")
    except Exception:
        return None
    return (value or "").strip() or None


def resolve_api_key() -> str:
    """The key at print time. Missing resolves empty; the caller reports it."""
    return _dotenv_api_key() or (os.environ.get("API_SERVER_KEY") or "").strip()


def _gateway_config_section() -> dict:
    """`gateway.platforms.api_server` merged under `gateway.api_server`.

    Hermes bridges both spellings (gateway/config.py ~:1624), so both are
    read here rather than picking one and being wrong on half the hosts.
    """
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly() or {}
    except Exception:
        return {}
    gateway = config.get("gateway") or {}
    if not isinstance(gateway, dict):
        return {}
    merged: dict = {}
    for candidate in (gateway.get("api_server"), (gateway.get("platforms") or {}).get("api_server")):
        if isinstance(candidate, dict):
            merged.update(candidate)
    return merged


def _configured_host() -> str:
    """The api_server's configured BIND host, in api_server.py's own order."""
    section = _gateway_config_section()
    if section.get("host"):
        return str(section["host"]).strip()
    return (os.environ.get("API_SERVER_HOST") or "").strip()


def _configured_port() -> int:
    """The api_server's port, in api_server.py's own order (~:1507-1510)."""
    for candidate in (_gateway_config_section().get("port"), os.environ.get("API_SERVER_PORT")):
        if candidate in (None, ""):
            continue
        try:
            port = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            return port
    return DEFAULT_GATEWAY_PORT


def _route_source_address() -> str | None:
    """The source address the kernel would use to reach Tailscale's own
    service address. Connecting a UDP socket sends no packet — this is a
    route lookup, not traffic."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("100.100.100.100", 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _tailnet_address() -> str | None:
    """The host's tailnet address, or None.

    Self-validating on purpose: the probe above answers with SOME source
    address on any routed machine, so the result is accepted only when it
    falls inside the CGNAT block. A host that is not on the tailnet yields
    None rather than a LAN address the phone cannot reach.
    """
    address = _route_source_address()
    if not address:
        return None
    try:
        if ipaddress.ip_address(address) in _TAILNET:
            return address
    except ValueError:
        return None
    return None


def resolve_gateway_url(override: str | None) -> tuple[str, str]:
    """Return `(url, provenance)`. Never invents a host.

    Order: `--gateway` verbatim → the configured bind host when it is a
    concrete routable address → this host's tailnet address. If none of
    those yields something a phone could reach, this raises rather than
    guessing; the operator passes `--gateway`.
    """
    if override:
        url = override.strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PairingQRError(
                f"--gateway {override!r} is not an absolute http(s) URL "
                "(expected e.g. http://100.110.102.59:8642)"
            )
        return url, "--gateway (verbatim, as given)"

    port = _configured_port()
    configured = _configured_host()
    if configured.lower() not in _UNROUTABLE_BIND_HOSTS:
        return f"http://{configured}:{port}", f"configured api_server host {configured!r}"

    tailnet = _tailnet_address()
    if tailnet:
        return f"http://{tailnet}:{port}", f"this host's tailnet address ({tailnet})"

    raise PairingQRError(
        "cannot derive a gateway URL a phone could reach: the api_server's "
        f"configured host is {configured or '(unset)'!r} (a bind address) and "
        "this machine has no tailnet address. Pass --gateway <url> explicitly."
    )


def resolve_host_name(override: str | None) -> str:
    """A human label for the profile the app creates. Never empty."""
    if override and override.strip():
        return override.strip()
    hostname = ""
    try:
        hostname = (socket.gethostname() or "").strip()
    except OSError:
        hostname = ""
    hostname = hostname.split(".")[0]
    return hostname or "Hermes host"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _import_qrcode():
    try:
        import qrcode
    except Exception:
        return None
    return qrcode


def _require_qrcode():
    module = _import_qrcode()
    if module is None:
        raise PairingQRError(_QRCODE_MISSING)
    return module


def payload_matrix(text: str, *, border: int = 0) -> list[list[bool]]:
    """The QR module grid. `border` is the quiet zone in modules."""
    qrcode = _require_qrcode()
    code = qrcode.QRCode(
        border=border,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    code.add_data(text)
    code.make(fit=True)
    return [[bool(cell) for cell in row] for row in code.get_matrix()]


# ANSI 256-colour black and white. Explicit on BOTH halves of every cell so
# the terminal's own theme cannot invert the code: a QR drawn in the
# terminal's foreground colour scans on a light theme and not on a dark one,
# which is the classic way terminal QRs "work on my machine".
_FG_DARK, _FG_LIGHT = "\x1b[38;5;0m", "\x1b[38;5;15m"
_BG_DARK, _BG_LIGHT = "\x1b[48;5;0m", "\x1b[48;5;15m"
_RESET = "\x1b[0m"

# Half-block glyphs for the colourless fallback (piped output). Polarity
# there depends on the terminal theme, which is exactly why colour is the
# default; this form exists so `| cat` produces something rather than noise.
_PLAIN = {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}


def render_ansi(text: str, *, color: bool = True, border: int = 4) -> str:
    """Render `text` as a QR built from half-block rows.

    Two module rows per text line, so the code comes out roughly square in
    a terminal's 1:2 cell aspect. `border` defaults to the spec's 4-module
    quiet zone.
    """
    matrix = payload_matrix(text, border=border)
    width = len(matrix[0])
    blank = [False] * width

    lines: list[str] = []
    for index in range(0, len(matrix), 2):
        top = matrix[index]
        bottom = matrix[index + 1] if index + 1 < len(matrix) else blank
        if not color:
            lines.append("".join(_PLAIN[(top[column], bottom[column])] for column in range(width)))
            continue
        cells = []
        for column in range(width):
            cells.append(
                (_FG_DARK if top[column] else _FG_LIGHT)
                + (_BG_DARK if bottom[column] else _BG_LIGHT)
                + "▀"
            )
        lines.append("".join(cells) + _RESET)
    return "\n".join(lines)


def write_png(text: str, path: str | Path, *, border: int = 4, box_size: int = 8) -> Path:
    """Write the same payload as a PNG.

    Uses qrcode's pure-Python PNG writer (pypng, a hard dependency of
    `qrcode` itself) rather than the Pillow backend, so the PNG arm needs
    nothing the ANSI arm does not already have.
    """
    qrcode = _require_qrcode()
    from qrcode.image.pure import PyPNGImage

    target = Path(path).expanduser()
    if target.parent and not target.parent.exists():
        raise PairingQRError(f"directory {str(target.parent)!r} does not exist")
    code = qrcode.QRCode(
        border=border,
        box_size=box_size,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        image_factory=PyPNGImage,
    )
    code.add_data(text)
    code.make(fit=True)
    with open(target, "wb") as handle:
        code.make_image().save(handle)
    return target


def mask_key(key: str) -> str:
    """First four and last four, so the operator can tell WHICH key went in
    without the key itself landing in a terminal scrollback."""
    if len(key) <= 8:
        return "…" * len(key)
    return f"{key[:4]}…{key[-4:]} ({len(key)} chars)"
