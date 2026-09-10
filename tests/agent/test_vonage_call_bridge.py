import json
import time
from unittest.mock import MagicMock, patch

from agent.vonage_call_bridge import (
    _billing_ping_sent,
    apply_call_inference_route,
    fetch_openrouter_generation_metrics,
    lookup_call_inference_route,
    maybe_notify_live_call_billing,
    maybe_record_openrouter_metrics,
)


def test_apply_call_inference_route_swaps_provider_fields():
    route = {
        "model": "moonshotai/kimi-k3",
        "runtime": {
            "provider": "nous",
            "api_key": "old",
            "base_url": "https://inference.example/v1",
            "api_mode": "chat_completions",
            "args": [],
        },
        "signature": ("moonshotai/kimi-k3", "nous", None, "https://inference.example/v1", "chat_completions", None, ()),
    }
    apply_call_inference_route(
        route,
        {
            "model": "anthropic/claude-haiku-4.5",
            "provider": "openrouter",
            "api_key": "or-key",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "openrouter_provider": {"order": ["anthropic"], "sort": "latency"},
        },
    )
    assert route["model"] == "anthropic/claude-haiku-4.5"
    assert route["runtime"]["provider"] == "openrouter"
    assert route["runtime"]["api_key"] == "or-key"
    assert route["provider_routing"] == {"order": ["anthropic"], "sort": "latency"}
    assert route["signature"][0] == "anthropic/claude-haiku-4.5"
    assert route["signature"][1] == "openrouter"
    assert '"sort": "latency"' in route["signature"][7]


def test_apply_call_inference_route_provider_sort_change_busts_signature():
    route = {
        "model": "anthropic/claude-haiku-4.5",
        "runtime": {"provider": "openrouter", "args": []},
        "signature": ("anthropic/claude-haiku-4.5", "openrouter", None, None, None, None, (), "{}"),
    }
    apply_call_inference_route(
        route,
        {
            "model": "anthropic/claude-haiku-4.5",
            "provider": "openrouter",
            "openrouter_provider": {"order": ["anthropic"], "sort": "latency"},
        },
    )
    sig_latency = route["signature"]
    apply_call_inference_route(
        route,
        {
            "model": "anthropic/claude-haiku-4.5",
            "provider": "openrouter",
            "openrouter_provider": {"order": ["anthropic"], "sort": "price"},
        },
    )
    assert route["signature"] != sig_latency


def test_lookup_call_inference_route_uses_registry_getter(monkeypatch):
    fake_registry = MagicMock()
    fake_registry.get_inference_route.return_value = {"model": "fast/model"}
    monkeypatch.setattr(
        "agent.vonage_call_bridge._import_media_registry",
        lambda: fake_registry,
    )
    route = lookup_call_inference_route("call:15551212")
    assert route == {"model": "fast/model"}
    fake_registry.get_inference_route.assert_called_once_with("call:15551212")


