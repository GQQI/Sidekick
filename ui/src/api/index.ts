import type { ModelSetup } from "../types/modelSetup";
import { normalizeModelSetup } from "../types/modelSetup";

export type Health = {
  ok: boolean;
  demo: boolean;
  model: string;
  subagent_model?: string;
  base_url?: string;
  provider?: string;
  main_provider_id?: string;
  subagent_provider_id?: string;
  workspace: string;
  workspace_configured?: boolean;
  thinking_enabled?: boolean;
  reasoning_effort?: string;
  context_limit?: number;
  compress_trigger_ratio?: number;
};

export type SkillItem = {
  name: string;
  tool: string;
  description: string;
  path: string;
  mode: string;
};

/** @deprecated use ModelSetup */
export type ModelConfig = ModelSetup;

export type WorkspaceItem = {
  id: string;
  name: string;
  path: string;
  is_default?: boolean;
};

export type RuntimeEvent = {
  type: string;
  data: Record<string, unknown>;
  ts?: number;
  agent_id?: string;
  parent_id?: string;
};

const BASE = "";

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${url}`, init);
  if (!r.ok) {
    let detail = `${url} failed: ${r.status}`;
    try {
      const body = (await r.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

export const fetchHealth = () => json<Health>("/api/health");
export const createSession = () =>
  json<{ id: string; demo: boolean }>("/api/sessions", { method: "POST" });

export type SessionItem = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  messages: number;
  user_turns?: number;
  demo?: boolean;
  source?: string;
};

export type SessionsPage = {
  items: SessionItem[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
};

export type SessionDetail = {
  id: string;
  title: string;
  messages: { role: string; content: string }[];
  tokens: number;
  limit?: number;
  demo: boolean;
};

export const HISTORY_PAGE_SIZE = 20;

export const fetchSessions = (page = 1, pageSize = HISTORY_PAGE_SIZE) =>
  json<SessionsPage>(
    `/api/sessions?page=${encodeURIComponent(String(page))}&page_size=${encodeURIComponent(String(pageSize))}`,
  );
export const fetchSession = (id: string) => json<SessionDetail>(`/api/sessions/${id}`);
export const stopSession = (id: string) =>
  json<{ status: string }>(`/api/sessions/${id}/stop`, { method: "POST" });
export const truncateSession = (
  id: string,
  keepUserTurns: number,
  opts?: { restoreFiles?: boolean },
) =>
  json<{
    status: string;
    session_id: string;
    keep_user_turns: number;
    messages: number;
    restore_files?: boolean;
    file_undo?: { undone_count?: number; partial?: boolean; errors?: string[] };
  }>(`/api/sessions/${id}/truncate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      keep_user_turns: keepUserTurns,
      restore_files: Boolean(opts?.restoreFiles),
    }),
  });

export const fetchSkills = () => json<SkillItem[]>("/api/skills");
export const fetchSkill = (name: string) =>
  json<{
    name: string;
    tool: string;
    description: string;
    path: string;
    body: string;
    mode?: string;
  }>(`/api/skills/${encodeURIComponent(name)}`);
export const fetchMemory = async () =>
  (await json<{ content: string }>("/api/memory")).content || "";
export const saveMemory = (content: string) =>
  json("/api/memory", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
export const fetchModel = async () =>
  normalizeModelSetup(await json<unknown>("/api/model"));

export const saveModel = async (patch: Partial<ModelSetup>) => {
  const res = await json<{ status: string; config: unknown; note: string }>(
    "/api/model",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    },
  );
  return { ...res, config: normalizeModelSetup(res.config) };
};

export const selectModel = async (
  role: "main" | "subagent" | "compress",
  providerId: string,
  model: string,
) => {
  const res = await json<{ status: string; config: unknown; note: string }>(
    "/api/model/select",
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, provider_id: providerId, model }),
    },
  );
  return { ...res, config: normalizeModelSetup(res.config) };
};

