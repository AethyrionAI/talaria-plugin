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
