"""Session title + persist ownership (worker-thread ContextVar) tests."""

from __future__ import annotations

from pathlib import Path

from metateam.core.config import Settings
from metateam.services.store import (
    SessionStore,
    _extract_skill_title_source,
    _summarize_title,
    is_untitled_session,
)
from metateam.services.tenant_context import set_user


def test_skill_title_from_short_tool_prompt() -> None:
    src = (
        "请立即调用函数工具 `skill_html_deck_editorial`，"
        "task 参数为：做一份 AI 简介幻灯片。"
        "调用后严格按工具返回的流程继续使用 write_file / run_shell 等工具完成任务，"
        "不要只复述 Skill 正文，也不要声称「没有这个技能」。"
    )
    assert "AI 简介" in _extract_skill_title_source(src)
    assert "请立即调用" not in _summarize_title(src)


def test_skill_title_from_slash_display() -> None:
    assert _summarize_title("/skill html-deck-editorial 做 PPT") == "做 PPT"
    assert "Skill" in _summarize_title("/skill html-deck-editorial")


def test_persist_works_without_request_context(tmp_path: Path) -> None:
    """SSE workers lose ContextVar; persist must still write by session.user_id."""
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = Settings(workspace=ws, allow_shell=False, demo_mode=True)
    # Point session files into the temp tree
    settings.root = tmp_path
    settings.data_dir = tmp_path / "data"
    settings.sessions_dir = tmp_path / "sessions"
    settings.ensure_dirs()

    store = SessionStore(settings)
    set_user("u_test_persist", "tester")
    sess = store.create()
    sess.agent.messages.append({"role": "user", "content": "帮我写一个登录页"})
    sess.title = "写登录页"
    sid = sess.id

    # Simulate worker thread: reset tenant context to default
    set_user("default", "")
    path = store.persist(sid)
    assert path, "persist must succeed without request ContextVar"
    assert Path(path).exists()
    raw = Path(path).read_text(encoding="utf-8")
    assert "帮我写一个登录页" in raw
    assert "写登录页" in raw
    assert "u_test_persist" in raw


def test_untitled_helpers() -> None:
    assert is_untitled_session("New chat")
    assert is_untitled_session("新会话")
    assert not is_untitled_session("做一份幻灯片")