export const fetchWorkspaces = () =>
  json<{
    configured: boolean;
    items: WorkspaceItem[];
    active: { path: string; name: string; configured?: boolean } | null;
  }>("/api/workspaces");
export const createWorkspace = (path: string) =>
  json<{
    status: string;
    configured: boolean;
    active: { path: string; name: string };
    items: WorkspaceItem[];
  }>("/api/workspaces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
export const setWorkspace = (path: string, create = false) =>
  json<{
    status: string;
    configured: boolean;
    active: { path: string; name: string };
    items: WorkspaceItem[];
  }>("/api/workspaces/active", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, create }),
  });

export const browseWorkspace = () =>
  json<{ cancelled: boolean; path: string | null }>("/api/workspaces/browse", {
    method: "POST",
  });

export const deleteSession = (id: string) =>
  json<{ status: string }>(`/api/sessions/${id}`, { method: "DELETE" });

export const saveSession = (id: string) =>
  json<{ status: string; path?: string }>(`/api/sessions/${id}/save`, {
    method: "POST",
  });

export type FsEntry = {
  name: string;
  path: string;
  /** Absolute path on the host machine (when provided by the API). */
  abs_path?: string;
  type: "file" | "dir";
  size?: number | null;
  kind?: string;
};

export type FsList = {
  path: string;
  name: string;
  type: string;
  entries: FsEntry[];
};

export type FileKind =
  | "text"
  | "image"
  | "pdf"
  | "audio"
  | "video"
  | "document"
  | "unsupported";

export type FilePayload = {
  path: string;
  name?: string;
  kind: FileKind | string;
  mime?: string;
  size: number;
  content?: string;
  preview?: string;
  truncated?: boolean;
  editable?: boolean;
  supported?: boolean;
  message?: string;
  raw_url?: string;
};

export const listFiles = (path = ".") =>
  json<FsList>(`/api/files?path=${encodeURIComponent(path)}`);
export type SearchHit = {
  path: string;
  name: string;
  kind: "file" | "dir" | string;
  match: "name" | "content" | string;
  line: number;
  snippet: string;
  /** All matching line numbers (content hits). */
  lines?: number[];
  /** Total matching lines found (may equal lines.length when capped). */
  matchCount?: number;
  /** Per-line snippets for expanded view. */
  snippets?: { line: number; text: string }[];
};
export const searchFiles = (q: string, path = ".") =>
  json<{ query: string; hits: SearchHit[] }>(
    `/api/files/search?q=${encodeURIComponent(q)}&path=${encodeURIComponent(path)}`,
  );
export const uploadFile = async (file: File) => {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${BASE}/api/files/upload`, { method: "POST", body: fd });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(text || `upload failed: ${r.status}`);
  }
  return r.json() as Promise<FilePayload & { uploaded?: boolean }>;
};
export const readFileContent = (path: string) =>
  json<FilePayload>(`/api/files/content?path=${encodeURIComponent(path)}`);
export const fileRawUrl = (path: string) =>
  `/api/files/raw?path=${encodeURIComponent(path)}`;
export const writeFileContent = (path: string, content: string) =>
  json<{ path: string; size: number }>("/api/files/content", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
export const createFsEntry = (path: string, kind: "file" | "dir" = "file") =>
  json<{ path: string; type: string }>("/api/files", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, kind }),
  });
export const deleteFsEntry = (path: string, recursive = false) =>
  json<{ path: string; type: string; deleted: boolean }>(
    `/api/files?path=${encodeURIComponent(path)}&recursive=${recursive ? "true" : "false"}`,
    { method: "DELETE" },
  );
export const renameFsEntry = (path: string, newName: string) =>
  json<{ path: string; from: string; name: string; type: string }>("/api/files/rename", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, new_name: newName }),
  });
export const moveFsEntry = (path: string, destDir: string) =>
  json<{ path: string; from: string; to_dir: string; type: string }>("/api/files/move", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, dest_dir: destDir }),
  });

export const revealFsEntry = (path: string) =>
  json<{ status: string; path: string; abs_path: string; type: string }>(
    "/api/files/reveal",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    },
  );

export const decideApproval = (
  sessionId: string,
  approvalId: string,
  approved: boolean,
  remember = false,
) =>
  json<{ status: string; approval_id: string; approved: boolean; remember?: boolean }>(
    `/api/sessions/${sessionId}/approvals/${approvalId}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, remember }),
    },
  );

