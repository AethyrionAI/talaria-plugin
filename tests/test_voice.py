"""#383 — the realtime voice bootstrap, re-homed onto this plugin.

The provider call is the only thing here that cannot run offline, so it is the
only thing given a seam (`poster`). Everything else — key resolution, the
system prompt, the response shaping, and all three verbs' authorization — is
exercised for real.
"""

import pytest

from talaria import database, outbox, store, voice
from talaria.envelope import EnvelopeService
from talaria.transport import TransportHub

# Built at runtime so the plugin security scanner never sees a
# credential-shaped literal (same reason as test_envelope.py).
API_KEY = "test-api-key-" + "64chars-" + "a" * 43
FAKE_OPENAI_KEY = "sk-test-" + "x" * 20


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")
    service = EnvelopeService(
        api_key_provider=lambda: API_KEY,
        hub=TransportHub(),
        store_mod=store,
        outbox_mod=outbox,
        hold_seconds=0.05,
        touch_throttle_seconds=0.0,
    )
    device_id, token = store.create_paired_device("i-voice", "phone")
    return service, device_id, token


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# -- key resolution ----------------------------------------------------------

def test_api_key_comes_from_env_first_then_the_hermes_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text(f'OPENAI_API_KEY="{FAKE_OPENAI_KEY}"\n', encoding="utf-8")
    assert voice.resolve_openai_api_key(tmp_path) == FAKE_OPENAI_KEY

    monkeypatch.setenv("OPENAI_API_KEY", "env-wins")
    assert voice.resolve_openai_api_key(tmp_path) == "env-wins"


def test_a_missing_env_file_yields_no_key_rather_than_raising(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert voice.resolve_openai_api_key(tmp_path / "nope") is None


# -- the system prompt -------------------------------------------------------

def test_the_prompt_carries_soul_and_memory_and_omits_the_retired_inputs(tmp_path):
    (tmp_path / "SOUL.md").write_text("I am Hermes, and this is my soul.", encoding="utf-8")
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "MEMORY.md").write_text("Owen ships at night.", encoding="utf-8")
    (tmp_path / "memories" / "USER.md").write_text("Owen. iOS. Talaria.", encoding="utf-8")

    prompt = voice.build_voice_instructions(tmp_path)

    assert "this is my soul" in prompt
    assert "Owen ships at night" in prompt
    assert "Owen. iOS. Talaria." in prompt
    # The three inputs dropped with the retired tiers (#352 sensors, #346 MCP,
    # and the hermes-CLI memory-provider shell-out).
    assert "sensor freshness" not in prompt.lower()
    assert "memory provider" not in prompt.lower()
    assert "tool readiness" not in prompt.lower()


def test_the_prompt_does_not_promise_a_tool_the_session_lacks():
    """The omission that is a FIX, not a loss.

    The connector's prompt described a `hermes_delegate` tool. #85 turned MCP
    advertising off, so no such tool is in the session — and a model told it
    has one says "let me check on that" and then does nothing. The replacement
    must state the limit instead.
    """
    prompt = voice.build_voice_instructions()
    assert "hermes_delegate" not in prompt
    assert "NO tools" in prompt


def test_a_missing_soul_falls_back_to_the_default_persona(tmp_path):
    prompt = voice.build_voice_instructions(tmp_path)
    assert "warm, playful, highly competent" in prompt
    assert "(not available)" not in prompt.split("Cached Hermes memory")[0]


# -- readiness ---------------------------------------------------------------

def test_readiness_is_blocked_with_a_NAMED_reason_when_no_key_is_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = voice.readiness(tmp_path)
    assert state["ready"] is False
    assert state["configured"] is False
    # #180: a refusal the user can act on beats a bare false.
    assert "not configured" in state["blockedReason"]


def test_readiness_reports_host_online_because_this_code_runs_in_the_gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    state = voice.readiness(tmp_path)
    assert state["ready"] is True
    assert state["hostOnline"] is True
    assert state["blockedReason"] is None


