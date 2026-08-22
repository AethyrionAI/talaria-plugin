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


# -- turn detection (#396-B) -------------------------------------------------
#
# #383 ported the connector's turn-detection VALUES as a literal and dropped
# its CONFIGURABILITY along the way — the connector carried
# `RealtimeTalkConfig.turn_detection_type / create_response /
# interrupt_response` as settings. Nobody asked for that loss; it was simply
# not part of the port. This restores it.
#
# **No default moves here (#396-D).** The dict this builds with an empty
# environment is byte-identical to the literal #383 shipped: semantic_vad,
# eagerness medium, create_response and interrupt_response both true. This
# commit makes the knobs REACHABLE; turning one is a separate decision that
# has to record its before/after.
#
# **Why `server_vad` had to become reachable at all**, rather than just
# lifting the literal into a constant: Owen's 2026-08-22 characterisation
# confirmed two faults with two different mechanisms. `semantic_vad` has NO
# activation threshold — it takes an `eagerness`, which governs how quickly
# the model decides the USER has finished, and so addresses the mutual
# cut-offs and nothing else. The threshold that governs what OPENS a turn
# (room noise, a television) belongs to `server_vad`, which #383 made
# unreachable by fixing the type. Config-ifying without this would have
# shipped configurability that cannot reach the confirmed complaint.

TURN_DETECTION_TYPES = ("semantic_vad", "server_vad")
SEMANTIC_VAD_EAGERNESS = ("low", "medium", "high", "auto")

DEFAULT_TURN_DETECTION_TYPE = "semantic_vad"
DEFAULT_SEMANTIC_EAGERNESS = "medium"
DEFAULT_CREATE_RESPONSE = True
DEFAULT_INTERRUPT_RESPONSE = True
# server_vad's own defaults, applied only when that type is selected. These
# are the provider's documented defaults, restated so a host that selects
# server_vad without tuning gets the provider's behaviour rather than ours.
DEFAULT_VAD_THRESHOLD = 0.5
DEFAULT_VAD_PREFIX_PADDING_MS = 300
DEFAULT_VAD_SILENCE_DURATION_MS = 500


def _resolve_setting(key: str, hermes_home: Path | None = None) -> str | None:
    """One voice setting: environment first, then HERMES_HOME's `.env`.

    Same precedence and the same two sources as `resolve_openai_api_key`, on
    purpose — one place to look for anything this module reads.
    """
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    home = hermes_home or resolve_hermes_home()
    return _read_env_file_value(home / ".env", key)


def _coerce_bool(raw: str | None, default: bool, *, key: str) -> bool:
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    _logger.warning("talaria voice: %s=%r is not a boolean — using %r", key, raw, default)
    return default


def _coerce_number(raw: str | None, default, *, key: str, cast, low, high):
    """Numeric setting with a RANGE, falling back loudly rather than raising.

    **A fat-fingered threshold must not take voice down.** This module sits on
    the bootstrap path: an exception here is a user who cannot start a voice
    session, which is strictly worse than a user whose typo was ignored. So
    every bad value logs and falls back — and the log is the only way anyone
    finds out, which is why it is WARNING rather than debug.
    """
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        _logger.warning("talaria voice: %s=%r is not a number — using %r", key, raw, default)
        return default
    if not (low <= value <= high):
        _logger.warning(
            "talaria voice: %s=%r is outside [%s, %s] — using %r", key, raw, low, high, default
        )
        return default
    return value