export const answerAsk = (
  sessionId: string,
  askId: string,
  choice: string,
  text = "",
  optionLabel = "",
) =>
  json<{ status: string; ask_id: string; choice: string }>(
    `/api/sessions/${sessionId}/asks/${askId}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        choice,
        text,
        option_label: optionLabel,
      }),
    },
  );

export const confirmPlan = (
  sessionId: string,
  planId: string,
  body: {
    approved: boolean;
    summary?: string;
    tasks?: Array<{ id?: string; title: string; detail?: string; status?: string }>;
  },
) =>
  json<{ status: string; plan_id: string; approved: boolean }>(
    `/api/sessions/${sessionId}/plans/${planId}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );

export type ChatHandlers = {
  onEvent: (ev: RuntimeEvent) => void;
  onFinal: (text: string, meta: Record<string, unknown>) => void;
  onError: (msg: string) => void;
  onAbort?: () => void;
};

function parseSseBlock(block: string): RuntimeEvent | null {
  // Normalize CRLF so event:/data: parsing is reliable on Windows servers
  const lines = block.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  let eventName = "";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (!line || line.startsWith(":")) continue; // comments / keep-alives
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    // Support both full envelope {type,data} and bare payloads
    if (typeof parsed.type === "string") {
      return parsed as unknown as RuntimeEvent;
    }
    return {
      type: eventName || "message",
      data: parsed as Record<string, unknown>,
    };
  } catch {
    return {
      type: eventName || "message",
      data: { text: raw, message: raw },
    };
  }
}

export async function streamChat(
  message: string,
  sessionId: string | null,
  handlers: ChatHandlers,
  signal?: AbortSignal,
  mode: "plan" | "agent" = "agent",
): Promise<string | null> {
  let r: Response;
  try {
    r = await fetch(`${BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ message, session_id: sessionId, mode }),
      signal,
    });
  } catch (e) {
    if (signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) {
      handlers.onAbort?.();
      return sessionId;
    }
    throw e;
  }
  if (!r.ok || !r.body) {
    handlers.onError(`chat failed: ${r.status}`);
    return sessionId;
  }

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let sid = sessionId;
  let sawFinal = false;
  let aborted = false;

  const handle = (ev: RuntimeEvent) => {
    const type = ev.type || "message";
    if (type === "session") {
      sid = String(ev.data?.session_id || sid);
      handlers.onEvent({ ...ev, type });
      return;
    }
    if (type === "final") {
      sawFinal = true;
      handlers.onFinal(String(ev.data?.text ?? ""), ev.data || {});
      sid = String(ev.data?.session_id || sid);
      return;
    }
    if (type === "error") {
      handlers.onError(String(ev.data?.message || "error"));
      return;
    }
    handlers.onEvent({ ...ev, type });
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE events are separated by blank lines; servers often use CRLF (\r\n\r\n)
      const parts = buf.split(/\r?\n\r?\n/);
      buf = parts.pop() || "";
      for (const block of parts) {
        const ev = parseSseBlock(block);
        if (ev) handle(ev);
      }
    }
    if (buf.trim()) {
      const ev = parseSseBlock(buf);
      if (ev) handle(ev);
    }
  } catch (e) {
    if (signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) {
      aborted = true;
      handlers.onAbort?.();
    } else {
      throw e;
    }
  }
  if (!sawFinal && !aborted) {
    handlers.onFinal("", { session_id: sid, partial: true });
  }
  return sid;
}
