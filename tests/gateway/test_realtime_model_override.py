"""Realtime (voice) sessions get a low-latency model; text chats do not.

A live call cannot absorb the primary model's first-token latency, but the same
platform also carries SMS — so the override has to key off the session, not the
platform alone.
"""

import pytest

from gateway.run import GatewayRunner


PRIMARY = "moonshotai/kimi-k3"
FAST = "anthropic/claude-haiku-4.5"


@pytest.fixture
def runner():
    return object.__new__(GatewayRunner)


def _route(model=PRIMARY):
    return {
        "model": model,
        "runtime": {
            "provider": "nous",
            "requested_provider": None,
            "base_url": None,
            "api_mode": None,
            "command": None,
            "args": [],
        },
        "signature": (model, "nous", None, None, None, None, ()),
    }


def _config(**vonage):
    return {"platforms": {"vonage": {"enabled": True, **vonage}}}


def test_live_call_is_routed_to_the_fast_model(runner):
    route = _config(realtime_model=FAST)
    r = _route()
    runner._apply_realtime_model_override(r, route, "vonage", "call:17274854546")
    assert r["model"] == FAST


def test_browser_media_session_is_also_realtime(runner):
    r = _route()
    runner._apply_realtime_model_override(
        r, _config(realtime_model=FAST), "vonage", "browser:smoketest"
    )
    assert r["model"] == FAST


def test_sms_on_the_same_platform_keeps_the_primary_model(runner):
    """The regression that matters: voice and SMS share the vonage adapter."""
    r = _route()
    runner._apply_realtime_model_override(
        r, _config(realtime_model=FAST), "vonage", "17274854546"
    )
    assert r["model"] == PRIMARY


def test_signature_follows_the_model_so_the_agent_cache_does_not_serve_the_old_one(runner):
    r = _route()
    before = r["signature"]
    runner._apply_realtime_model_override(
        r, _config(realtime_model=FAST), "vonage", "call:1"
    )
    assert r["signature"] != before
    assert r["signature"][0] == FAST


def test_provider_and_credentials_are_left_alone(runner):
    r = _route()
    runner._apply_realtime_model_override(
        r, _config(realtime_model=FAST), "vonage", "call:1"
    )
    assert r["runtime"]["provider"] == "nous"


def test_no_override_configured_is_a_no_op(runner):
    r = _route()
    runner._apply_realtime_model_override(r, _config(), "vonage", "call:1")
    assert r["model"] == PRIMARY


def test_other_platforms_are_untouched(runner):
    r = _route()
    runner._apply_realtime_model_override(
        r, _config(realtime_model=FAST), "telegram", "call:1"
    )
    assert r["model"] == PRIMARY


def test_custom_prefixes_replace_the_defaults(runner):
    r = _route()
    cfg = _config(realtime_model=FAST, realtime_chat_prefixes=["sip:"])
    runner._apply_realtime_model_override(r, cfg, "vonage", "call:1")
    assert r["model"] == PRIMARY

    r2 = _route()
    runner._apply_realtime_model_override(r2, cfg, "vonage", "sip:alice")
    assert r2["model"] == FAST


def test_a_single_prefix_may_be_given_as_a_bare_string(runner):
    r = _route()
    cfg = _config(realtime_model=FAST, realtime_chat_prefixes="sip:")
    runner._apply_realtime_model_override(r, cfg, "vonage", "sip:alice")
    assert r["model"] == FAST


def test_gateway_platforms_shape_is_also_honoured(runner):
    """``hermes gateway setup`` writes settings under gateway.platforms."""
    r = _route()
    cfg = {"gateway": {"platforms": {"vonage": {"realtime_model": FAST}}}}
    runner._apply_realtime_model_override(r, cfg, "vonage", "call:1")
    assert r["model"] == FAST


@pytest.mark.parametrize(
    "platforms",
    [["vonage"], None, "vonage"],
    ids=["list-of-names", "missing", "scalar"],
)
def test_non_dict_platform_config_does_not_raise(runner, platforms):
    """gateway setup can emit a list of enabled names rather than a mapping."""
    r = _route()
    runner._apply_realtime_model_override(r, {"platforms": platforms}, "vonage", "call:1")
    assert r["model"] == PRIMARY