def resolve_turn_detection(hermes_home: Path | None = None) -> dict:
    """Build the session's `turn_detection` block from host configuration.

    **The keys are TYPE-SCOPED and that is not tidiness.** `eagerness` belongs
    to `semantic_vad` and `threshold`/`prefix_padding_ms`/`silence_duration_ms`
    belong to `server_vad`; sending one type the other's keys is a provider
    error, which on this path means a failed bootstrap. So the emitted dict
    carries only the keys valid for the selected type, and a host that has
    tuned server_vad values but left the type at semantic_vad simply gets
    semantic_vad — its knobs ignored, not smuggled through.
    """
    home = hermes_home or resolve_hermes_home()

    raw_type = _resolve_setting("TALARIA_VOICE_TURN_DETECTION", home)
    detection_type = (raw_type or DEFAULT_TURN_DETECTION_TYPE).strip().lower()
    if detection_type not in TURN_DETECTION_TYPES:
        _logger.warning(
            "talaria voice: TALARIA_VOICE_TURN_DETECTION=%r is not one of %s — using %r",
            raw_type, ", ".join(TURN_DETECTION_TYPES), DEFAULT_TURN_DETECTION_TYPE,
        )
        detection_type = DEFAULT_TURN_DETECTION_TYPE

    block: dict = {
        "type": detection_type,
        "create_response": _coerce_bool(
            _resolve_setting("TALARIA_VOICE_CREATE_RESPONSE", home),
            DEFAULT_CREATE_RESPONSE,
            key="TALARIA_VOICE_CREATE_RESPONSE",
        ),
        "interrupt_response": _coerce_bool(
            _resolve_setting("TALARIA_VOICE_INTERRUPT_RESPONSE", home),
            DEFAULT_INTERRUPT_RESPONSE,
            key="TALARIA_VOICE_INTERRUPT_RESPONSE",
        ),
    }

    if detection_type == "semantic_vad":
        raw_eagerness = _resolve_setting("TALARIA_VOICE_EAGERNESS", home)
        eagerness = (raw_eagerness or DEFAULT_SEMANTIC_EAGERNESS).strip().lower()
        if eagerness not in SEMANTIC_VAD_EAGERNESS:
            _logger.warning(
                "talaria voice: TALARIA_VOICE_EAGERNESS=%r is not one of %s — using %r",
                raw_eagerness, ", ".join(SEMANTIC_VAD_EAGERNESS), DEFAULT_SEMANTIC_EAGERNESS,
            )
            eagerness = DEFAULT_SEMANTIC_EAGERNESS
        block["eagerness"] = eagerness
        return block

    block["threshold"] = _coerce_number(
        _resolve_setting("TALARIA_VOICE_VAD_THRESHOLD", home),
        DEFAULT_VAD_THRESHOLD,
        key="TALARIA_VOICE_VAD_THRESHOLD", cast=float, low=0.0, high=1.0,
    )
    block["prefix_padding_ms"] = _coerce_number(
        _resolve_setting("TALARIA_VOICE_VAD_PREFIX_PADDING_MS", home),
        DEFAULT_VAD_PREFIX_PADDING_MS,
        key="TALARIA_VOICE_VAD_PREFIX_PADDING_MS", cast=int, low=0, high=5000,
    )
    block["silence_duration_ms"] = _coerce_number(
        _resolve_setting("TALARIA_VOICE_VAD_SILENCE_DURATION_MS", home),
        DEFAULT_VAD_SILENCE_DURATION_MS,
        key="TALARIA_VOICE_VAD_SILENCE_DURATION_MS", cast=int, low=0, high=10000,
    )
    return block


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
        # #396-B: the EFFECTIVE turn detection, so the configuration is
        # observable instead of having to be inferred from behaviour. 396-D
        # forbids a silent default change; a value nobody can read is one
        # nobody can notice has moved. Additive — the shipped Swift client
        # decodes a fixed field set and ignores unknown keys.
        "turnDetection": resolve_turn_detection(home),
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
    turn_detection: dict | None = None,
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
    # #396-B: resolved once, OUTSIDE the model-fallback loop. Resolving per
    # attempt would re-read the environment mid-bootstrap and could hand two
    # models two different configurations, which is the kind of difference
    # nobody would think to look for when a session behaves oddly.
    turn_detection = turn_detection or resolve_turn_detection()
    last_error: str | None = None
    for model in (models or DEFAULT_REALTIME_MODELS):
        session_definition = {
            "type": "realtime",
            "model": model,
            "instructions": instructions,
            "audio": {
                "output": {"voice": voice or DEFAULT_REALTIME_VOICE},
                "input": {
                    "turn_detection": turn_detection,
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
