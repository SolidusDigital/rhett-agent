"""Tool-service top-up URLs for live-call billing recovery.

Used by the Vonage ``service_topup`` tool and optional live-call billing
notifications. Distinct from :mod:`agent.billing_links`, which maps inference
providers onto billing pages for model 402/entitlement failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ServiceTopup:
    label: str
    url: str
    notes: str = ""


def _nous_topup_url() -> str:
    try:
        from hermes_cli.nous_account import nous_portal_topup_url

        return nous_portal_topup_url(None) or "https://portal.nousresearch.com/billing?topup=open"
    except Exception:
        return "https://portal.nousresearch.com/billing?topup=open"


# Aliases the Vonage tool may pass (slug, provider id, env-ish names).
_SERVICE_TOPUPS: dict[str, ServiceTopup] = {
    "openrouter": ServiceTopup(
        "OpenRouter",
        "https://openrouter.ai/settings/credits",
        "Add credits for routed model inference.",
    ),
    "nous": ServiceTopup(
        "Nous Portal",
        _nous_topup_url(),
        "Managed inference and bundled tool credits; same surface as /topup.",
    ),
    "nous-portal": ServiceTopup(
        "Nous Portal",
        _nous_topup_url(),
        "Managed inference and bundled tool credits; same surface as /topup.",
    ),
    "fishaudio": ServiceTopup(
        "Fish Audio",
        "https://fish.audio/app/developers",
        "ASR/TTS sidecar credits for live voice.",
    ),
    "fish": ServiceTopup(
        "Fish Audio",
        "https://fish.audio/app/developers",
        "ASR/TTS sidecar credits for live voice.",
    ),
    "browser_use": ServiceTopup(
        "Nous Portal",
        _nous_topup_url(),
        "Browser automation is billed through Nous managed tools.",
    ),
    "browser-use": ServiceTopup(
        "Nous Portal",
        _nous_topup_url(),
        "Browser automation is billed through Nous managed tools.",
    ),
    "firecrawl": ServiceTopup(
        "Firecrawl",
        "https://firecrawl.dev",
        "Web extract/search credits.",
    ),
    "vonage": ServiceTopup(
        "Vonage",
        "https://dashboard.nexmo.com/settings",
        "Carrier account balance and API settings.",
    ),
}


def get_service_topup(service: str) -> Dict[str, Any]:
    """Return ``{label, url, notes}`` for a tool service slug.

    Unknown services degrade to a readable label with no invented URL.
    """
    key = (service or "").strip().lower().replace(" ", "_")
    hit = _SERVICE_TOPUPS.get(key)
    if hit:
        return {"label": hit.label, "url": hit.url, "notes": hit.notes}
    label = key.replace("_", " ").replace("-", " ").strip().title() or "Service"
    return {"label": label, "url": "", "notes": "No curated top-up link for this service."}


def infer_service_from_billing_block(block: Optional[dict]) -> Optional[str]:
    """Map a structured billing block onto a service_topup slug when possible."""
    if not isinstance(block, dict):
        return None
    provider = str(block.get("provider") or "").strip().lower()
    if block.get("is_nous"):
        return "nous"
    if provider in _SERVICE_TOPUPS:
        return provider
    if provider == "openrouter":
        return "openrouter"
    return None
