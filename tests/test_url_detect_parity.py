# -*- coding: utf-8 -*-
"""Lightweight parity check for URL trimming used by sandbox hot-links."""

import re

URL_RE = re.compile(r'https?://[^\s<>"\'`)\]}>，。；！？]+', re.I)


def normalize(raw: str) -> str:
    u = (raw or "").strip()
    return re.sub(r"[),.;:!?，。；！？]+$", "", u)


def test_dev_server_local_url():
    line = "  ➜  Local:   http://localhost:5173/"
    m = URL_RE.search(line)
    assert m
    assert normalize(m.group(0)) == "http://localhost:5173/"


def test_trailing_punctuation():
    assert normalize("http://127.0.0.1:8787).") == "http://127.0.0.1:8787"