def test_empty_chat_id_is_a_no_op(runner):
    r = _route()
    runner._apply_realtime_model_override(r, _config(realtime_model=FAST), "vonage", "")
    assert r["model"] == PRIMARY


def test_call_scoped_inference_route_wins_over_realtime_model(runner, monkeypatch):
    r = _route()
    monkeypatch.setattr(
        "agent.vonage_call_bridge.lookup_call_inference_route",
        lambda chat_id: {
            "model": "openrouter/custom-fast",
            "provider": "openrouter",
            "api_key": "or-test",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "openrouter_provider": {"sort": "latency"},
        }
        if chat_id == "call:1"
        else None,
    )
    runner._apply_realtime_model_override(r, _config(realtime_model=FAST), "vonage", "call:1")
    assert r["model"] == "openrouter/custom-fast"
    assert r["runtime"]["provider"] == "openrouter"
    assert r["runtime"]["api_key"] == "or-test"
    assert r["provider_routing"] == {"sort": "latency"}


def test_default_realtime_model_still_applies_when_no_call_route(runner, monkeypatch):
    r = _route()
    monkeypatch.setattr(
        "agent.vonage_call_bridge.lookup_call_inference_route",
        lambda _chat_id: None,
    )
    runner._apply_realtime_model_override(r, _config(realtime_model=FAST), "vonage", "call:1")
    assert r["model"] == FAST
    assert r["runtime"]["provider"] == "nous"


def test_call_route_replace_updates_signature(runner, monkeypatch):
    r = _route()
    state = {
        "route": {
            "model": "first/model",
            "provider": "openrouter",
            "api_key": "a",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        }
    }

    def _lookup(_chat_id):
        return dict(state["route"])

    monkeypatch.setattr("agent.vonage_call_bridge.lookup_call_inference_route", _lookup)
    runner._apply_realtime_model_override(r, _config(realtime_model=FAST), "vonage", "call:1")
    first_sig = r["signature"]

    state["route"]["model"] = "second/model"
    state["route"]["api_key"] = "b"
    runner._apply_realtime_model_override(r, _config(realtime_model=FAST), "vonage", "call:1")
    assert r["model"] == "second/model"
    assert r["signature"] != first_sig
    assert r["signature"][0] == "second/model"


# -- tool-activity notices ---------------------------------------------------
#
# Tool lifecycle callbacks are installed only for Discord's voice ack and
# Slack's task cards. A live call needs them too, or its audible "still
# working" cue goes silent for exactly the wait it exists to cover: a tool
# that runs after the agent has already spoken.


class _Source:
    def __init__(self, chat_id, platform="vonage"):
        self.chat_id = chat_id
        self.platform = platform


class _CueAdapter:
    def note_tool_activity(self, chat_id, active=True):
        pass


class _PlainAdapter:
    pass


def _runner_with(adapter, platform="vonage"):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {platform: adapter} if adapter is not None else {}
    return runner


@pytest.mark.parametrize(
    "chat_id, wanted",
    [("call:17274854546", True), ("browser:rhett", True), ("17274854546", False)],
    ids=["call", "browser", "sms"],
)
def test_only_realtime_sessions_ask_for_tool_notices(chat_id, wanted):
    runner = _runner_with(_CueAdapter())
    assert runner._wants_realtime_tool_notices(_Source(chat_id)) is wanted


def test_adapter_without_the_hook_is_not_asked():
    runner = _runner_with(_PlainAdapter())
    assert runner._wants_realtime_tool_notices(_Source("call:1")) is False


def test_unknown_platform_is_not_asked():
    runner = _runner_with(_CueAdapter(), platform="vonage")
    assert runner._wants_realtime_tool_notices(_Source("call:1", "telegram")) is False


def test_a_broken_source_never_raises():
    """This runs on the agent thread mid-turn; it must not be able to kill it."""
    runner = _runner_with(_CueAdapter())
    assert runner._wants_realtime_tool_notices(None) is False
