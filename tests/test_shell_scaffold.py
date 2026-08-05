# -*- coding: utf-8 -*-
from metateam.runtime.tools import (
    _has_noninteractive_flags,
    _looks_interactive_scaffold,
)


def test_detect_create_vue():
    assert _looks_interactive_scaffold("npm create vue@latest my-app")
    assert _looks_interactive_scaffold("npm create vite@latest app")
    assert not _looks_interactive_scaffold("npm run dev")


def test_noninteractive_flags():
    assert _has_noninteractive_flags("npm create vue@latest app -- --default")
    assert _has_noninteractive_flags(
        "npm create vite@latest app -- --template vue-ts"
    )
    assert not _has_noninteractive_flags("npm create vue@latest app")
