"""History restore must include tool cards, not only short text turns."""

from __future__ import annotations

from metateam.core.config import Settings
from metateam.services.store import SessionStore
from metateam.services.tenant_context import set_user


def test_ui_messages_includes_tools(tmp_path) -> None:
    set_user("u_hist", "t")
    settings = Settings(workspace=tmp_path, allow_shell=False, demo_mode=True)
    settings.root = tmp_path
    store = SessionStore(settings)
    sess = store.create()
    sess.agent.messages = [
        {"role": "user", "content": "写个页面"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"a.html","content":"<h1>hi</h1>"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "write_file",
            "content": "wrote a.html (10 chars)",
        },
        {"role": "assistant", "content": "已经写好 a.html。"},
    ]

    ui = store.ui_messages(sess)
    roles = [m["role"] for m in ui]
    assert roles == ["user", "tool", "assistant"]
    tool = ui[1]
    assert tool["name"] == "write_file"
    assert tool["args"]["path"] == "a.html"
    assert "wrote a.html" in tool["result"]
    assert ui[2]["content"] == "已经写好 a.html。"


def test_ui_messages_skips_internal_plan_steps(tmp_path) -> None:
    set_user("u_hist2", "t")
    settings = Settings(workspace=tmp_path, allow_shell=False, demo_mode=True)
    store = SessionStore(settings)
    sess = store.create()
    sess.agent.messages = [
        {"role": "user", "content": "目标"},
        {
            "role": "user",
            "content": "[Plan step 1/2] do thing",
            "sidekick_internal": True,
        },
        {"role": "assistant", "content": "步骤完成"},
    ]
    ui = store.ui_messages(sess)
    assert [m["role"] for m in ui] == ["user", "assistant"]
    assert ui[0]["content"] == "目标"
