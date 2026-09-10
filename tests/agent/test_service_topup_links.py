from agent.service_topup_links import get_service_topup, infer_service_from_billing_block


def test_get_service_topup_known_services():
    or_link = get_service_topup("openrouter")
    assert or_link["label"] == "OpenRouter"
    assert "openrouter.ai/settings/credits" in or_link["url"]

    fish = get_service_topup("fishaudio")
    assert fish["label"] == "Fish Audio"
    assert "fish.audio" in fish["url"]

    browser = get_service_topup("browser_use")
    assert browser["label"] == "Nous Portal"
    assert browser["url"]


def test_get_service_topup_unknown_service():
    unknown = get_service_topup("mystery-widget")
    assert unknown["label"] == "Mystery Widget"
    assert unknown["url"] == ""


def test_infer_service_from_billing_block():
    assert infer_service_from_billing_block({"is_nous": True}) == "nous"
    assert infer_service_from_billing_block({"provider": "openrouter"}) == "openrouter"
