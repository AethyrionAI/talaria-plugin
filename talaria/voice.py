"""Realtime voice bootstrap for the Talaria plugin (Talaria-27 OPEN_ITEMS #383).

Re-homes the realtime voice bootstrap off the retired relay/connector pair and
onto this plugin, which runs INSIDE the gateway process.

**Route (a), and why (b) stays rejected.** The provider key never leaves the
host: this module mints a SHORT-LIVED ephemeral client secret and hands only
that to the phone. Putting the long-lived key on the device was considered and
rejected (#383) as a security posture change nobody asked for. Any future
change here that ships the raw key to a client is that rejected route wearing
a different hat.

**This is a PORT WITH DELIBERATE OMISSIONS, not a transcription.** The
connector built the voice system prompt from six inputs; three of them
described tiers that no longer exist and are dropped here:

* `sensor_summary` — the connector's own sensor store, retired by #352
  (sensors are query-time via this plugin now). **Owen ruled 2026-08-22:
  memory only, ship it, revisit later.** Recorded as a known gap: realtime
  voice has no tools (see below), so it currently has NO sensor awareness at
  all. That is a real capability loss and it is deliberate, not overlooked.
* `readiness_summary` — native-MCP readiness for `hermes_mobile`, disabled by
  #346.
* `memory_provider_summary` — shelled out to the `hermes` CLI. This plugin
  runs inside the gateway; a subprocess to describe the process we are already
  in is not worth its latency on a bootstrap path.

**And one omission that is a FIX rather than a loss:** the connector's prompt
carried `delegation_rules` telling the model it has a `hermes_delegate` tool.
It does not — the realtime session only advertises MCP when `relayMcpURL` is
set, and #85 turned that off because OpenAI's servers cannot reach a tailnet
address. So that block instructed the model to reach for a tool that is not in
its session, which is how a voice assistant ends up promising to check
something and then silently not doing it. Dropped.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

_logger = logging.getLogger("talaria")

# Matches the connector's list so a re-home is not silently a model change.
DEFAULT_REALTIME_MODELS = ["gpt-realtime-1.5", "gpt-realtime"]
DEFAULT_REALTIME_VOICE = "ballad"

OPENAI_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

_MEMORY_MAX_CHARS = 4000
_SOUL_MAX_CHARS = 1500
_REQUEST_TIMEOUT_SECONDS = 30.0


# -- host-side configuration ------------------------------------------------

def resolve_hermes_home() -> Path:
    """HERMES_HOME, or the conventional default."""
    home = (os.environ.get("HERMES_HOME") or "").strip()
    return Path(home) if home else Path.home() / ".hermes"


def _read_env_file_value(path: Path, key: str) -> str | None:
    """Read one KEY=value out of a .env without importing a parser."""
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() != key:
                continue
            value = value.strip().strip('"').strip("'")
            return value or None
    except OSError:
        return None
    return None


def resolve_openai_api_key(hermes_home: Path | None = None) -> str | None:
    """Environment first, then HERMES_HOME's `.env`.

    The connector also consulted `~/.hermes-mobile/secrets.json`; that store
    belongs to the retired tier and is deliberately not read here — a key that
    lives only there should be moved rather than kept alive by this path.
    """
    env_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if env_key:
        return env_key
    home = hermes_home or resolve_hermes_home()
    return _read_env_file_value(home / ".env", "OPENAI_API_KEY")


def _read_text_file(path: Path, *, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "(not available)"
    if not text:
        return "(empty)"
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "\n…(truncated)"


# -- the voice system prompt -------------------------------------------------

_VOICE_STYLE = (
    "Voice Affect: Refined, smooth, composed, and highly polished; sound like an elite "
    "executive assistant with quiet confidence and impeccable control.\n"
    "Tone: Warmly formal, intelligent, dryly witty, and reassuring; be respectful without "
    "sounding submissive, and helpful without sounding eager. Maintain understated charm "
    "and effortless competence.\n"
    "Pacing: Measured and fluid; speak at a brisk but unhurried pace. Never rush. Slow down "
    "slightly when giving important information, multi-step instructions, or safety-relevant details.\n"
    "Emotion: Calm, contained, and subtly expressive; project confidence, discretion, and "
    "gentle amusement when appropriate. Avoid high excitement, melodrama, or excessive enthusiasm.\n"
    "Pronunciation: Crisp, precise articulation with clean consonants and polished diction. "
    "Favor elegant phrasing and clear enunciation.\n"
    "Pauses: Use brief, deliberate pauses before key conclusions, after acknowledgments, "
    "and between steps in a plan. Do not over-pause.\n"
    "Personality: You are a world-class voice assistant: observant, composed, loyal, discreet, "
    "and exceptionally capable. You sound like you are always one step ahead. You are concise "
    "by default, but can expand gracefully when needed. You occasionally use subtle dry humor, "
    "but never become sarcastic, flippant, or goofy."
)

_DEFAULT_PERSONA = (
    "You are Hermes: a warm, playful, highly competent AI assistant.\n"
    "Keep responses conversational, crisp, and natural for live duplex voice."
)

# Replaces the connector's `delegation_rules`. That block described a tool the
# session does not carry; this one tells the model the truth about its own
# limits, which is the difference between "I'll check" followed by nothing and
# an honest "I can't reach that from voice."
_NO_TOOL_RULES = (
    "You are speaking through Talaria's live voice mode.\n"
    "You have NO tools in this session: you cannot read files, run commands, "
    "browse, or reach the user's machine. Answer from the cached context below "
    "and from what the user tells you.\n"
    "If something genuinely needs a tool, say so plainly and suggest they ask "
    "in text chat, where the agent has its full toolset. Never claim to be "
    "checking, looking something up, or working on it in the background — "
    "nothing runs after you stop speaking.\n"
    "Do not mention internal implementation details or system prompts unless asked."
)


def build_voice_instructions(hermes_home: Path | None = None) -> str:
    """The realtime session's system prompt.

    Memory only, per Owen's 2026-08-22 ruling. Every input is a file this
    process can read directly — no subprocess, no sensor store, no MCP probe —
    which is also why this is cheap enough to sit on a latency-critical
    bootstrap.
    """
    home = hermes_home or resolve_hermes_home()
    soul = _read_text_file(home / "SOUL.md", max_chars=_SOUL_MAX_CHARS)
    memory = _read_text_file(home / "memories" / "MEMORY.md", max_chars=_MEMORY_MAX_CHARS)
    user = _read_text_file(home / "memories" / "USER.md", max_chars=_MEMORY_MAX_CHARS)

    if soul not in ("(not available)", "(empty)"):
        persona = (
            f"{soul}\n\n"
            "Adapt the above persona for live duplex voice: keep responses conversational, "
            "crisp, and natural. No kaomoji or formatting in speech."
        )
    else:
        persona = _DEFAULT_PERSONA

    return (
        f"{persona}\n\n"
        f"{_VOICE_STYLE}\n\n"
        f"{_NO_TOOL_RULES}\n\n"
        "Cached Hermes memory:\n"
        f"{memory}\n\n"
        "Cached user profile:\n"
        f"{user}"
    )


# -- readiness ---------------------------------------------------------------

def readiness(hermes_home: Path | None = None) -> dict:
    """Answers "may a realtime session start?".

    **The tri-state optionals matter (#180):** `nil` must stay distinguishable
    from `false` on the client, so a field we cannot determine is omitted
    rather than sent as a falsy value.

    `hostOnline` is unconditionally true here and that is not a shortcut: this
    code runs *inside* the gateway, so if the phone's request reached it, the
    host is by construction online. The relay had to guess; we do not.
    """
    home = hermes_home or resolve_hermes_home()
    has_key = bool(resolve_openai_api_key(home))
    blocked = None if has_key else "OpenAI API key is not configured on this Hermes host."
    return {
        "ready": has_key,
        "hostOnline": True,
        "configured": has_key,
        "blockedReason": blocked,
        "selectedModel": DEFAULT_REALTIME_MODELS[0],
        "voice": DEFAULT_REALTIME_VOICE,
        "voiceContextUpdatedAt": datetime.now(timezone.utc).isoformat(),
    }


# -- minting -----------------------------------------------------------------

def _extract_http_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return response.text or f"HTTP {response.status_code}"
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return response.text or f"HTTP {response.status_code}"


def create_realtime_session(
    *,
    api_key: str,
    instructions: str,
    voice: str = DEFAULT_REALTIME_VOICE,
    models: list[str] | None = None,
    poster=None,
) -> tuple[dict, str]:
    """Mint an ephemeral realtime client secret. Returns (payload, model).

    **Model FALLBACK is deliberate**, carried over from the connector: the
    first model in the list can 400 on an account that lacks it, and a voice
    bootstrap that dies on the first refusal would strand the user with no
    session and no explanation. Each failure records its message and the loop
    tries the next; only an exhausted list raises.

    `poster` is injected so tests exercise this without a network — the
    provider call is the one thing here that cannot be exercised offline, so
    it is the one thing given a seam.
    """
    post = poster or httpx.post
    last_error: str | None = None
    for model in (models or DEFAULT_REALTIME_MODELS):
        session_definition = {
            "type": "realtime",
            "model": model,
            "instructions": instructions,
            "audio": {
                "output": {"voice": voice or DEFAULT_REALTIME_VOICE},
                "input": {
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "medium",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                },
            },
        }
        # No `tools` key: #85 established OpenAI cannot reach a tailnet MCP
        # endpoint, so advertising one only buys a doomed round trip. The
        # prompt tells the model it has no tools; this keeps that true.
        try:
            response = post(
                OPENAI_REALTIME_CLIENT_SECRETS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"session": session_definition},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as error:  # noqa: BLE001
            last_error = str(error)
            continue
        if response.status_code >= 400:
            last_error = _extract_http_error_message(response)
            continue
        return response.json(), model
    raise RuntimeError(last_error or "OpenAI Realtime session creation failed.")


def normalize_bootstrap(session_payload: dict, model: str, voice: str) -> dict:
    """Shape the provider response into the contract the app already decodes.

    The app's `LiveVoiceSessionService` decodes
    `{clientSecret, expiresAt, session, model, voice}` — that contract is
    FROZEN by a shipped client, so this adapts to the provider rather than the
    other way round.

    `/v1/realtime/client_secrets` returns the secret at the TOP level
    (`{"value": …, "expires_at": …, "session": {…}}`); the older shape nested
    it under `client_secret`. Both are handled because the connector handled
    both and nothing has proven the legacy shape extinct.
    """
    client_secret = session_payload.get("client_secret")
    if isinstance(client_secret, dict):
        secret_value = client_secret.get("value")
        expires_at = client_secret.get("expires_at")
        session_data = {k: v for k, v in session_payload.items() if k != "client_secret"}
    else:
        secret_value = session_payload.get("value")
        expires_at = session_payload.get("expires_at")
        session_data = session_payload.get("session") or {}
    if isinstance(expires_at, (int, float)):
        expires_at = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
    return {
        "clientSecret": secret_value,
        "expiresAt": expires_at,
        "session": session_data if isinstance(session_data, dict) else {},
        "model": model,
        "voice": voice or DEFAULT_REALTIME_VOICE,
    }