# -- minting -----------------------------------------------------------------

def test_the_model_list_FALLS_BACK_when_the_first_model_is_refused():
    """A voice bootstrap must not die on the first model an account lacks."""
    seen = []

    def poster(url, headers=None, json=None, timeout=None):
        model = json["session"]["model"]
        seen.append(model)
        if model == "gpt-realtime-1.5":
            return _Response(400, {"error": {"message": "model not available"}})
        return _Response(200, {"value": "ek_live", "expires_at": 1756000000, "session": {"id": "s1"}})

    payload, model = voice.create_realtime_session(
        api_key=FAKE_OPENAI_KEY, instructions="be brief", poster=poster
    )
    assert seen == ["gpt-realtime-1.5", "gpt-realtime"]
    assert model == "gpt-realtime"
    assert payload["value"] == "ek_live"


def test_an_exhausted_model_list_raises_with_the_PROVIDERS_message():
    def poster(url, headers=None, json=None, timeout=None):
        return _Response(400, {"error": {"message": "quota exceeded"}})

    with pytest.raises(RuntimeError, match="quota exceeded"):
        voice.create_realtime_session(
            api_key=FAKE_OPENAI_KEY, instructions="x", poster=poster
        )


def test_the_session_advertises_NO_tools(monkeypatch):
    """#85: OpenAI cannot reach a tailnet MCP endpoint, so advertising one
    buys a doomed round trip — and the prompt tells the model it has no tools.
    This keeps that true."""
    captured = {}

    def poster(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Response(200, {"value": "ek", "expires_at": 1, "session": {}})

    voice.create_realtime_session(api_key=FAKE_OPENAI_KEY, instructions="x", poster=poster)
    assert "tools" not in captured["session"]


def test_the_provider_key_is_sent_ONLY_as_a_bearer_header_never_in_the_body():
    """Route (b) was rejected to keep the long-lived key host-side. This pins
    that the key never reaches a place the response could echo to a client."""
    captured = {}

    def poster(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        captured["body"] = json
        return _Response(200, {"value": "ek", "expires_at": 1, "session": {}})

    voice.create_realtime_session(api_key=FAKE_OPENAI_KEY, instructions="x", poster=poster)
    assert captured["headers"]["Authorization"] == f"Bearer {FAKE_OPENAI_KEY}"
    assert FAKE_OPENAI_KEY not in str(captured["body"])


# -- response shaping (the app's decode target is FROZEN by a shipped client) --

def test_the_top_level_secret_shape_is_normalized():
    out = voice.normalize_bootstrap(
        {"value": "ek_top", "expires_at": 1756000000, "session": {"id": "s"}},
        "gpt-realtime", "ballad",
    )
    assert out["clientSecret"] == "ek_top"
    assert out["session"] == {"id": "s"}
    assert out["expiresAt"].startswith("2025-") or "T" in out["expiresAt"]


def test_the_LEGACY_nested_secret_shape_is_also_normalized():
    out = voice.normalize_bootstrap(
        {"client_secret": {"value": "ek_nested", "expires_at": 1756000000}, "id": "s2"},
        "gpt-realtime", "ballad",
    )
    assert out["clientSecret"] == "ek_nested"
    assert out["session"]["id"] == "s2"


# -- the verbs ---------------------------------------------------------------

async def test_every_voice_verb_requires_a_token_bound_to_the_claimed_device(env):
    service, device_id, _ = env
    for verb in ("talk_readiness", "talk_session_create", "talk_session_end"):
        refused = await service.dispatch({"type": verb, "auth": "junk", "device_id": device_id})
        assert refused["code"] == "device_auth_mismatch", verb


async def test_session_create_refuses_CLEANLY_when_no_key_is_configured(env, monkeypatch, tmp_path):
    service, device_id, token = env
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    out = await service.dispatch(
        {"type": "talk_session_create", "auth": token, "device_id": device_id}
    )
    # Named, not a 500 and not a generic storage_error.
    assert out["code"] == "talk_not_configured"


async def test_session_end_ACKS_a_session_this_process_never_saw(env):
    """A gateway restart between create and end, and #383's compensating end
    for a bootstrap abandoned by supersession, both produce this. Refusing
    would turn cleanup into an error the app must decide to ignore."""
    service, device_id, token = env
    out = await service.dispatch(
        {"type": "talk_session_end", "auth": token, "device_id": device_id,
         "voice_session_id": "never-existed"}
    )
    assert out["ended"] is True


async def test_the_five_original_verbs_are_untouched(env):
    """383-A's additive claim, at the dispatch level: adding voice must not
    disturb chat or sensors."""
    service, device_id, token = env
    out = await service.dispatch({"type": "drain", "auth": token, "device_id": device_id})
    assert "items" in out and "queries" in out
    unknown = await service.dispatch({"type": "talk_turn_append", "auth": token, "device_id": device_id})
    # Deliberately NOT implemented — and it must say so rather than 500.
    assert unknown["code"] == "unknown_event_type"


# -- turn detection (#396-B) -------------------------------------------------

def _clear_voice_env(monkeypatch):
    for key in (
        "TALARIA_VOICE_TURN_DETECTION", "TALARIA_VOICE_EAGERNESS",
        "TALARIA_VOICE_CREATE_RESPONSE", "TALARIA_VOICE_INTERRUPT_RESPONSE",
        "TALARIA_VOICE_VAD_THRESHOLD", "TALARIA_VOICE_VAD_PREFIX_PADDING_MS",
        "TALARIA_VOICE_VAD_SILENCE_DURATION_MS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_the_default_block_is_BYTE_IDENTICAL_to_the_literal_383_shipped(monkeypatch, tmp_path):
    """**396-D, as an assertion rather than a promise.**

    This item restores configurability; it does NOT retune anything. If this
    test ever has to change, a default moved — and 396-D says that requires a
    recorded before/after and a session that justifies it.

    **396-P-C extends the same contract to the coarse picker:** "normal" and
    every unrecognized tuning — junk strings and non-strings included — are
    the SAME call as no tuning at all. The picker adds reachable presets; it
    must not be able to move the default.
    """
    _clear_voice_env(monkeypatch)
    literal_383 = {
        "type": "semantic_vad",
        "create_response": True,
        "interrupt_response": True,
        "eagerness": "medium",
    }
    assert voice.resolve_turn_detection(tmp_path) == literal_383
    for tuning in (None, "normal", "garbage", 123):
        assert voice.resolve_turn_detection(tmp_path, tuning=tuning) == literal_383, tuning


def test_server_vad_is_REACHABLE_and_brings_its_own_knobs(monkeypatch, tmp_path):
    """The whole reason this went past config-ification.

    `semantic_vad` has no activation threshold, so the fault Owen actually
    reported — a television transcribed word for word — is unreachable until
    this type is selectable.
    """
    _clear_voice_env(monkeypatch)
    monkeypatch.setenv("TALARIA_VOICE_TURN_DETECTION", "server_vad")
    monkeypatch.setenv("TALARIA_VOICE_VAD_THRESHOLD", "0.8")
    block = voice.resolve_turn_detection(tmp_path)
    assert block["type"] == "server_vad"
    assert block["threshold"] == 0.8
    assert block["prefix_padding_ms"] == 300
    assert block["silence_duration_ms"] == 500


def test_the_keys_are_TYPE_SCOPED_because_the_wrong_key_is_a_provider_error(monkeypatch, tmp_path):
    """Sending a type the other type's keys fails the mint, which on this path
    means a user who cannot start a voice session at all."""
    _clear_voice_env(monkeypatch)
    # server_vad values set, but the type left alone: they must be IGNORED,
    # not smuggled into a semantic_vad block.
    monkeypatch.setenv("TALARIA_VOICE_VAD_THRESHOLD", "0.9")
    semantic = voice.resolve_turn_detection(tmp_path)
    assert "threshold" not in semantic
    assert semantic["eagerness"] == "medium"

    monkeypatch.setenv("TALARIA_VOICE_TURN_DETECTION", "server_vad")
    monkeypatch.setenv("TALARIA_VOICE_EAGERNESS", "low")
    server = voice.resolve_turn_detection(tmp_path)
    assert "eagerness" not in server


def test_a_BAD_value_falls_back_LOUDLY_instead_of_taking_voice_down(monkeypatch, tmp_path):
    """A typo must not be able to break the bootstrap.

    This module sits on the start path: raising here is a user with no voice
    session, which is strictly worse than a user whose typo was ignored.
    """
    _clear_voice_env(monkeypatch)
    monkeypatch.setenv("TALARIA_VOICE_TURN_DETECTION", "sematic_vad")   # typo
    monkeypatch.setenv("TALARIA_VOICE_EAGERNESS", "very")               # not a level
    monkeypatch.setenv("TALARIA_VOICE_CREATE_RESPONSE", "sure")         # not a bool
    block = voice.resolve_turn_detection(tmp_path)
    assert block == {
        "type": "semantic_vad",
        "create_response": True,
        "interrupt_response": True,
        "eagerness": "medium",
    }

    # Out-of-range is refused the same way as unparseable.
    monkeypatch.setenv("TALARIA_VOICE_TURN_DETECTION", "server_vad")
    monkeypatch.setenv("TALARIA_VOICE_VAD_THRESHOLD", "7")              # > 1.0
    assert voice.resolve_turn_detection(tmp_path)["threshold"] == 0.5


def test_settings_come_from_the_env_FILE_too_not_only_the_environment(monkeypatch, tmp_path):
    """Same precedence and the same two sources as the API key — one place to
    look for anything this module reads."""
    _clear_voice_env(monkeypatch)
    (tmp_path / ".env").write_text(
        'TALARIA_VOICE_EAGERNESS="low"\nTALARIA_VOICE_INTERRUPT_RESPONSE=false\n',
        encoding="utf-8",
    )
    block = voice.resolve_turn_detection(tmp_path)
    assert block["eagerness"] == "low"
    assert block["interrupt_response"] is False

    monkeypatch.setenv("TALARIA_VOICE_EAGERNESS", "high")
    assert voice.resolve_turn_detection(tmp_path)["eagerness"] == "high"


def test_the_minted_session_CARRIES_the_resolved_block(monkeypatch):
    """The resolver existing is not the same as the session using it."""
    captured = {}

    def poster(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Response(200, {"value": "ek", "expires_at": 1, "session": {}})

    voice.create_realtime_session(
        api_key=FAKE_OPENAI_KEY, instructions="x", poster=poster,
        turn_detection={"type": "server_vad", "threshold": 0.72},
    )
    assert captured["session"]["audio"]["input"]["turn_detection"] == {
        "type": "server_vad", "threshold": 0.72,
    }


def test_readiness_REPORTS_the_effective_turn_detection(monkeypatch, tmp_path):
    """396-D's spirit: a value nobody can read is one nobody can notice has
    moved. Additive — the shipped Swift client ignores unknown keys."""
    _clear_voice_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("TALARIA_VOICE_EAGERNESS", "low")
    state = voice.readiness(tmp_path)
    assert state["turnDetection"]["eagerness"] == "low"
    assert state["turnDetection"]["type"] == "semantic_vad"


# -- the coarse picker's presets (#396) ---------------------------------------

def test_quiet_and_noisy_are_VETTED_server_vad_blocks_with_EXACTLY_that_types_keys(monkeypatch, tmp_path):
    """396-P-D (host half): both presets are server_vad — the only type with
    an activation threshold — and full-dict equality pins the key set along
    with the vetted values, because a smuggled `eagerness` (or any other
    wrong-type key) is a provider error that kills the bootstrap."""
    _clear_voice_env(monkeypatch)
    vetted = {
        "quiet": {"threshold": 0.4, "prefix_padding_ms": 300, "silence_duration_ms": 500},
        "noisy": {"threshold": 0.75, "prefix_padding_ms": 400, "silence_duration_ms": 900},
    }
    for tuning, values in vetted.items():
        assert voice.resolve_turn_detection(tmp_path, tuning=tuning) == {
            "type": "server_vad",
            "create_response": True,
            "interrupt_response": True,
            **values,
        }, tuning


def test_a_preset_OVERRIDES_the_env_knobs_but_NOT_the_response_flags(monkeypatch, tmp_path):
    """The vetted values are the point of a preset: a host that has tuned
    server_vad by hand still gets exactly the quiet block when the phone asks
    for quiet. The response flags are different — they ride the SAME env
    resolution the default uses, because create/interrupt behaviour is host
    policy, not room acoustics."""
    _clear_voice_env(monkeypatch)
    monkeypatch.setenv("TALARIA_VOICE_TURN_DETECTION", "semantic_vad")
    monkeypatch.setenv("TALARIA_VOICE_VAD_THRESHOLD", "0.9")
    monkeypatch.setenv("TALARIA_VOICE_INTERRUPT_RESPONSE", "false")
    block = voice.resolve_turn_detection(tmp_path, tuning="quiet")
    assert block["type"] == "server_vad"
    assert block["threshold"] == 0.4
    assert block["create_response"] is True
    assert block["interrupt_response"] is False


def test_readiness_advertises_the_tunings_the_mint_accepts(monkeypatch, tmp_path):
    """396-P-E (host half): the static capability list is what lets the app
    render the picker only against a host that understands the field."""
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    state = voice.readiness(tmp_path)
    assert state["tunings"] == ["quiet", "normal", "noisy"]


def _capturing_poster(captured):
    def poster(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Response(200, {"value": "ek", "expires_at": 1756000000, "session": {"id": "s"}})
    return poster


async def test_session_create_passes_the_payloads_tuning_to_the_mint(env, monkeypatch):
    """The preset existing is not the same as the phone being able to select
    it: a `"tuning": "noisy"` payload must reach the provider request with the
    noisy block."""
    service, device_id, token = env
    _clear_voice_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    captured = {}
    monkeypatch.setattr(voice.httpx, "post", _capturing_poster(captured))
    out = await service.dispatch({
        "type": "talk_session_create", "auth": token, "device_id": device_id,
        "tuning": "noisy",
    })
    assert out["voiceSession"]["status"] == "active"
    assert captured["session"]["audio"]["input"]["turn_detection"] == {
        "type": "server_vad",
        "create_response": True,
        "interrupt_response": True,
        "threshold": 0.75,
        "prefix_padding_ms": 400,
        "silence_duration_ms": 900,
    }


async def test_session_create_with_garbage_or_absent_tuning_mints_with_the_DEFAULT(env, monkeypatch):
    """396-P-C at the envelope: a payload without the field, with a junk
    string, or with a non-string all mint exactly today's default session —
    the untrusted field degrades to current behaviour, never to an error."""
    service, device_id, token = env
    _clear_voice_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    captured = {}
    monkeypatch.setattr(voice.httpx, "post", _capturing_poster(captured))
    for extra in ({}, {"tuning": "cathedral"}, {"tuning": 123}):
        captured.clear()
        out = await service.dispatch({
            "type": "talk_session_create", "auth": token, "device_id": device_id, **extra,
        })
        assert out["voiceSession"]["status"] == "active", extra
        assert captured["session"]["audio"]["input"]["turn_detection"] == {
            "type": "semantic_vad",
            "create_response": True,
            "interrupt_response": True,
            "eagerness": "medium",
        }, extra
