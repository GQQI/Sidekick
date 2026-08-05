# -*- coding: utf-8 -*-
"""needs_plan is model-driven; tests cover parsing + skill goal extraction."""

from metateam.runtime.plan import (
    _obviously_simple_goal,
    _parse_plan_json,
    extract_plan_goal,
    needs_plan,
    parse_needs_plan_reply,
)


class _FakeLLM:
    def __init__(self, reply: str, *, fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def complete_text(self, system: str, user: str, temperature: float = 0.1) -> str:
        self.calls.append((system, user))
        if self.fail:
            raise RuntimeError("boom")
        return self.reply


def test_parse_needs_plan_reply():
    assert parse_needs_plan_reply('{"plan": true, "reason": "multi-step"}') is True
    assert parse_needs_plan_reply('{"plan": false}') is False
    assert parse_needs_plan_reply('```json\n{"needs_plan": true}\n```') is True
    assert parse_needs_plan_reply("not json") is None


def test_extract_plan_goal_strips_skill_scaffold():
    huge = (
        "### \u3010Skill \u5df2\u6ce8\u5165\u3011demo\n\n"
        "----- SKILL START -----\n"
        + ("x" * 2000)
        + "\n----- SKILL END -----\n\n"
        "\u73b0\u5728\u5f00\u59cb\uff1a\u7528\u8be5 Skill"
    )
    assert extract_plan_goal(huge) == ""

    with_task = (
        "### \u3010Skill \u5df2\u6ce8\u5165\u3011demo\n\n"
        "----- SKILL START -----\nbody\n----- SKILL END -----\n\n"
        "\u7528\u6237\u672c\u6b21\u9644\u52a0\u6307\u4ee4\uff1a\n"
        "build a deck\n\n"
        "\u8bf7\u6309\u8be5 Skill \u7684\u6d41\u7a0b\uff0c\u76f4\u63a5\u9488\u5bf9\u4e0a\u8ff0\u6307\u4ee4\u6267\u884c\u5e76\u7ed9\u51fa\u7ed3\u679c\u3002"
    )
    assert extract_plan_goal(with_task) == "build a deck"
    assert extract_plan_goal("write index.html") == "write index.html"


def test_needs_plan_uses_model_and_skips_empty_goal():
    llm = _FakeLLM('{"plan": true, "reason": "deck"}')
    assert needs_plan(llm, "make an html presentation") is True
    assert llm.calls

    # Short single-file tweaks short-circuit without calling the model.
    llm2 = _FakeLLM('{"plan": true}')
    assert needs_plan(llm2, "fix typo in readme") is False
    assert not llm2.calls

    huge = (
        "### \u3010Skill \u5df2\u6ce8\u5165\u3011demo\n\n"
        "----- SKILL START -----\n"
        + ("x" * 5000)
        + "\n----- SKILL END -----\n\n"
        "\u73b0\u5728\u5f00\u59cb"
    )
    llm3 = _FakeLLM('{"plan": true}')
    assert needs_plan(llm3, huge) is False
    assert not llm3.calls


def test_needs_plan_defaults_false_on_failure():
    assert needs_plan(_FakeLLM("nonsense"), "do something complex please") is False
    assert needs_plan(_FakeLLM("", fail=True), "do something complex please") is False


def test_obviously_simple_goal():
    assert _obviously_simple_goal("改六个图片") is True
    assert _obviously_simple_goal("替换 index.html 里重复的图片") is True
    assert _obviously_simple_goal("从零搭建整站架构并做多模块迁移") is False


def test_parse_plan_json_salvages_broken_verify_command():
    # Nested PowerShell quotes used to break JSON and dump the blob into summary.
    broken = (
        '{\n'
        '  "summary": "修改HTML，使六个图片标签引用六张不同的图片，消除重复",\n'
        '  "shape_contract": {\n'
        '    "reuse": "复用现有的index.html",\n'
        '    "create_only_if": "无需新建",\n'
        '    "config_placement": "直接替换",\n'
        '    "control_flow": "线性步骤",\n'
        '    "why_not_smaller": "无法更简化",\n'
        '    "verify_command": "powershell -Command \\"$html=Get-Content index.html -Raw; '
        "$matches=[regex]::Matches($html,'<img[^>]*src=\\\\\\\"([^\\\\\\\\\\\\\\\"]*)\\\\\\\"')\"\n"
        "  },\n"
        '  "tasks": [\n'
        '    {"title": "定位重复图片", "detail": "检查 src"},\n'
        '    {"title": "替换为六张不同图", "detail": "改六个 img"}\n'
        "  ]\n"
        "}"
    )
    data = _parse_plan_json(broken)
    assert data["summary"] == "修改HTML，使六个图片标签引用六张不同的图片，消除重复"
    assert not str(data.get("summary", "")).startswith("{")
    titles = [t["title"] for t in data.get("tasks") or []]
    assert "定位重复图片" in titles
    assert "替换为六张不同图" in titles
