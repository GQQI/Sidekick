# -*- coding: utf-8 -*-
"""Select Mode attachment protocol (Capability A)."""

from metateam.runtime.browser_protocol import (
    DOM_ELEMENT_KIND,
    DomElementPayload,
    chip_label,
    format_dom_element_for_agent,
    parse_dom_element_json,
)
from metateam.services.browser_sandbox import HOST_KIND


def test_host_kind_is_cdp_playwright():
    assert HOST_KIND == "cdp_playwright"


def test_format_dom_element_for_agent_omits_screenshot():
    payload = DomElementPayload(
        kind=DOM_ELEMENT_KIND,
        url="http://127.0.0.1:5173/",
        tag="button",
        id="cta",
        classes=["primary"],
        inner_text="Sign up",
        xpath="/html/body/button[1]",
        screenshot_base64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
        outer_html="<button id='cta' class='primary'>Sign up</button>",
    )
    text = format_dom_element_for_agent(payload)
    assert "```dom-element" in text
    assert "Sign up" in text
    assert "screenshot_base64" not in text
    assert "修改**源码**" in text or "源码" in text


def test_chip_label_and_parse():
    payload = DomElementPayload(tag="div", id="hero", classes=["wrap", "dark"])
    assert chip_label(payload).startswith("DOM ·")
    assert "#hero" in chip_label(payload)
    raw = payload.model_dump_json()
    parsed = parse_dom_element_json(raw)
    assert parsed is not None
    assert parsed.tag == "div"
    assert parse_dom_element_json("not-json") is None
