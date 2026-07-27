/** Client-side tool call summaries for chips / streaming. */

function short(text: string, n = 80) {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length <= n) return t;
  return `${t.slice(0, n - 1)}…`;
}

export function formatToolSummary(name: string, args: unknown, fallback = ""): string {
  if (fallback) return fallback;
  const a =
    args && typeof args === "object" && !Array.isArray(args)
      ? (args as Record<string, unknown>)
      : {};
  const str = (k: string) => (typeof a[k] === "string" ? (a[k] as string) : "");

  if (name === "write_file") {
    const path = str("path") || "（路径待定）";
    const content = str("content");
    return `写入 ${path}${content ? `（${content.length} 字符）` : ""}`;
  }
  if (name === "delete_file") return `删除 ${str("path")}`;
  if (name === "read_file") return str("path") ? `读取 ${str("path")}` : "读取文件";
  if (name === "list_dir") return `列出 ${str("path") || "."}`;
  if (name === "search_text") {
    const q = str("query") || str("pattern");
    return `搜索 “${short(q, 40)}” @ ${str("path") || "."}`;
  }
  if (name === "run_shell") {
    const bg = a.background ? " · 后台" : "";
    return `shell${bg}: ${short(str("command"), 100)}`;
  }
  if (name === "skill_save") return `保存技能 ${str("name")}`;
  if (name === "memory_append") return `追加记忆: ${short(str("note"), 80)}`;
  if (name === "memory_remove") return `删除记忆: ${short(str("match"), 80)}`;
  if (name === "memory_write") return `覆写记忆（${str("content").length} 字符）`;
  if (name === "memory_read") return "读取记忆";
  if (name === "delegate_task") return `委派: ${short(str("goal") || str("task"), 80)}`;
  if (name === "ask_user") return `询问用户: ${short(str("question"), 80)}`;
  if (name.startsWith("skill_")) return `调用技能 ${name}`;
  for (const key of ["path", "command", "query", "name", "goal", "note", "question"]) {
    if (str(key)) return `${name}: ${short(str(key), 80)}`;
  }
  return name || "tool";
}