def test_fetch_openrouter_generation_metrics_parses_response(monkeypatch):
    payload = {
        "data": {
            "id": "gen-abc",
            "provider_name": "Anthropic",
            "latency": 900,
            "tokens_prompt": 12,
            "tokens_completion": 8,
            "total_cost": 0.002,
            "model": "anthropic/claude-haiku-4.5",
        }
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode()

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("agent.vonage_call_bridge.urlopen", lambda *a, **k: _Resp())

    metrics = fetch_openrouter_generation_metrics("gen-abc")
    assert metrics["provider_name"] == "Anthropic"
    assert metrics["latency_ms"] == 900
    assert metrics["cost_usd"] == 0.002
    assert metrics["tokens"]["prompt"] == 12


def test_apply_call_inference_route_clears_openrouter_prefs_on_nous():
    route = {
        "model": "anthropic/claude-haiku-4.5",
        "runtime": {
            "provider": "openrouter",
            "api_key": "or-key",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
            "args": ["--old"],
        },
        "provider_routing": {"order": ["anthropic"], "sort": "latency"},
    }
    apply_call_inference_route(
        route,
        {
            "model": "moonshotai/kimi-k3",
            "provider": "nous",
            "api_key": "nous-key",
            "base_url": "https://inference.example/v1",
            "api_mode": "chat_completions",
        },
    )
    assert route["model"] == "moonshotai/kimi-k3"
    assert route["runtime"]["provider"] == "nous"
    assert route["runtime"]["api_key"] == "nous-key"
    assert "provider_routing" not in route
    assert "args" not in route["runtime"]
    assert route["signature"][7] == "{}"


def test_maybe_notify_live_call_billing_sends_to_home(monkeypatch):
    _billing_ping_sent.clear()
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-5584119018")
    sent = []

    def _fake_handle_send(args):
        sent.append(args)
        return json.dumps({"success": True})

    monkeypatch.setattr("tools.send_message_tool._handle_send", _fake_handle_send)
    maybe_notify_live_call_billing(
        "call:15551212",
        {
            "failed": True,
            "failure_reason": "billing",
            "billing_block": {"provider": "openrouter"},
        },
    )
    deadline = time.time() + 2.0
    while time.time() < deadline and not sent:
        time.sleep(0.01)
    assert sent == [
        {
            "target": "telegram:-5584119018",
            "message": "Live call billing blocked (OpenRouter). Top up: https://openrouter.ai/settings/credits",
        }
    ]
    assert ("call:15551212", "openrouter") in _billing_ping_sent


def test_maybe_notify_live_call_billing_dedupes_per_call_and_service(monkeypatch):
    _billing_ping_sent.clear()
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-5584119018")
    sent = []

    monkeypatch.setattr(
        "tools.send_message_tool._handle_send",
        lambda args: sent.append(args) or json.dumps({"success": True}),
    )
    result = {
        "failed": True,
        "failure_reason": "billing",
        "billing_block": {"provider": "nous", "is_nous": True},
    }
    maybe_notify_live_call_billing("call:1", result)
    maybe_notify_live_call_billing("call:1", result)
    deadline = time.time() + 2.0
    while time.time() < deadline and len(sent) < 1:
        time.sleep(0.01)
    assert len(sent) == 1


def test_maybe_notify_live_call_billing_failed_send_does_not_dedupe(monkeypatch):
    _billing_ping_sent.clear()
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-5584119018")
    attempts = {"count": 0}

    def _flaky_send(_args):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return json.dumps({"error": "send failed"})
        return json.dumps({"success": True})

    monkeypatch.setattr("tools.send_message_tool._handle_send", _flaky_send)
    result = {
        "failed": True,
        "failure_reason": "billing",
        "billing_block": {"provider": "fishaudio"},
    }
    maybe_notify_live_call_billing("call:retry", result)
    deadline = time.time() + 2.0
    while time.time() < deadline and attempts["count"] < 1:
        time.sleep(0.01)
    assert ("call:retry", "fishaudio") not in _billing_ping_sent

    maybe_notify_live_call_billing("call:retry", result)
    while time.time() < deadline and attempts["count"] < 2:
        time.sleep(0.01)
    assert attempts["count"] == 2
    assert ("call:retry", "fishaudio") in _billing_ping_sent


def test_maybe_notify_live_call_billing_rejects_positive_dm_home(monkeypatch):
    _billing_ping_sent.clear()
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "795544298")
    sent = []

    monkeypatch.setattr(
        "tools.send_message_tool._handle_send",
        lambda args: sent.append(args) or json.dumps({"success": True}),
    )
    maybe_notify_live_call_billing(
        "call:1",
        {"failed": True, "failure_reason": "billing", "billing_block": {"provider": "nous", "is_nous": True}},
    )
    time.sleep(0.05)
    assert sent == []
    assert not _billing_ping_sent


def test_maybe_record_openrouter_metrics_skips_non_media_chat_ids(monkeypatch):
    called = {"value": False}

    def _fake_fetch(*_a, **_k):
        called["value"] = True
        return {}

    monkeypatch.setattr(
        "agent.vonage_call_bridge.fetch_openrouter_generation_metrics",
        _fake_fetch,
    )
    agent = MagicMock()
    agent._is_openrouter_url.return_value = True
    agent._last_openrouter_generation_id = "gen-abc"
    maybe_record_openrouter_metrics("17205551212", agent, {"failed": False})
    assert called["value"] is False
