"""Hermes-core hooks for Vonage live-call media sessions.

Coordinates with the Vonage plugin media registry (sibling agent) via stable
function names. All imports are best-effort so core gateway tests run without
the plugin installed.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent.service_topup_links import get_service_topup, infer_service_from_billing_block

logger = logging.getLogger(__name__)

_REALTIME_PREFIXES = ("call:", "browser:")

_REGISTRY_MODULE_NAMES = (
    "hermes_plugins.vonage_platform.media.registry",
    "hermes_plugins.vonage.media.registry",
)

_billing_ping_lock = threading.Lock()
_billing_ping_sent: set[tuple[str, str]] = set()


def is_realtime_media_chat_id(chat_id: str) -> bool:
    text = str(chat_id or "")
    return bool(text) and text.startswith(_REALTIME_PREFIXES)


def _import_media_registry():
    for name in _REGISTRY_MODULE_NAMES:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def lookup_call_inference_route(chat_id: str) -> Optional[Dict[str, Any]]:
    """Return the call-scoped inference route dict, if the Vonage registry exposes one."""
    if not is_realtime_media_chat_id(chat_id):
        return None
    registry = _import_media_registry()
    if registry is None:
        return None

    getter = getattr(registry, "get_inference_route", None)
    if callable(getter):
        try:
            route = getter(chat_id)
        except Exception:
            logger.debug("get_inference_route failed for %s", chat_id, exc_info=True)
            route = None
        if route:
            return dict(route)

    session_getter = getattr(registry, "get", None)
    if callable(session_getter):
        try:
            session = session_getter(chat_id)
        except Exception:
            logger.debug("media registry get() failed for %s", chat_id, exc_info=True)
            session = None
        if session is not None:
            route = getattr(session, "inference_route", None)
            if route:
                return dict(route)
    return None


def _openrouter_provider_routing(route: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prefs = route.get("openrouter_provider") or route.get("provider_preferences")
    if not isinstance(prefs, dict) or not prefs:
        return None
    out: Dict[str, Any] = {}
    for key in ("only", "ignore", "order", "sort", "require_parameters", "data_collection"):
        if key in prefs and prefs[key] not in (None, "", [], {}):
            out[key] = prefs[key]
    return out or None


def _provider_routing_signature(routing: Optional[Dict[str, Any]]) -> str:
    """Stable cache key fragment for OpenRouter provider routing prefs."""
    import json

    return json.dumps(routing or {}, sort_keys=True, default=str)


_RUNTIME_REPLACE_KEYS = (
    "provider",
    "requested_provider",
    "api_key",
    "base_url",
    "api_mode",
    "command",
    "credential_pool",
    "max_tokens",
)


def apply_call_inference_route(turn_route: dict, inference: Dict[str, Any]) -> None:
    """Apply a full per-call inference swap into ``turn_route`` (never persisted).

    Replaces (not merges) call-scoped inference fields. Omitted or empty keys
    from the route builder clear any previous value — e.g. switching
    OpenRouter → Nous drops stale ``provider_routing`` prefs.
    """
    model = inference.get("model")
    if model not in (None, ""):
        turn_route["model"] = model

    runtime = turn_route.setdefault("runtime", {})
    for key in _RUNTIME_REPLACE_KEYS:
        val = inference.get(key)
        if val in (None, ""):
            runtime.pop(key, None)
        else:
            runtime[key] = val

    if "args" in inference:
        args = inference.get("args")
        if args:
            runtime["args"] = list(args)
        else:
            runtime.pop("args", None)
    else:
        runtime.pop("args", None)

    provider_routing = _openrouter_provider_routing(inference)
    if provider_routing:
        turn_route["provider_routing"] = provider_routing
    else:
        turn_route.pop("provider_routing", None)

    runtime = turn_route.get("runtime") or {}
    turn_route["signature"] = (
        turn_route.get("model"),
        runtime.get("provider"),
        runtime.get("requested_provider"),
        runtime.get("base_url"),
        runtime.get("api_mode"),
        runtime.get("command"),
        tuple(runtime.get("args") or []),
        _provider_routing_signature(provider_routing),
    )


def set_last_inference_metrics(chat_id: str, metrics: Dict[str, Any]) -> None:
    """Store OpenRouter (or other) inference metrics on the live media session."""
    if not chat_id or not metrics:
        return
    registry = _import_media_registry()
    if registry is None:
        return

    setter = getattr(registry, "set_last_inference_metrics", None)
    if callable(setter):
        try:
            setter(chat_id, dict(metrics))
            return
        except Exception:
            logger.debug("set_last_inference_metrics failed for %s", chat_id, exc_info=True)

    session_getter = getattr(registry, "get", None)
    if callable(session_getter):
        try:
            session = session_getter(chat_id)
        except Exception:
            session = None
        if session is not None:
            session.last_inference_metrics = dict(metrics)


def _openrouter_api_key(explicit: Optional[str] = None) -> str:
    key = (explicit or os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set — add it to ~/.hermes/.env for OpenRouter metrics."
        )
    return key


def fetch_openrouter_generation_metrics(
    generation_id: str,
    *,
    api_key: Optional[str] = None,
    timeout_s: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """Fetch basic metrics from OpenRouter's generation endpoint."""
    gen_id = str(generation_id or "").strip()
    if not gen_id:
        return None
    key = _openrouter_api_key(api_key)
    query = urlencode({"id": gen_id})
    req = Request(
        f"https://openrouter.ai/api/v1/generation?{query}",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    with urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - curated HTTPS endpoint
        import json

        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    tokens = {
        "prompt": data.get("tokens_prompt"),
        "completion": data.get("tokens_completion"),
        "native_prompt": data.get("native_tokens_prompt"),
        "native_completion": data.get("native_tokens_completion"),
    }
    return {
        "generation_id": data.get("id") or gen_id,
        "provider_name": data.get("provider_name"),
        "latency_ms": data.get("latency"),
        "generation_time_ms": data.get("generation_time"),
        "tokens": tokens,
        "cost_usd": data.get("total_cost"),
        "model": data.get("model"),
        "fetched_at": time.time(),
    }


def maybe_record_openrouter_metrics(chat_id: str, agent: Any, result: Optional[dict]) -> None:
    """Best-effort OpenRouter generation metrics for a completed live-call turn."""
    if not is_realtime_media_chat_id(chat_id):
        return
    if not isinstance(result, dict) or result.get("failed"):
        return
    if agent is None:
        return
    try:
        is_openrouter = (
            str(getattr(agent, "provider", "") or "").lower() == "openrouter"
            or bool(getattr(agent, "_is_openrouter_url", lambda: False)())
        )
    except Exception:
        is_openrouter = False
    if not is_openrouter:
        return

    generation_id = getattr(agent, "_last_openrouter_generation_id", None)
    if not generation_id:
        return

    api_key = getattr(agent, "api_key", None) or None

    def _worker() -> None:
        try:
            metrics = fetch_openrouter_generation_metrics(
                generation_id,
                api_key=api_key,
            )
            if metrics:
                set_last_inference_metrics(chat_id, metrics)
        except Exception:
            logger.debug(
                "OpenRouter generation metrics fetch failed for %s",
                chat_id,
                exc_info=True,
            )
        finally:
            try:
                agent._last_openrouter_generation_id = None
            except Exception:
                pass

    threading.Thread(
        target=_worker,
        name=f"or-metrics-{str(chat_id)[:24]}",
        daemon=True,
    ).start()


def _telegram_home_chat_id() -> Optional[str]:
    """Return a validated Telegram home chat id, aligned with the Vonage plugin."""
    home = (os.getenv("TELEGRAM_HOME_CHANNEL") or "").strip()
    if not home:
        try:
            from agent.secret_scope import get_secret

            home = (get_secret("TELEGRAM_HOME_CHANNEL") or "").strip()
        except Exception:
            home = ""
    if not home:
        try:
            from gateway.config import Platform, load_gateway_config

            hc = load_gateway_config().get_home_channel(Platform.TELEGRAM)
            if hc and hc.chat_id:
                home = str(hc.chat_id).strip()
        except Exception:
            home = ""
    if not home or home == "all":
        return None
    try:
        chat_int = int(home)
    except ValueError:
        return home
    if chat_int > 0:
        logger.info(
            "live call billing — TELEGRAM_HOME_CHANNEL must be a group id (negative), not DM %s",
            home,
        )
        return None
    return home


def _infer_billing_service(billing_block: Optional[dict]) -> Optional[str]:
    service = infer_service_from_billing_block(billing_block)
    if service:
        return service
    if not isinstance(billing_block, dict):
        return None
    base_url = str(billing_block.get("base_url") or "").lower()
    if "openrouter" in base_url:
        return "openrouter"
    return None


def maybe_notify_live_call_billing(chat_id: str, result: Optional[dict]) -> None:
    """One Telegram home-channel ping per service per call on billing failures.

    Hook point: gateway turn completion after ``agent_result`` is available.
    """
    if not is_realtime_media_chat_id(chat_id) or not isinstance(result, dict):
        return
    if not result.get("failed"):
        return
    reason = str(result.get("failure_reason") or "")
    billing_block = result.get("billing_block")
    if reason != "billing" and not billing_block:
        return

    service = _infer_billing_service(billing_block)
    if not service:
        logger.info(
            "live call billing for %s — unknown service, skipping home notify",
            chat_id,
        )
        return

    dedupe_key = (str(chat_id), service)
    with _billing_ping_lock:
        if dedupe_key in _billing_ping_sent:
            return

    topup = get_service_topup(service)
    url = topup.get("url") or ""
    label = topup.get("label") or service
    home = _telegram_home_chat_id()
    if not home:
        logger.info(
            "live call billing for %s (%s) — set TELEGRAM_HOME_CHANNEL to notify: %s",
            chat_id,
            service,
            url or label,
        )
        return

    text = f"Live call billing blocked ({label})."
    if url:
        text += f" Top up: {url}"

    def _send() -> None:
        try:
            import json

            from tools.send_message_tool import _handle_send

            payload = _handle_send({"target": f"telegram:{home}", "message": text})
            if isinstance(payload, str) and '"error"' in payload:
                try:
                    parsed = json.loads(payload)
                    if parsed.get("error"):
                        logger.debug(
                            "live-call billing notify failed for %s: %s",
                            chat_id,
                            parsed.get("error"),
                        )
                        return
                except json.JSONDecodeError:
                    pass
            with _billing_ping_lock:
                _billing_ping_sent.add(dedupe_key)
        except Exception:
            logger.debug("live-call billing notify failed for %s", chat_id, exc_info=True)

    threading.Thread(target=_send, name=f"call-billing-{service}", daemon=True).start()
