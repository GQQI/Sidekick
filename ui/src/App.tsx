import { useEffect, useMemo, useRef, useState } from "react";
import {
  browseWorkspace,
  createSession,
  decideApproval,
  answerAsk,
  confirmPlan,
  deleteSession,
  fetchHealth,
  fetchMemory,
  fetchModel,
  fetchSkill,
  fetchSession,
  fetchSessions,
  fetchSkills,
  fetchWorkspaces,
  HISTORY_PAGE_SIZE,
  readFileContent,
  saveMemory,
  saveModel,
  selectModel,
  saveSession,
  setWorkspace,
  stopSession,
  streamChat,
  truncateSession,
  uploadFile,
  writeFileContent,
  searchFiles,
  listFiles,
  type FilePayload,
  type Health,
  type ModelConfig,
  type RuntimeEvent,
  type SessionDetail,
  type SessionItem,
  type SkillItem,
  type WorkspaceItem,
} from "./api";
import { FileExplorer } from "./components/FileExplorer";
import { FileHighlightView } from "./components/FileHighlightView";
import { FileSearchPanel } from "./components/FileSearchPanel";
import { HistoryPanel } from "./components/HistoryPanel";
import { FileTypeIcon, fileCardMeta } from "./components/FileTypeIcon";
import { IconRobotCube } from "./components/IconRobotCube";
import {
  IconAt,
  IconBinoculars,
  IconBraces,
  IconCheck,
  IconClock,
  IconCube,
  IconFiles,
  IconMoon,
  IconPlus,
  IconRobot,
  IconSearch,
  IconSend,
  IconSettings,
  IconSun,
  IconUser,
  IconX,
} from "./components/icons";
import { loadActiveSessionId, saveActiveSessionId } from "./sessionPersist";
import { MarkdownView } from "./components/MarkdownView";
import { usePrefs } from "./prefs";
import { AskDialog } from "./components/AskDialog";
import { SlashMenu } from "./components/SlashMenu";
import {
  AtFileMenu,
  atFileMenuQuery,
  stripTrailingAtQuery,
} from "./components/AtFileMenu";
import { TaskPlanPanel, type ActivePlan, type PlanTask, type PlanTaskStatus } from "./components/TaskPlanPanel";
import { DiffReview } from "./components/DiffReview";
import {
  buildFileDiff,
  isFileMutatingTool,
  toolDiffKey,
  type FileDiffPreview,
} from "./utils/diffPreview";
import { PlanConfirmDialog } from "./components/PlanConfirmDialog";
import { WelcomeGate } from "./components/WelcomeGate";
import type { PlanConfirmState } from "./types/plan";
import { ThinkingBlock } from "./components/ThinkingBlock";
import {
  buildSlashMenuItems,
  formatHelpText,
  parseSlashLine,
  resolveSlashRoute,
  slashMenuQuery,
  type SlashMenuItem,
} from "./slash/commands";
import { formatToolSummary } from "./utils/toolSummary";
import { ModelSettings } from "./components/ModelSettings";
import { ModelSwitcher } from "./components/ModelSwitcher";
import type { ModelSetup, ModelRole } from "./types/modelSetup";
import { modelLabel } from "./types/modelSetup";
import { ThinkTagSplitter, splitThinkTags } from "./utils/thinkTags";
import {
  ASK_CUSTOM_KEY,
  ATTACH_MARKER,
  FS_MUTATING_TOOLS,
  type ApprovalPrompt,
  type AskOption,
  type AskPrompt,
  type ChatMsg,
  type DetailView,
  type LiveLine,
  type MsgAttachment,
  type PendingConfirm,
  type QueuedMsg,
  type SettingsTab,
  type SubNode,
  type SubTool,
  type SubTranscriptItem,
  type ToolCard,
} from "./types/chat";
import {
  buildSuggestions,
  fileToDetail,
  formatArgs,
  formatBytes,
  formatTime,
  greetingKey,
  isSkillInjectMessage,
  langFromPath,
  mapSessionMessages,
  parseUserAttachments,
  softParseToolArgs,
  uid,
  writeFilePreview,
} from "./utils/chatHelpers";

function roleIcon(role: string) {
  const r = role.toLowerCase();
  if (r.includes("research") || r.includes("调研") || r.includes("搜索"))
    return <IconBinoculars size={15} />;
  if (r.includes("code") || r.includes("coder") || r.includes("编码") || r.includes("开发"))
    return <IconBraces size={15} />;
  if (r.includes("tool") || r.includes("工具")) return <IconCube size={15} />;
  return <IconRobot size={15} />;
}

export function App() {
  const { t, locale, theme, setLocale, setTheme } = usePrefs();
  const [health, setHealth] = useState<Health | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<
    {
      id: string;
      name: string;
      path: string;
      kind: string;
      text?: string;
      size?: number;
    }[]
  >([]);
  const [attachBusy, setAttachBusy] = useState(false);
  const attachInputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<LiveLine[]>([]);
  const [subs, setSubs] = useState<SubNode[]>([]);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [memory, setMemory] = useState("");
  const [model, setModel] = useState<ModelSetup | null>(null);
  const [modelSaving, setModelSaving] = useState(false);
  const [modelSwitchRole, setModelSwitchRole] = useState<ModelRole>("main");
  const [workspaces, setWorkspaces] = useState<WorkspaceItem[]>([]);
  const [activeWs, setActiveWs] = useState<{ path: string; name: string } | null>(null);
  const [wsBusy, setWsBusy] = useState(false);
  /** False until health + workspace state are known — avoids welcome flash on refresh. */
  const [bootReady, setBootReady] = useState(false);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsPage, setSessionsPage] = useState(1);
  const [sessionsTotal, setSessionsTotal] = useState(0);
  const [sessionsTotalPages, setSessionsTotalPages] = useState(1);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("workspace");
  const [stats, setStats] = useState({ tokens: 0, iters: 0 });
  const [ctx, setCtx] = useState({ tokens: 0, limit: 48000 });
  const [compressState, setCompressState] = useState<{
    active: boolean;
    message: string;
    attempt: number;
    maxAttempts: number;
    before: number;
    after?: number;
  } | null>(null);
  const [toast, setToast] = useState("");
  const [explorerCollapsed, setExplorerCollapsed] = useState(false);
  const [sidePanel, setSidePanel] = useState<"files" | "search" | "history">("files");
  const [explorerWidth, setExplorerWidth] = useState(280);
  const [detailWidth, setDetailWidth] = useState(420);
  const [fsRefresh, setFsRefresh] = useState(0);
  const [detail, setDetail] = useState<DetailView>(null);
  const [approval, setApproval] = useState<ApprovalPrompt | null>(null);
  const [approvalDiff, setApprovalDiff] = useState<FileDiffPreview | null>(null);
  const [approvalDiffLoading, setApprovalDiffLoading] = useState(false);
  const [detailDiff, setDetailDiff] = useState<FileDiffPreview | null>(null);
  const [detailDiffLoading, setDetailDiffLoading] = useState(false);
  const [askPrompt, setAskPrompt] = useState<AskPrompt | null>(null);
  const [askChoice, setAskChoice] = useState<string>("");
  const [askOtherText, setAskOtherText] = useState("");
  const [askSubmitting, setAskSubmitting] = useState(false);
  const [chatMode, setChatMode] = useState<"plan" | "agent">("agent");
  const [activePlan, setActivePlan] = useState<ActivePlan | null>(null);
  const [planConfirm, setPlanConfirm] = useState<PlanConfirmState | null>(null);
  const [planConfirmSubmitting, setPlanConfirmSubmitting] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [atFileHits, setAtFileHits] = useState<
    { path: string; name: string; kind?: string; match?: string }[]
  >([]);
  const [atFileIndex, setAtFileIndex] = useState(0);
  const [atFileLoading, setAtFileLoading] = useState(false);
  const [queued, setQueued] = useState<QueuedMsg[]>([]);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [editRestorePrompt, setEditRestorePrompt] = useState<{
    msgId: string;
    text: string;
    keepUserTurns: number;
  } | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const stickBottomRef = useRef(true);
  const resizingRef = useRef(false);
  const resizingDetailRef = useRef(false);
  const queuedRef = useRef<QueuedMsg[]>([]);
  const busyRef = useRef(false);
  const sessionIdRef = useRef<string | null>(null);
  const askPendingRef = useRef(false);
  const planPendingRef = useRef(false);

  const transcriptRef = useRef<ChatMsg[]>([]);
  const streamIdRef = useRef<string | null>(null);
  const streamTextRef = useRef("");
  const streamReasoningRef = useRef("");
  /** True once a native reasoning_content / reasoning delta arrived this turn. */
  const nativeReasoningRef = useRef(false);
  const thinkSplitRef = useRef(new ThinkTagSplitter());
  const abortRef = useRef<AbortController | null>(null);
  const stoppingRef = useRef(false);
  const turnDoneRef = useRef(false);

  function commit(next: ChatMsg[]) {
    transcriptRef.current = next;
    setMessages(next);
  }

  function appendMsg(msg: ChatMsg) {
    commit([...transcriptRef.current, msg]);
  }

  function updateMsg(id: string, patch: Partial<ChatMsg>) {
    commit(transcriptRef.current.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }

  /** Keep the open tool detail panel in sync when status/callId advances. */
  function syncToolPanel(tool: ToolCard, prevCallId?: string) {
    setDetail((d) => {
      if (d?.type !== "tool") return d;
      const same =
        d.tool.id === tool.id ||
        d.tool.callId === tool.callId ||
        (prevCallId != null &&
          prevCallId !== "" &&
          d.tool.callId === prevCallId) ||
        (Boolean(d.tool.name) &&
          d.tool.name === tool.name &&
          (d.tool.status === "streaming" ||
            d.tool.status === "running" ||
            d.tool.status === "pending") &&
          (tool.status === "running" ||
            tool.status === "pending" ||
            tool.status === "done" ||
            tool.status === "error"));
      return same ? { type: "tool", tool } : d;
    });
  }

  function findToolMsg(opts: {
    callId?: string;
    name?: string;
    statuses?: ToolCard["status"][];
  }): ChatMsg | undefined {
    const callId = opts.callId || "";
    const name = opts.name || "";
    const statuses = opts.statuses;
    const list = transcriptRef.current;
    for (let i = list.length - 1; i >= 0; i--) {
      const m = list[i];
      if (m.role !== "tool" || !m.tool) continue;
      if (callId && m.tool.callId === callId) return m;
      if (
        name &&
        m.tool.name === name &&
        (!statuses || statuses.includes(m.tool.status))
      ) {
        return m;
      }
    }
    return undefined;
  }

  function setBusyState(v: boolean) {
    busyRef.current = v;
    setBusy(v);
  }

  function setQueuedState(next: QueuedMsg[]) {
    queuedRef.current = next;
    setQueued(next);
  }

  function enqueueMessage(
    text: string,
    opts?: { userDisplay?: string; attachments?: MsgAttachment[] },
  ) {
    const item: QueuedMsg = {
      id: uid(),
      text,
      userDisplay: opts?.userDisplay,
      attachments: opts?.attachments,
    };
    setQueuedState([...queuedRef.current, item]);
    setInput("");
    setToast("已加入队列，当前任务结束后发送。");
  }

  function removeQueued(id: string) {
    setQueuedState(queuedRef.current.filter((q) => q.id !== id));
  }

  function clearQueued() {
    setQueuedState([]);
  }

  function updateSubagentMsg(childId: string, patch: Partial<SubNode>) {
    const hit = transcriptRef.current.find(
      (m) => m.role === "subagent" && m.subagent?.id === childId,
    );
    if (!hit?.subagent) return;
    const subagent = { ...hit.subagent, ...patch };
    updateMsg(hit.id, {
      subagent,
      content: subagent.summary || subagent.goal,
    });
    setSubs((prev) => prev.map((s) => (s.id === childId ? { ...s, ...patch } : s)));
    setDetail((d) =>
      d?.type === "subagent" && d.subagent.id === childId ? { type: "subagent", subagent } : d,
    );
  }

  function patchSubagent(
    childId: string,
    fn: (s: SubNode) => SubNode,
  ) {
    const hit = transcriptRef.current.find(
      (m) => m.role === "subagent" && m.subagent?.id === childId,
    );
    if (!hit?.subagent) return;
    const subagent = fn(hit.subagent);
    updateMsg(hit.id, {
      subagent,
      content: subagent.summary || subagent.goal,
    });
    setSubs((prev) => prev.map((s) => (s.id === childId ? { ...s, ...subagent } : s)));
    setDetail((d) =>
      d?.type === "subagent" && d.subagent.id === childId ? { type: "subagent", subagent } : d,
    );
  }

  function sealSubassistant(transcript: SubTranscriptItem[]): SubTranscriptItem[] {
    const tr = [...transcript];
    const last = tr[tr.length - 1];
    if (last?.kind === "assistant" && last.streaming) {
      tr[tr.length - 1] = { ...last, streaming: false };
    }
    return tr;
  }

  function looksLikeOptionList(text: string): boolean {
    const lines = text
      .split("\n")
      .filter((line) => /^\s*(\d+|[A-Za-z])[\.\)、：]\s*.+/.test(line));
    return lines.length >= 2;
  }

  function discardStreamBubble() {
    const id = streamIdRef.current;
    if (id) {
      commit(transcriptRef.current.filter((m) => m.id !== id));
    }
    streamIdRef.current = null;
    streamTextRef.current = "";
    streamReasoningRef.current = "";
    nativeReasoningRef.current = false;
    thinkSplitRef.current.reset();
  }

  function stripDuplicateAskBubble(question: string) {
    discardStreamBubble();
    const q = question.trim();
    const qHead = q.slice(0, Math.min(80, q.length));
    for (let i = transcriptRef.current.length - 1; i >= 0; i--) {
      const m = transcriptRef.current[i];
      if (m.role !== "assistant") continue;
      if (m.tool) break;
      const text = (m.content || "").trim();
      if (!text) break;
      const sameQuestion = Boolean(
        qHead &&
          (text.includes(qHead) ||
            (text.length >= 24 && q.includes(text.slice(0, Math.min(80, text.length))))),
      );
      if (looksLikeOptionList(text) || sameQuestion) {
        commit(transcriptRef.current.filter((x) => x.id !== m.id));
      }
      break;
    }
  }

  function sealStreamBubble() {
    const id = streamIdRef.current;
    if (!id) return;
    // Flush any buffered partial <think> tag leftovers
    for (const p of thinkSplitRef.current.flush()) {
      if (p.kind === "reasoning") streamReasoningRef.current += p.text;
      else streamTextRef.current += p.text;
    }
    thinkSplitRef.current.reset();
    const text = streamTextRef.current;
    const reasoning = streamReasoningRef.current;
    if (text.trim() || reasoning.trim()) {
      updateMsg(id, {
        content: text,
        reasoning: reasoning || undefined,
        streaming: false,
        reasoningStreaming: false,
      });
    } else {
      commit(transcriptRef.current.filter((m) => m.id !== id));
    }
    streamIdRef.current = null;
    streamTextRef.current = "";
    streamReasoningRef.current = "";
    nativeReasoningRef.current = false;
  }

  function ensureStreamBubble(reset: boolean) {
    if (reset) sealStreamBubble();
    if (!streamIdRef.current) {
      const id = uid();
      streamIdRef.current = id;
      streamTextRef.current = "";
      streamReasoningRef.current = "";
      nativeReasoningRef.current = false;
      thinkSplitRef.current.reset();
      appendMsg({
        id,
        role: "assistant",
        content: "",
        reasoning: "",
        streaming: true,
        reasoningStreaming: false,
      });
    }
  }

  function syncStreamBubble() {
    const id = streamIdRef.current!;
    updateMsg(id, {
      content: streamTextRef.current,
      reasoning: streamReasoningRef.current || undefined,
      streaming: true,
      reasoningStreaming:
        Boolean(streamReasoningRef.current) && !streamTextRef.current.trim(),
    });
  }

  function appendStreamChunk(chunk: string, reset = false, discard = false) {
    if (reset) {
      if (discard) discardStreamBubble();
      else sealStreamBubble();
    }
    if (!chunk) {
      if (!discard) ensureStreamBubble(false);
      return;
    }
    ensureStreamBubble(false);
    // Peel <think>…</think> out of content (models that embed thinking in content).
    // Skip tagged pieces once native reasoning_content has started this turn.
    for (const p of thinkSplitRef.current.feed(chunk)) {
      if (p.kind === "reasoning") {
        if (nativeReasoningRef.current) continue;
        streamReasoningRef.current += p.text;
      } else {
        streamTextRef.current += p.text;
      }
    }
    syncStreamBubble();
  }

  function appendReasoningChunk(chunk: string, reset = false) {
    ensureStreamBubble(reset);
    if (!chunk) return;
    // Native reasoning_content / reasoning field
    nativeReasoningRef.current = true;
    streamReasoningRef.current += chunk;
    syncStreamBubble();
  }

  function finalizeAssistant(text: string, opts?: { stopped?: boolean }) {
    if (turnDoneRef.current) return;
    turnDoneRef.current = true;
    for (const p of thinkSplitRef.current.flush()) {
      if (p.kind === "reasoning") streamReasoningRef.current += p.text;
      else streamTextRef.current += p.text;
    }
    thinkSplitRef.current.reset();
    const id = streamIdRef.current;
    const streamed = streamTextRef.current.trim();
    const incoming = (text || "").trim();
    const placeholder = "（已停止）";
    let body: string;
    if (opts?.stopped) {
      // Prefer already-streamed UI text; never replace it with the stop placeholder
      const existing =
        id
          ? (transcriptRef.current.find((m) => m.id === id)?.content || "").trim()
          : "";
      body =
        streamed ||
        existing ||
        (incoming && incoming !== placeholder ? incoming : "") ||
        placeholder;
    } else {
      body = incoming || streamed;
    }
    let reasoning = streamReasoningRef.current.trim();
    if (/<\/?think/i.test(body)) {
      const peeled = splitThinkTags(body);
      body = peeled.content.trim();
      // Prefer native reasoning; only keep tagged peel when none was streamed.
      if (peeled.reasoning && !nativeReasoningRef.current) {
        reasoning = `${reasoning}${peeled.reasoning}`.trim();
      }
    }
    if (!body && !opts?.stopped) {
      body = t("emptyRoundOutput");
    }
    if (id && transcriptRef.current.some((m) => m.id === id)) {
      updateMsg(id, {
        content: body,
        reasoning: reasoning || undefined,
        streaming: false,
        reasoningStreaming: false,
      });
    } else if (body) {
      appendMsg({
        id: uid(),
        role: "assistant",
        content: body,
        reasoning: reasoning || undefined,
      });
    }
    streamIdRef.current = null;
    streamTextRef.current = "";
    streamReasoningRef.current = "";
    nativeReasoningRef.current = false;
  }

  async function refreshSessions(page?: number) {
    try {
      const target = page ?? sessionsPage;
      const res = await fetchSessions(target, HISTORY_PAGE_SIZE);
      setSessions(res.items || []);
      setSessionsPage(res.page || 1);
      setSessionsTotal(res.total || 0);
      setSessionsTotalPages(res.total_pages || 1);
    } catch {
      /* ignore */
    }
  }

  function openHistoryPanel() {
    setSidePanel("history");
    setExplorerCollapsed(false);
    void refreshSessions(sessionsPage);
  }

  async function refreshWorkspaces() {
    const w = await fetchWorkspaces();
    setWorkspaces(w.items);
    setActiveWs(w.active?.path ? w.active : null);
  }

  function syncContextFromSession(detail: { tokens?: number; limit?: number }) {
    const tokens = Number(detail.tokens ?? 0);
    const limit = Number(detail.limit ?? 0);
    setCtx((c) => ({
      tokens: Number.isFinite(tokens) && tokens >= 0 ? tokens : 0,
      limit: Number.isFinite(limit) && limit > 0 ? limit : c.limit,
    }));
  }

  function resetContextUsage() {
    setCtx((c) => ({ ...c, tokens: 0 }));
  }

  function applySessionDetail(detail: SessionDetail) {
    setSessionId(detail.id);
    syncContextFromSession(detail);
    commit(mapSessionMessages(detail.messages));
    streamIdRef.current = null;
    streamTextRef.current = "";
    streamReasoningRef.current = "";
    nativeReasoningRef.current = false;
    setLive([]);
    setSubs([]);
  }

  async function restoreOrCreateSession(workspacePath: string | null) {
    const tryOpen = async (id: string) => {
      const detail = await fetchSession(id);
      applySessionDetail(detail);
      return true;
    };

    const saved = loadActiveSessionId(workspacePath);
    if (saved) {
      try {
        if (await tryOpen(saved)) return;
      } catch {
        /* deleted or corrupt — fall through */
      }
    }

    try {
      const list = await fetchSessions(1, HISTORY_PAGE_SIZE);
      const hit = (list.items || []).find((s) => (s.user_turns ?? 0) > 0 || s.messages > 0);
      if (hit) {
        try {
          if (await tryOpen(hit.id)) return;
        } catch {
          /* ignore */
        }
      }
    } catch {
      /* ignore */
    }

    const s = await createSession();
    setSessionId(s.id);
    commit([]);
    resetContextUsage();
  }

  async function boot() {
    // Load health + workspace together, then apply in one paint — otherwise
    // setHealth alone makes needsWorkspace true and flashes the welcome gate.
    const [h, w] = await Promise.all([fetchHealth(), fetchWorkspaces()]);
    const wsPath = w.active?.path || null;
    setHealth(h);
    setWorkspaces(w.items);
    setActiveWs(wsPath ? w.active : null);
    setBootReady(true);
    await restoreOrCreateSession(wsPath);
    setSkills(await fetchSkills());
    setMemory(await fetchMemory());
    setModel(await fetchModel());
    await refreshSessions();
  }

  useEffect(() => {
    void boot().catch((e) => {
      setHealth({ ok: false, demo: true, model: "offline", workspace: String(e) });
      setBootReady(true);
    });
  }, []);

  useEffect(() => {
    if (sessionId) saveActiveSessionId(sessionId, activeWs?.path || null);
  }, [sessionId, activeWs?.path]);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    const apply = () => {
      if (mq.matches) setExplorerCollapsed(true);
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  useEffect(() => {
    if (health?.context_limit) {
      setCtx((c) => ({ ...c, limit: health.context_limit || c.limit }));
    }
  }, [health?.context_limit]);

  useEffect(() => {
    if (!stickBottomRef.current) return;
    const el = threadRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    } else {
      bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }
  }, [messages, busy, compressState?.active]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (resizingRef.current) {
        const next = Math.min(520, Math.max(200, e.clientX - 64));
        setExplorerWidth(next);
        setExplorerCollapsed(false);
      }
      if (resizingDetailRef.current) {
        const fromRight = window.innerWidth - e.clientX - 12;
        const next = Math.min(720, Math.max(280, fromRight));
        setDetailWidth(next);
      }
    };
    const onUp = () => {
      resizingRef.current = false;
      resizingDetailRef.current = false;
      document.body.classList.remove("resizing-sidebar");
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "TEXTAREA" ||
          target.tagName === "INPUT" ||
          target.isContentEditable);

      if (e.key === "Escape") {
        if (approval || askPrompt || planConfirm) return;
        if (settingsOpen) {
          setSettingsOpen(false);
          return;
        }
        if (sidePanel === "history" && !explorerCollapsed) {
          setExplorerCollapsed(true);
          return;
        }
        if (detail) {
          setDetail(null);
          return;
        }
        if (input.startsWith("/")) {
          setInput("");
          return;
        }
      }

      if (mod && e.key.toLowerCase() === "n") {
        e.preventDefault();
        void newChat();
        return;
      }
      if (mod && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openHistoryPanel();
        return;
      }
      if (mod && e.key === ",") {
        e.preventDefault();
        openSettings();
        return;
      }
      if (mod && e.key === "/") {
        e.preventDefault();
        composerRef.current?.focus();
        setInput((v) => (v.startsWith("/") ? v : "/"));
        return;
      }
      if (!typing && e.key === "/" && !mod && !e.altKey) {
        e.preventDefault();
        composerRef.current?.focus();
        setInput("/");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approval, askPrompt, settingsOpen, sidePanel, explorerCollapsed, detail, input, sessionsPage]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 5000);
    return () => window.clearTimeout(t);
  }, [toast]);

  function onThreadScroll() {
    const el = threadRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickBottomRef.current = dist < 100;
  }

  const brandSub = useMemo(() => {
    if (!health) return "启动中…";
    if (model?.demo_mode || health.demo) return "Demo";
    const ref = modelSwitchRole === "subagent" ? model?.subagent : model?.main;
    const label = modelLabel(model, ref);
    if (label) return label;
    return health.model || "model";
  }, [health, model, modelSwitchRole]);

  const slashQuery = useMemo(() => slashMenuQuery(input), [input]);
  const slashItems = useMemo(
    () => (slashQuery != null ? buildSlashMenuItems(slashQuery, skills, locale) : []),
    [slashQuery, skills, locale],
  );
  const slashOpen = slashQuery != null;
  const atFileQuery = useMemo(
    () => (slashOpen ? null : atFileMenuQuery(input)),
    [input, slashOpen],
  );
  const atFileOpen = atFileQuery != null;

  useEffect(() => {
    setSlashIndex(0);
  }, [slashQuery]);

  useEffect(() => {
    if (!approval || !isFileMutatingTool(approval.name)) {
      setApprovalDiff(null);
      setApprovalDiffLoading(false);
      return;
    }
    let cancelled = false;
    setApprovalDiff(null);
    setApprovalDiffLoading(true);
    void buildFileDiff(approval.name, approval.args)
      .then((d) => {
        if (!cancelled) setApprovalDiff(d);
      })
      .catch(() => {
        if (!cancelled) setApprovalDiff(null);
      })
      .finally(() => {
        if (!cancelled) setApprovalDiffLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // key captures name/args/callId identity
  }, [approval, approval ? toolDiffKey(approval.name, approval.args, approval.callId) : ""]);

  useEffect(() => {
    if (!detail || detail.type !== "tool" || !isFileMutatingTool(detail.tool.name)) {
      setDetailDiff(null);
      setDetailDiffLoading(false);
      return;
    }
    const tool = detail.tool;
    let cancelled = false;
    setDetailDiff(null);
    setDetailDiffLoading(true);
    void buildFileDiff(tool.name, tool.args)
      .then((d) => {
        if (!cancelled) setDetailDiff(d);
      })
      .catch(() => {
        if (!cancelled) setDetailDiff(null);
      })
      .finally(() => {
        if (!cancelled) setDetailDiffLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    detail,
    detail?.type === "tool"
      ? toolDiffKey(detail.tool.name, detail.tool.args, detail.tool.callId)
      : "",
  ]);

  useEffect(() => {
    setAtFileIndex(0);
  }, [atFileQuery]);

  useEffect(() => {
    if (atFileQuery == null) {
      setAtFileHits([]);
      setAtFileLoading(false);
      return;
    }
    if (!activeWs?.path) {
      setAtFileHits([]);
      return;
    }
    let cancelled = false;
    setAtFileLoading(true);
    const q = atFileQuery.trim();
    const timer = window.setTimeout(() => {
      const run = async () => {
        try {
          if (!q) {
            const listed = await listFiles(".");
            if (cancelled) return;
            const files = (listed.entries || [])
              .filter((e) => e.type === "file")
              .slice(0, 40)
              .map((e) => ({
                path: e.path,
                name: e.name,
                kind: e.kind || "file",
              }));
            setAtFileHits(files);
            return;
          }
          const res = await searchFiles(q, ".");
          if (cancelled) return;
          const files = (res.hits || [])
            .filter((h) => h.kind !== "dir")
            .slice(0, 40)
            .map((h) => ({
              path: h.path,
              name: h.name || h.path.split("/").pop() || h.path,
              kind: h.kind,
              match: h.match,
            }));
          const seen = new Set<string>();
          const uniq = files.filter((f) => {
            if (seen.has(f.path)) return false;
            seen.add(f.path);
            return true;
          });
          setAtFileHits(uniq);
        } catch {
          if (!cancelled) setAtFileHits([]);
        } finally {
          if (!cancelled) setAtFileLoading(false);
        }
      };
      void run();
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [atFileQuery, activeWs?.path]);

  async function applyAtFile(item: { path: string; name: string; kind?: string }) {
    setInput(stripTrailingAtQuery(input));
    if (!activeWs?.path) {
      setToast(t("pickWorkspaceToast"));
      openSettings("workspace");
      return;
    }
    setAttachBusy(true);
    try {
      const data = await readFileContent(item.path);
      const text =
        typeof data.content === "string"
          ? data.content
          : typeof data.preview === "string"
            ? data.preview
            : "";
      setAttachments((prev) => [
        ...prev,
        {
          id: uid(),
          name: data.name || item.name,
          path: data.path || item.path,
          kind: String(data.kind || item.kind || "file"),
          text: text.slice(0, 12000),
          size: data.size,
        },
      ]);
      setToast(t("composerAtAdded"));
    } catch (e) {
      setToast(e instanceof Error ? e.message : String(e));
    } finally {
      setAttachBusy(false);
      requestAnimationFrame(() => composerRef.current?.focus());
    }
  }

  function postSystem(content: string) {
    appendMsg({ id: uid(), role: "system", content });
  }

  function findSkill(query: string): SkillItem | undefined {
    const q = query.trim().toLowerCase();
    if (!q) return undefined;
    return (
      skills.find((s) => s.name.toLowerCase() === q || s.tool.toLowerCase() === q) ||
      skills.find(
        (s) =>
          s.name.toLowerCase().includes(q) ||
          s.tool.toLowerCase().includes(q) ||
          s.tool.toLowerCase().endsWith(`_${q}`),
      )
    );
  }

  async function runSlashCommand(raw: string): Promise<boolean> {
    const parsed = parseSlashLine(raw);
    if (!parsed) return false;
    const { name, args } = parsed;
    if (!name) {
      postSystem(formatHelpText(locale));
      return true;
    }
    const def = resolveSlashRoute(name);
    if (!def) {
      postSystem(`未知命令 \`/${name}\`。输入 \`/help\` 查看可用命令。`);
      return true;
    }

    switch (def.id) {
      case "help":
        postSystem(formatHelpText(locale));
        return true;
      case "new":
        await newChat();
        return true;
      case "stop":
        if (busy) await stopChat();
        else setToast("当前没有进行中的任务");
        return true;
      case "clear": {
        commit([]);
        setLive([]);
        setSubs([]);
        setDetail(null);
        clearQueued();
        setAttachments([]);
        if (sessionId) {
          try {
            // Persist empty transcript so refresh won't restore old messages.
            await truncateSession(sessionId, 0);
            setToast(t("chatCleared"));
          } catch (e) {
            setToast(e instanceof Error ? e.message : String(e));
          }
        } else {
          setToast(t("chatCleared"));
        }
        return true;
      }
      case "history":
        openHistoryPanel();
        return true;
      case "save": {
        if (!sessionId) {
          setToast("当前没有可保存的会话");
          return true;
        }
        try {
          const res = await saveSession(sessionId);
          postSystem(`会话已保存${res.path ? `：\`${res.path}\`` : ""}`);
          await refreshSessions();
        } catch (e) {
          setToast(e instanceof Error ? e.message : String(e));
        }
        return true;
      }
      case "skills": {
        const list = skills.length
          ? skills
          : await fetchSkills().then((s) => {
              setSkills(s);
              return s;
            });
        if (!list.length) {
          postSystem("暂无 Skills。可在 `skills/` 下添加 `SKILL.md`。");
        } else {
          const body = [
            `共 ${list.length} 个 Skill（输入 \`/skill <名称>\` 调用）：`,
            "",
            ...list.map((s) => `- **${s.name}** · \`${s.tool}\`\n  ${s.description || "（无描述）"}`),
          ].join("\n");
          postSystem(body);
        }
        return true;
      }
      case "skill": {
        if (!args) {
          postSystem("用法：`/skill <名称>`。先用 `/skills` 查看列表。");
          return true;
        }
        let sk = findSkill(args);
        if (!sk) {
          try {
            const list = await fetchSkills();
            setSkills(list);
            sk =
              list.find((s) => s.name.toLowerCase() === args.toLowerCase()) ||
              list.find(
                (s) =>
                  s.name.toLowerCase().includes(args.toLowerCase()) ||
                  s.tool.toLowerCase().includes(args.toLowerCase()),
              );
          } catch {
            /* ignore */
          }
        }
        if (!sk) {
          postSystem(`未找到 Skill：\`${args}\`。输入 \`/skills\` 查看全部。`);
          return true;
        }
        try {
          const detail = await fetchSkill(sk.name);
          const body = (detail.body || "").trim();
          const prompt = [
            `### 【Skill 已注入】${detail.name}`,
            "",
            `请立即按下列 Skill 执行（对应工具名：\`${detail.tool}\`）。`,
            `若工具列表中存在 \`${detail.tool}\`，请优先调用它（可带 task 参数）；否则直接严格遵循正文扮演/执行，不要声称「没有这个技能」。`,
            "",
            "----- SKILL START -----",
            "",
            body.slice(0, 24000),
            body.length > 24000 ? "\n…(truncated)" : "",
            "",
            "----- SKILL END -----",
            "",
            "现在开始：用该 Skill 的视角与流程回应用户接下来的需求。若上文已有用户问题，直接针对它输出。",
          ].join("\n");
          if (busyRef.current) {
            enqueueMessage(prompt);
            setToast("Skill 已加入队列，当前任务结束后执行。");
          } else {
            // Show the injected Skill body in the thread (Markdown), same as history restore.
            await sendChat(prompt, { showUser: true });
          }
        } catch (e) {
          postSystem(
            `加载 Skill 失败：${e instanceof Error ? e.message : String(e)}。也可让模型直接调用 \`${sk.tool}\`。`,
          );
          if (busyRef.current) enqueueMessage(`请调用函数工具 ${sk.tool} 并严格执行返回的流程。`);
          else await sendChat(`请调用函数工具 ${sk.tool} 并严格执行返回的流程。`, { showUser: false });
        }
        return true;
      }
      case "memory": {
        const sub = args.toLowerCase();
        if (sub === "edit" || sub === "open") {
          openSettings("memory");
          return true;
        }
        if (sub === "refresh") {
          const text = await fetchMemory();
          setMemory(text);
          setToast("记忆已刷新。");
          return true;
        }
        const text = (await fetchMemory()).trim();
        setMemory(text);
        postSystem(
          text
            ? `当前记忆（MEMORY.md）：\n\n\`\`\`md\n${text.slice(0, 6000)}${text.length > 6000 ? "\n…" : ""}\n\`\`\`\n\n输入 \`/memory edit\` 打开编辑。`
            : "记忆为空。输入 `/memory edit` 打开编辑。",
        );
        return true;
      }
      case "model":
        openSettings("model");
        return true;
      case "workspace":
        openSettings("workspace");
        return true;
      case "runtime":
        openSettings("runtime");
        return true;
      case "stats":
        postSystem(
          [
            `**上下文** ${ctx.tokens} / ${ctx.limit} tokens（{Math.min(100, Math.round((ctx.tokens / Math.max(1, ctx.limit)) * 100))}%）`,
            `**迭代** ${stats.iters}`,
            `**模型** ${health?.model || modelLabel(model, model?.main) || "—"}`,
            `**工作区** ${activeWs?.path || "未选择"}`,
            `**会话** ${sessionId || "—"}`,
          ].join("\n"),
        );
        return true;
      case "files":
        setSidePanel("files");
        setExplorerCollapsed(false);
        setToast("已展开文件浏览器。");
        return true;
      case "settings":
        openSettings(args === "model" ? "model" : args === "memory" ? "memory" : "workspace");
        return true;
      default:
        postSystem(`命令 \`/${def.name}\` 尚未实现。`);
        return true;
    }
  }

  function applySlashItem(item: SlashMenuItem) {
    if (item.needsArgs && item.kind === "command" && !item.args) {
      setInput(item.insert.endsWith(" ") ? item.insert : `${item.insert} `);
      requestAnimationFrame(() => composerRef.current?.focus());
      return;
    }
    const line = item.insert.trim();
    setInput("");
    // Re-focus so the next `/` can open the menu again in the same turn.
    requestAnimationFrame(() => composerRef.current?.focus());
    void (async () => {
      const parsed = parseSlashLine(line);
      // `/skill` shows the injected Skill markdown instead of the raw slash line.
      if (parsed?.name !== "skill") {
        appendMsg({ id: uid(), role: "user", content: line });
      }
      try {
        await runSlashCommand(line);
      } catch (e) {
        postSystem(`命令执行失败：${e instanceof Error ? e.message : String(e)}`);
      }
    })();
  }

  async function newChat() {
    // Instant UI switch — don't wait for stop / session list before clearing.
    const wasBusy = busy;
    clearQueued();
    setAttachments([]);
    streamIdRef.current = null;
    streamTextRef.current = "";
    streamReasoningRef.current = "";
    nativeReasoningRef.current = false;
    commit([]);
    setLive([]);
    setSubs([]);
    setDetail(null);
    resetContextUsage();
    setToast(t("chatStarted"));

    if (wasBusy) {
      // Stop old turn in background; new session id will replace it.
      void stopChat();
    }

    try {
      const s = await createSession();
      setSessionId(s.id);
    } catch (e) {
      setToast(e instanceof Error ? e.message : String(e));
      return;
    }
    void refreshSessions(1);
  }

  async function openSession(id: string) {
    if (id === sessionId && !busy) {
      return;
    }
    if (busy) await stopChat();
    const detailSession = await fetchSession(id);
    applySessionDetail(detailSession);
    {
      const rawTitle = (detailSession.title || "").trim();
      const titleLabel =
        !rawTitle || rawTitle === "新会话" || rawTitle === "New chat" || rawTitle === "Untitled"
          ? t("sessionUntitled")
          : rawTitle;
      setToast(t("historyOpened", titleLabel));
    }
  }

  async function switchWorkspace(path: string, create = false) {
    const res = await setWorkspace(path, create);
    setActiveWs(res.active?.path ? res.active : null);
    setWorkspaces(res.items);
    setHealth(await fetchHealth());
    setFsRefresh((n) => n + 1);
    await newChat();
    setToast(`工作区：${res.active.path}`);
  }

  async function browseAndSetWorkspace() {
    setWsBusy(true);
    try {
      const res = await browseWorkspace();
      if (res.cancelled || !res.path) {
        setToast("已取消选择");
        return;
      }
      await switchWorkspace(res.path);
    } catch (e) {
      setToast(e instanceof Error ? e.message : String(e));
    } finally {
      setWsBusy(false);
    }
  }

  async function removeSession(id: string) {
    await deleteSession(id);
    if (sessionId === id) await newChat();
    const res = await fetchSessions(sessionsPage, HISTORY_PAGE_SIZE);
    if ((res.items || []).length === 0 && sessionsPage > 1) {
      await refreshSessions(sessionsPage - 1);
    } else {
      setSessions(res.items || []);
      setSessionsPage(res.page || 1);
      setSessionsTotal(res.total || 0);
      setSessionsTotalPages(res.total_pages || 1);
    }
    setToast("已删除对话。");
  }

  async function copyBubble(id: string, text: string) {
    const body = (text || "").trim();
    if (!body) {
      setToast("没有可复制的内容");
      return;
    }
    try {
      await navigator.clipboard.writeText(body);
      setCopiedId(id);
      setToast("已复制。");
      window.setTimeout(() => setCopiedId((cur) => (cur === id ? null : cur)), 1600);
    } catch {
      setToast("复制失败");
    }
  }

  function startEditUser(id: string, content: string) {
    if (busyRef.current) {
      setToast("请先等待当前回复结束，或点击停止");
      return;
    }
    setEditingId(id);
    setEditDraft(content);
    setEditRestorePrompt(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditDraft("");
    setEditRestorePrompt(null);
  }

  function requestSubmitEdit(msgId: string) {
    const text = editDraft.trim();
    if (!text) {
      setToast(t("editEmpty"));
      return;
    }
    if (busyRef.current) {
      setToast(t("editBusy"));
      return;
    }
    const list = transcriptRef.current;
    const msgIndex = list.findIndex((m) => m.id === msgId);
    if (msgIndex < 0) return;
    let keepUserTurns = 0;
    for (let i = 0; i < msgIndex; i++) {
      if (list[i].role === "user") keepUserTurns += 1;
    }
    setEditRestorePrompt({ msgId, text, keepUserTurns });
  }

  async function submitEdit(msgId: string, restoreFiles: boolean) {
    const pending = editRestorePrompt;
    const text = (pending?.msgId === msgId ? pending.text : editDraft).trim();
    if (!text) {
      setToast(t("editEmpty"));
      return;
    }
    if (busyRef.current) {
      setToast(t("editBusy"));
      return;
    }
    const list = transcriptRef.current;
    const msgIndex = list.findIndex((m) => m.id === msgId);
    if (msgIndex < 0) return;
    let keepUserTurns =
      pending?.msgId === msgId ? pending.keepUserTurns : 0;
    if (pending?.msgId !== msgId) {
      keepUserTurns = 0;
      for (let i = 0; i < msgIndex; i++) {
        if (list[i].role === "user") keepUserTurns += 1;
      }
    }
    setEditRestorePrompt(null);
    commit(list.slice(0, msgIndex));
    setEditingId(null);
    setEditDraft("");
    setDetail(null);
    setSubs([]);
    setLive([]);
    setApproval(null);
    setAskPrompt(null);
    setAskChoice("");
    setAskOtherText("");
    if (sessionId) {
      try {
        const res = await truncateSession(sessionId, keepUserTurns, {
          restoreFiles,
        });
        if (restoreFiles) {
          setFsRefresh((n) => n + 1);
          const n = res.file_undo?.undone_count ?? 0;
          if (res.file_undo?.partial) {
            setToast(t("editRestorePartial", String(n)));
          } else if (n > 0) {
            setToast(t("editRestoreOk", String(n)));
          } else {
            setToast(t("editRestoreNone"));
          }
        }
      } catch (e) {
        setToast(e instanceof Error ? e.message : String(e));
        return;
      }
    }
    await sendChat(text);
  }

  async function stopChat() {
    const pending = approval;
    const pendingAsk = askPrompt;
    const pendingPlan = planConfirm;
    // Clear UI immediately so the panel cannot stick
    setApproval(null);
    setAskPrompt(null);
    askPendingRef.current = false;
    setAskChoice("");
    setAskOtherText("");
    setPlanConfirm(null);
    planPendingRef.current = false;

    if (!busyRef.current && !abortRef.current) {
      // Chat already idle: only dismiss leftover prompts
      if (pending && sessionId) {
        try {
          await decideApproval(sessionId, pending.approvalId, false, false);
        } catch {
          /* ignore */
        }
        setToast("已取消待确认操作");
      }
      if (pendingAsk && (sessionIdRef.current || sessionId)) {
        try {
          await answerAsk(
            sessionIdRef.current || sessionId!,
            pendingAsk.askId,
            ASK_CUSTOM_KEY,
            "",
          );
        } catch {
          /* ignore */
        }
      }
      if (pendingPlan && (sessionIdRef.current || sessionId || pendingPlan.sessionId)) {
        try {
          await confirmPlan(
            pendingPlan.sessionId || sessionIdRef.current || sessionId!,
            pendingPlan.planId,
            { approved: false },
          );
        } catch {
          /* ignore */
        }
      }
      return;
    }

    stoppingRef.current = true;
    setToast("正在停止…");
    const sid = sessionId;
    // Server cancel also rejects pending approvals — do not double-call decide here
    if (sid) {
      try {
        await stopSession(sid);
      } catch {
        /* ignore */
      }
    }
    abortRef.current?.abort();
  }

  function upsertToolStart(ev: RuntimeEvent) {
    sealStreamBubble();
    const callId = String(ev.data.call_id || uid());
    const name = String(ev.data.name || "tool");
    const pending = Boolean(ev.data.needs_approval);
    const existing =
      findToolMsg({ callId }) ||
      findToolMsg({
        name,
        statuses: ["streaming", "pending"],
      });
    if (existing?.tool) {
      const prevCallId = existing.tool.callId;
      const tool: ToolCard = {
        ...existing.tool,
        callId,
        status: pending ? "pending" : "running",
        args: ev.data.args ?? existing.tool.args,
        name: name || existing.tool.name,
        summary:
          String(ev.data.summary || "") ||
          formatToolSummary(name, ev.data.args ?? existing.tool.args),
      };
      updateMsg(existing.id, { tool });
      syncToolPanel(tool, prevCallId);
      if (name === "write_file") {
        const preview = writeFilePreview(tool.args);
        if (preview) setDetail({ type: "tool", tool });
      }
      return tool;
    }
    const tool: ToolCard = {
      id: uid(),
      callId,
      name,
      args: ev.data.args,
      status: pending ? "pending" : "running",
      summary:
        String(ev.data.summary || "") || formatToolSummary(name, ev.data.args),
    };
    appendMsg({ id: tool.id, role: "tool", content: "", tool });
    syncToolPanel(tool);
    if (name === "write_file") setDetail({ type: "tool", tool });
    return tool;
  }

  function upsertToolDelta(ev: RuntimeEvent) {
    sealStreamBubble();
    const index = Number(ev.data.index ?? 0);
    const streamKey = `stream_${index}`;
    const realId = String(ev.data.id || "");
    const name = String(ev.data.name || "");
    const argsRaw = String(ev.data.arguments || "");
    const args = softParseToolArgs(argsRaw);
    const callId = realId || streamKey;

    const existing =
      findToolMsg({ callId }) ||
      findToolMsg({ callId: streamKey }) ||
      (realId ? findToolMsg({ callId: realId }) : undefined) ||
      transcriptRef.current.find(
        (m) =>
          m.role === "tool" &&
          m.tool?.status === "streaming" &&
          Number(
            (m.tool.args as { _streamIndex?: number } | undefined)?._streamIndex,
          ) === index,
      );

    const summary = formatToolSummary(name || existing?.tool?.name || "", args);
    if (existing?.tool) {
      const prevCallId = existing.tool.callId;
      const tool: ToolCard = {
        ...existing.tool,
        callId,
        name: name || existing.tool.name,
        args: { ...args, _streamIndex: index },
        argsRaw,
        status: "streaming",
        summary,
      };
      updateMsg(existing.id, { tool });
      syncToolPanel(tool, prevCallId);
      if ((name || existing.tool.name) === "write_file") {
        setDetail({ type: "tool", tool });
      }
      return;
    }

    const tool: ToolCard = {
      id: uid(),
      callId,
      name: name || "tool",
      args: { ...args, _streamIndex: index },
      argsRaw,
      status: "streaming",
      summary,
    };
    appendMsg({ id: tool.id, role: "tool", content: "", tool });
    if (name === "write_file" || !name) {
      setDetail({ type: "tool", tool });
    }
  }

  function upsertToolEnd(ev: RuntimeEvent) {
    const callId = String(ev.data.call_id || "");
    const name = String(ev.data.name || "tool");
    const result = String(ev.data.result ?? ev.data.preview ?? "");
    const ok = ev.data.ok !== false && !result.startsWith("ERROR");
    const hit =
      findToolMsg({ callId }) ||
      findToolMsg({
        name,
        statuses: ["running", "streaming", "pending"],
      });
    if (hit?.tool) {
      const prevCallId = hit.tool.callId;
      const tool: ToolCard = {
        ...hit.tool,
        callId: callId || hit.tool.callId,
        name,
        args: ev.data.args ?? hit.tool.args,
        result,
        status: ok ? "done" : "error",
      };
      updateMsg(hit.id, { tool });
      syncToolPanel(tool, prevCallId);
      return;
    }
    const tool: ToolCard = {
      id: uid(),
      callId: callId || uid(),
      name,
      args: ev.data.args,
      result,
      status: ok ? "done" : "error",
    };
    appendMsg({ id: tool.id, role: "tool", content: "", tool });
    syncToolPanel(tool);
  }

  async function addAttachments(files: FileList | null) {
    if (!files || files.length === 0) return;
    if (!activeWs?.path) {
      setToast(t("pickWorkspaceToast"));
      openSettings("workspace");
      return;
    }
    setAttachBusy(true);
    try {
      const next = [...attachments];
      for (const file of Array.from(files)) {
        const uploaded = await uploadFile(file);
        const text =
          typeof uploaded.content === "string"
            ? uploaded.content
            : typeof uploaded.preview === "string"
              ? uploaded.preview
              : "";
        next.push({
          id: uid(),
          name: uploaded.name || file.name,
          path: uploaded.path,
          kind: String(uploaded.kind || "file"),
          text: text.slice(0, 12000),
          size: uploaded.size ?? file.size,
        });
      }
      setAttachments(next);
      setFsRefresh((n) => n + 1);
      setToast(t("composerAttach"));
    } catch (e) {
      setToast(e instanceof Error ? e.message : String(e));
    } finally {
      setAttachBusy(false);
      if (attachInputRef.current) attachInputRef.current.value = "";
    }
  }

  function buildMessageWithAttachments(userText: string) {
    if (attachments.length === 0) return userText;
    const blocks = attachments.map((a) => {
      const head = `### 附件：${a.name}\n路径：\`${a.path}\``;
      if (a.text?.trim()) {
        return `${head}\n\`\`\`\n${a.text.trim()}\n\`\`\``;
      }
      return `${head}\n${t("attachBinary")}`;
    });
    const attachBlock = [
      "用户上传了以下附件，请根据附件内容进行分析与回答。",
      "",
      ...blocks,
    ].join("\n");
    return userText.trim() ? `${userText.trim()}\n\n${attachBlock}` : attachBlock;
  }

  async function send(text?: string) {
    const raw = (text ?? input).trim();
    const hasAttach = attachments.length > 0 && text === undefined;
    if (!raw && !hasAttach) return;

    if (busyRef.current) {
      if (raw === "/stop" || raw.startsWith("/stop ")) {
        setInput("");
        await stopChat();
        return;
      }
      if (raw.startsWith("/")) {
        const parsed = parseSlashLine(raw);
        const localIds = new Set([
          "help",
          "stop",
          "clear",
          "history",
          "save",
          "skills",
          "skill",
          "memory",
          "model",
          "workspace",
          "runtime",
          "stats",
          "files",
          "settings",
          "new",
        ]);
        if (parsed && localIds.has(resolveSlashRoute(parsed.name)?.id || parsed.name)) {
          const def = resolveSlashRoute(parsed.name);
          if (def?.id === "skill") {
            setInput("");
            await runSlashCommand(raw);
            return;
          }
          if (def?.id === "new" || def?.id === "stop") {
            setInput("");
            appendMsg({ id: uid(), role: "user", content: raw });
            await runSlashCommand(raw);
            return;
          }
          setInput("");
          appendMsg({ id: uid(), role: "user", content: raw });
          await runSlashCommand(raw);
          return;
        }
      }
      const attachMeta = hasAttach
        ? attachments.map((a) => ({ name: a.name, path: a.path, kind: a.kind }))
        : undefined;
      const payload = hasAttach ? buildMessageWithAttachments(raw) : raw;
      enqueueMessage(payload, { userDisplay: raw, attachments: attachMeta });
      if (hasAttach) setAttachments([]);
      return;
    }

    if ((raw.startsWith("/") || raw.startsWith("／")) && !hasAttach) {
      const line = raw.replace(/^／/, "/");
      const parsed = parseSlashLine(line);
      setInput("");
      if (parsed?.name !== "skill") {
        appendMsg({ id: uid(), role: "user", content: line });
      }
      try {
        await runSlashCommand(line);
      } catch (e) {
        postSystem(`命令执行失败：${e instanceof Error ? e.message : String(e)}`);
      }
      return;
    }

    const attachMeta = hasAttach
      ? attachments.map((a) => ({ name: a.name, path: a.path, kind: a.kind }))
      : undefined;
    const payload = hasAttach ? buildMessageWithAttachments(raw) : raw;
    setAttachments([]);
    await sendChat(payload, {
      showUser: true,
      userDisplay: raw,
      attachments: attachMeta,
    });
  }

  async function drainQueueSoon() {
    window.setTimeout(() => {
      if (busyRef.current || stoppingRef.current) return;
      const [next, ...rest] = queuedRef.current;
      if (!next) return;
      setQueuedState(rest);
      void sendChat(next.text, {
        showUser: true,
        userDisplay: next.userDisplay,
        attachments: next.attachments,
      });
    }, 40);
  }

  async function sendChat(
    msg: string,
    opts?: {
      showUser?: boolean;
      userDisplay?: string;
      attachments?: MsgAttachment[];
      mode?: "plan" | "agent";
    },
  ) {
    if (!msg || busyRef.current) return;
    if (!activeWs?.path) {
      setToast(t("pickWorkspaceToast"));
      openSettings("workspace");
      return;
    }
    const showUser = opts?.showUser !== false;
    setInput("");
    setBusyState(true);
    stoppingRef.current = false;
    turnDoneRef.current = false;
    stickBottomRef.current = true;
    setLive([]);
    setSubs([]);
    setCompressState(null);
    setActivePlan(null);
    streamIdRef.current = null;
    streamTextRef.current = "";
    streamReasoningRef.current = "";
    nativeReasoningRef.current = false;
    thinkSplitRef.current.reset();
    if (showUser) {
      const displayText =
        opts?.userDisplay !== undefined ? opts.userDisplay : msg;
      appendMsg({
        id: uid(),
        role: "user",
        content: displayText,
        attachments: opts?.attachments,
      });
    }

    const ac = new AbortController();
    abortRef.current = ac;

    const runMode = opts?.mode ?? chatMode;

    try {
      const sid = await streamChat(
        msg,
        sessionId,
        {
          onEvent: (ev: RuntimeEvent) => {
            const type = ev.type;

            const parsePlanTasks = (raw: unknown): PlanTask[] => {
              if (!Array.isArray(raw)) return [];
              const out: PlanTask[] = [];
              raw.forEach((item, i) => {
                const o = item as Record<string, unknown>;
                const title = String(o.title || "").trim();
                if (!title) return;
                out.push({
                  id: String(o.id || `task_${i}`),
                  title,
                  detail: String(o.detail || "").trim() || undefined,
                  status: String(o.status || "pending") as PlanTaskStatus,
                });
              });
              return out;
            };

            if (type === "plan_created") {
              const tasks = parsePlanTasks(ev.data.tasks);
              const summary = String(ev.data.summary || "");
              const planId = String(ev.data.plan_id || "");
              const awaiting =
                Boolean(ev.data.awaiting_confirm) || ev.data.mode === "plan";
              if (awaiting) {
                // Plan mode: only the confirm dialog — never the preview card
                const planSid = String(
                  ev.data.session_id || sessionIdRef.current || sessionId || "",
                );
                planPendingRef.current = true;
                setPlanConfirm({
                  planId,
                  sessionId: planSid,
                  summary,
                  tasks,
                });
                setActivePlan(null);
              } else {
                // Merge with existing progress so a late plan_created does not
                // wipe statuses already applied by plan_step.
                setActivePlan((prev) => {
                  const byId = new Map((prev?.tasks || []).map((t) => [t.id, t.status]));
                  return {
                    planId,
                    summary,
                    mode: "agent",
                    awaitingConfirm: false,
                    tasks: tasks.map((t, i) => ({
                      ...t,
                      status:
                        byId.get(t.id) ||
                        prev?.tasks[i]?.status ||
                        t.status ||
                        "pending",
                    })),
                  };
                });
              }
            }
            if (type === "plan_confirm_request") {
              const tasks = parsePlanTasks(ev.data.tasks);
              const summary = String(ev.data.summary || "");
              const planId = String(ev.data.plan_id || "");
              const planSid = String(
                ev.data.session_id || sessionIdRef.current || sessionId || "",
              );
              planPendingRef.current = true;
              setPlanConfirm({
                planId,
                sessionId: planSid,
                summary,
                tasks,
              });
              setActivePlan(null);
            }
            if (type === "plan_confirm_resolved") {
              planPendingRef.current = false;
              setPlanConfirm((cur) =>
                cur && cur.planId === String(ev.data.plan_id || "") ? null : cur,
              );
              if (!ev.data.approved) {
                setActivePlan(null);
              }
            }
            if (type === "plan_step") {
              const taskId = String(ev.data.task_id || "");
              const status = String(ev.data.status || "running") as PlanTaskStatus;
              const index = Number(ev.data.index);
              const title = String(ev.data.title || "");
              setPlanConfirm(null);
              setActivePlan((prev) => {
                if (!prev) {
                  return {
                    planId: String(ev.data.plan_id || ""),
                    summary: title || t("taskPlanTitle"),
                    mode: "agent",
                    awaitingConfirm: false,
                    tasks: [
                      {
                        id: taskId || `task_${index || 0}`,
                        title: title || `步骤 ${(index || 0) + 1}`,
                        status,
                      },
                    ],
                  };
                }
                const tasks = prev.tasks.map((t, i) => {
                  const hit =
                    (taskId && t.id === taskId) ||
                    (Number.isFinite(index) && i === index);
                  if (!hit) return t;
                  return { ...t, status, title: title || t.title };
                });
                // When a later step starts running, mark earlier unfinished steps done
                if (status === "running" && Number.isFinite(index) && index > 0) {
                  for (let i = 0; i < index; i++) {
                    if (tasks[i]?.status === "pending" || tasks[i]?.status === "running") {
                      tasks[i] = { ...tasks[i], status: "done" };
                    }
                  }
                }
                return {
                  ...prev,
                  mode: "agent",
                  awaitingConfirm: false,
                  tasks,
                };
              });
            }
            if (type === "plan_done") {
              planPendingRef.current = false;
              setPlanConfirm(null);
              // Hide the plan card once the run finishes
              setActivePlan(null);
            }

            // Subagent events share the bus — accumulate into subagent transcript
            if (ev.parent_id) {
              const childId = String(ev.agent_id || "");
              if (!childId) {
                return;
              }
              if (type === "assistant_delta") {
                const reset = Boolean(ev.data.reset);
                const chunk = String(ev.data.chunk ?? ev.data.text ?? "");
                patchSubagent(childId, (s) => {
                  let tr = [...(s.transcript || [])];
                  if (reset) tr = sealSubassistant(tr);
                  const last = tr[tr.length - 1];
                  if (
                    !reset &&
                    last?.kind === "assistant" &&
                    last.streaming
                  ) {
                    tr[tr.length - 1] = {
                      ...last,
                      text: last.text + chunk,
                    };
                  } else if (chunk || reset) {
                    tr = sealSubassistant(tr);
                    tr.push({
                      id: uid(),
                      kind: "assistant",
                      text: chunk,
                      streaming: true,
                    });
                  }
                  return { ...s, transcript: tr, activity: "生成中…" };
                });
              } else if (type === "assistant_reasoning_delta") {
                const chunk = String(ev.data.chunk ?? "");
                patchSubagent(childId, (s) => {
                  let tr = [...(s.transcript || [])];
                  const last = tr[tr.length - 1];
                  if (last?.kind === "assistant" && last.streaming) {
                    tr[tr.length - 1] = {
                      ...last,
                      reasoning: (last.reasoning || "") + chunk,
                      reasoningStreaming: !last.text.trim(),
                    };
                  } else {
                    tr = sealSubassistant(tr);
                    tr.push({
                      id: uid(),
                      kind: "assistant",
                      text: "",
                      reasoning: chunk,
                      streaming: true,
                      reasoningStreaming: true,
                    });
                  }
                  return { ...s, transcript: tr, activity: t("thinkingActivity") };
                });
              } else if (type === "tool_call_delta") {
                const callId = String(ev.data.id || `stream_${ev.data.index ?? 0}`);
                const name = String(ev.data.name || "");
                const argsRaw = String(ev.data.arguments || "");
                const args = softParseToolArgs(argsRaw);
                const summary = formatToolSummary(name, args);
                patchSubagent(childId, (s) => {
                  let tr = sealSubassistant(s.transcript || []);
                  const idx = tr.findIndex(
                    (x) =>
                      x.kind === "tool" &&
                      (x.tool.callId === callId ||
                        (x.tool.status === "streaming" && x.tool.name === name)),
                  );
                  const tool: SubTool = {
                    id: idx >= 0 && tr[idx].kind === "tool" ? tr[idx].tool.id : uid(),
                    callId,
                    name: name || (idx >= 0 && tr[idx].kind === "tool" ? tr[idx].tool.name : "tool"),
                    summary,
                    status: "streaming",
                    args,
                  };
                  if (idx >= 0) tr[idx] = { id: tool.id, kind: "tool", tool };
                  else tr.push({ id: tool.id, kind: "tool", tool });
                  return { ...s, transcript: tr, activity: summary };
                });
              } else if (type === "tool_start") {
                const callId = String(ev.data.call_id || uid());
                const name = String(ev.data.name || "tool");
                const summary =
                  String(ev.data.summary || "") ||
                  formatToolSummary(name, ev.data.args);
                patchSubagent(childId, (s) => {
                  let tr = sealSubassistant(s.transcript || []);
                  const idx = tr.findIndex(
                    (x) =>
                      x.kind === "tool" &&
                      (x.tool.callId === callId ||
                        (x.tool.status === "streaming" &&
                          (x.tool.name === name || !x.tool.name))),
                  );
                  const tool: SubTool = {
                    id: idx >= 0 && tr[idx].kind === "tool" ? tr[idx].tool.id : uid(),
                    callId,
                    name,
                    summary,
                    status: "running",
                    args: ev.data.args,
                  };
                  if (idx >= 0) tr[idx] = { id: tool.id, kind: "tool", tool };
                  else tr.push({ id: tool.id, kind: "tool", tool });
                  return { ...s, transcript: tr, activity: summary };
                });
              } else if (type === "tool_end") {
                const callId = String(ev.data.call_id || "");
                const name = String(ev.data.name || "tool");
                const ok = Boolean(ev.data.ok !== false);
                const result = String(ev.data.result ?? ev.data.preview ?? "");
                const summary =
                  String(ev.data.summary || "") ||
                  formatToolSummary(name, ev.data.args);
                patchSubagent(childId, (s) => {
                  const tr = [...(s.transcript || [])];
                  const idx = tr.findIndex(
                    (x) =>
                      x.kind === "tool" &&
                      (x.tool.callId === callId ||
                        (x.tool.name === name &&
                          (x.tool.status === "running" || x.tool.status === "streaming"))),
                  );
                  const tool: SubTool = {
                    id: idx >= 0 && tr[idx].kind === "tool" ? tr[idx].tool.id : uid(),
                    callId: callId || uid(),
                    name,
                    summary,
                    status: ok ? "done" : "error",
                    args: ev.data.args,
                    result,
                  };
                  if (idx >= 0) tr[idx] = { id: tool.id, kind: "tool", tool };
                  else tr.push({ id: tool.id, kind: "tool", tool });
                  return {
                    ...s,
                    transcript: tr,
                    activity: ok ? `${name} 完成` : `${name} 失败`,
                  };
                });
                if (FS_MUTATING_TOOLS.has(name)) {
                  setFsRefresh((n) => n + 1);
                }
              }
              const label =
                (ev.data.message as string) ||
                `${type}${ev.data.name ? " " + ev.data.name : ""}`;
              setLive((prev) =>
                [...prev, { id: uid(), text: `[子] ${label}`, kind: type }].slice(-120),
              );
              return;
            }

            if (type === "session") {
              const sid = String(ev.data.session_id || "");
              if (sid) {
                sessionIdRef.current = sid;
                setSessionId(sid);
              }
            }

            if (type === "context_usage" || type === "llm_start") {
              const budget = (ev.data.budget || {}) as Record<string, unknown>;
              setCtx((c) => {
                const tokens = Number(ev.data.tokens ?? budget.tokens_est ?? c.tokens);
                const limit = Number(ev.data.limit ?? c.limit);
                return {
                  tokens: Number.isFinite(tokens) ? tokens : c.tokens,
                  limit: Number.isFinite(limit) && limit > 0 ? limit : c.limit,
                };
              });
            }

            if (type === "compress_start" || type === "compress_progress") {
              setCompressState({
                active: true,
                message: String(ev.data.message || "正在快速压缩上下文…"),
                attempt: Number(ev.data.attempt || 0),
                maxAttempts: Number(ev.data.max_attempts || 3),
                before: Number(ev.data.before || ev.data.tokens || 0),
              });
              setCtx((c) => ({
                tokens: Number(ev.data.tokens ?? c.tokens),
                limit: Number(ev.data.limit ?? c.limit),
              }));
            }

            if (type === "compress") {
              const after = Number(ev.data.after || 0);
              const before = Number(ev.data.before || 0);
              setCompressState({
                active: true,
                message: String(ev.data.message || `上下文已重置 ${before}→${after}`),
                attempt: Number((ev.data.meta as { attempts?: number } | undefined)?.attempts || 0),
                maxAttempts: Number(ev.data.max_attempts || 3),
                before,
                after,
              });
              setCtx((c) => ({
                tokens: after || Number(ev.data.tokens || 0),
                limit: Number(ev.data.limit || c.limit),
              }));
              window.setTimeout(() => setCompressState(null), 2200);
            }

            if (type === "assistant_delta") {
              const reset = Boolean(ev.data.reset);
              const discard = Boolean(ev.data.discard);
              const chunk = String(ev.data.chunk ?? ev.data.text ?? "");
              appendStreamChunk(chunk, reset, discard);
            }

            if (type === "assistant_reasoning_delta") {
              const chunk = String(ev.data.chunk ?? ev.data.text ?? "");
              appendReasoningChunk(chunk, false);
            }

            if (type === "tool_call_delta") {
              upsertToolDelta(ev);
            }

            if (type === "approval_request") {
              setApproval({
                approvalId: String(ev.data.approval_id || ""),
                callId: String(ev.data.call_id || ""),
                name: String(ev.data.name || "tool"),
                args: ev.data.args,
                summary: String(ev.data.summary || ev.data.message || ""),
              });
              const callId = String(ev.data.call_id || "");
              const hit =
                findToolMsg({ callId }) ||
                findToolMsg({
                  name: String(ev.data.name || ""),
                  statuses: ["streaming", "running", "pending"],
                });
              if (hit?.tool) {
                const prevCallId = hit.tool.callId;
                const tool: ToolCard = {
                  ...hit.tool,
                  callId: callId || hit.tool.callId,
                  status: "pending",
                  args: ev.data.args ?? hit.tool.args,
                  summary: String(ev.data.summary || ""),
                };
                updateMsg(hit.id, { tool });
                syncToolPanel(tool, prevCallId);
                if (isFileMutatingTool(tool.name)) setDetail({ type: "tool", tool });
              }
            }
            if (type === "approval_resolved") {
              setApproval((cur) =>
                cur && cur.approvalId === String(ev.data.approval_id || "") ? null : cur,
              );
            }

            if (type === "ask_request") {
              const rawOpts = Array.isArray(ev.data.options) ? ev.data.options : [];
              const options: AskOption[] = rawOpts
                .map((o) => {
                  if (!o || typeof o !== "object") return null;
                  const rec = o as Record<string, unknown>;
                  const key = String(rec.key || "").trim();
                  const label = String(rec.label || "").trim();
                  if (!key || !label) return null;
                  return { key, label };
                })
                .filter((o): o is AskOption => Boolean(o));
              const question = String(ev.data.question || "");
              const allowCustom = ev.data.allow_custom !== false;
              const customLabel = String(
                ev.data.custom_label || (locale === "en" ? "Other (type your answer)" : "其他（请补充）"),
              );
              stripDuplicateAskBubble(question);
              const askSid = String(
                ev.data.session_id || sessionIdRef.current || sessionId || "",
              );
              askPendingRef.current = true;
              setAskChoice("");
              setAskOtherText("");
              setAskPrompt({
                askId: String(ev.data.ask_id || ""),
                callId: String(ev.data.call_id || ""),
                sessionId: askSid,
                question,
                options,
                allowCustom,
                customLabel,
                summary: String(ev.data.summary || ev.data.message || ""),
              });
            }
            if (type === "ask_resolved") {
              askPendingRef.current = false;
              setAskPrompt((cur) =>
                cur && cur.askId === String(ev.data.ask_id || "") ? null : cur,
              );
              setAskChoice("");
              setAskOtherText("");
            }

            if (type === "tool_start") {
              upsertToolStart(ev);
            }
            if (type === "tool_end") {
              upsertToolEnd(ev);
              const toolName = String(ev.data.name || "");
              if (FS_MUTATING_TOOLS.has(toolName)) {
                setFsRefresh((n) => n + 1);
              }
            }

            if (type === "subagent_start") {
              const node: SubNode = {
                id: String(ev.data.child_id),
                goal: String(ev.data.goal || ""),
                status: "running",
                role: String(ev.data.role || "leaf"),
                activity: "启动中…",
                transcript: [],
              };
              setSubs((prev) => [...prev, node]);
              appendMsg({
                id: uid(),
                role: "subagent",
                content: node.goal,
                subagent: node,
              });
              // Auto-open detail so progress is visible immediately
              setDetail({ type: "subagent", subagent: node });
            }
            if (type === "subagent_end") {
              const childId = String(ev.data.child_id);
              const summary = String(ev.data.summary || "");
              const cancelled = Boolean(ev.data.cancelled);
              const ok = !cancelled && !summary.startsWith("ERROR");
              patchSubagent(childId, (s) => ({
                ...s,
                status: ok ? "done" : "error",
                summary: cancelled ? summary || "（已停止）" : summary,
                activity: undefined,
                transcript: sealSubassistant(s.transcript || []),
              }));
            }

            const label =
              (ev.data.message as string) ||
              `${type}${ev.data.name ? " " + ev.data.name : ""}`;
            setLive((prev) =>
              [...prev, { id: uid(), text: label, kind: type }].slice(-120),
            );
          },
          onFinal: (textOut, meta) => {
            const stopped = Boolean(meta.cancelled);
            finalizeAssistant(textOut, { stopped });
            if (stopped && String(textOut || "").trim()) {
              setToast(t("stoppedKeep"));
            }
            setStats({
              tokens: Number(meta.tokens || 0),
              iters: Number(meta.iterations || 0),
            });
            if (meta.session_id) setSessionId(String(meta.session_id));
            void fetchSkills().then(setSkills);
            void fetchMemory().then(setMemory);
            void refreshSessions();
            setFsRefresh((n) => n + 1);
          },
          onError: (err) => {
            sealStreamBubble();
            appendMsg({ id: uid(), role: "assistant", content: `错误：${err}` });
            turnDoneRef.current = true;
          },
          onAbort: () => {
            const had = streamTextRef.current.trim();
            finalizeAssistant(had, { stopped: true });
            setToast(had ? t("stoppedKeep") : t("stopped"));
          },
        },
        ac.signal,
        runMode,
      );
      if (sid) setSessionId(sid);
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        appendMsg({
          id: uid(),
          role: "assistant",
          content: `请求失败：${e instanceof Error ? e.message : String(e)}`,
        });
      }
    } finally {
      abortRef.current = null;
      stoppingRef.current = false;
      setBusyState(false);
      setApproval(null);
      if (!askPendingRef.current) {
        setAskPrompt(null);
        setAskChoice("");
        setAskOtherText("");
      }
      if (!planPendingRef.current) {
        setPlanConfirm(null);
      }
      void drainQueueSoon();
    }
  }

  async function applyModel(next?: ModelSetup, opts?: { restartChat?: boolean }) {
    const cfg = next ?? model;
    if (!cfg) return;
    setModelSaving(true);
    try {
      const res = await saveModel({ ...cfg, version: 3 });
      setModel(res.config);
      setHealth(await fetchHealth());
      setToast(res.note);
      if (opts?.restartChat) await newChat();
    } finally {
      setModelSaving(false);
    }
  }

  async function switchModelRole(role: ModelRole, providerId: string, modelId: string) {
    try {
      const res = await selectModel(role, providerId, modelId);
      setModel(res.config);
      setHealth(await fetchHealth());
      setToast(res.note);
    } catch (e) {
      setToast(e instanceof Error ? e.message : String(e));
    }
  }

  function openSettings(tab: SettingsTab = "workspace") {
    setSettingsTab(tab);
    setSettingsOpen(true);
  }

  async function saveDetailFile() {
    if (!detail || detail.type !== "file" || !detail.editable) return;
    setPendingConfirm({
      key: `save-file-${detail.path}`,
      title: "保存并覆盖文件？",
      detail: detail.path,
      confirmLabel: "保存",
      run: async () => {
        if (!detail || detail.type !== "file") return;
        await writeFileContent(detail.path, detail.content);
        setDetail({ ...detail, dirty: false });
        setFsRefresh((n) => n + 1);
        setToast(`已保存 ${detail.path}`);
      },
    });
  }

  async function resolveApproval(approved: boolean, remember = false) {
    if (!approval || !sessionId) return;
    const id = approval.approvalId;
    const toolName = approval.name;
    // Optimistic dismiss — prevents double-submit / stuck panel
    setApproval(null);
    try {
      await decideApproval(sessionId, id, approved, remember);
      if (!approved) setToast(t("approvalRejected"));
      else if (remember) setToast(t("approvalApprovedClass", toolName));
      else setToast(t("approvalApproved"));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // Treat already-resolved as fine (stop / double-click races)
      if (/not found|already resolved/i.test(msg)) {
        if (!approved) setToast("已取消待确认操作");
        return;
      }
      setToast(msg);
    }
  }

  async function resolveAsk(choice: string, otherText = "") {
    const prompt = askPrompt;
    const sid = prompt?.sessionId || sessionIdRef.current || sessionId;
    if (!prompt || !sid || askSubmitting) {
      if (prompt && !sid) setToast("会话未就绪，请稍后重试。");
      return;
    }
    const id = prompt.askId;
    if (!id) {
      setToast("询问已失效，请重新发送消息。");
      return;
    }
    const opt = prompt.options.find((o) => o.key === choice);
    const label = opt?.label || "";
    const text = choice === ASK_CUSTOM_KEY ? otherText.trim() : "";
    setAskSubmitting(true);
    setAskPrompt(null);
    setAskChoice("");
    setAskOtherText("");
    try {
      await answerAsk(sid, id, choice, text, label);
      setToast(t("askAnswered"));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setToast(msg);
      askPendingRef.current = true;
      setAskPrompt(prompt);
      if (choice === ASK_CUSTOM_KEY) setAskOtherText(otherText);
      setAskChoice(choice);
    } finally {
      setAskSubmitting(false);
    }
  }

  async function resolvePlanConfirm(
    approved: boolean,
    draft?: { summary: string; tasks: PlanTask[] },
  ) {
    const prompt = planConfirm;
    const sid = prompt?.sessionId || sessionIdRef.current || sessionId;
    if (!prompt || !sid || planConfirmSubmitting) {
      if (prompt && !sid) setToast("会话未就绪，请稍后重试。");
      return;
    }
    if (!prompt.planId) {
      setToast("方案已失效，请重新发送消息。");
      return;
    }
    const nextSummary = (draft?.summary ?? prompt.summary).trim() || prompt.summary;
    const nextTasks = (draft?.tasks ?? prompt.tasks).map((t) => ({
      ...t,
      status: "pending" as const,
    }));
    setPlanConfirmSubmitting(true);
    setPlanConfirm(null);
    // Seed the progress panel BEFORE awaiting the API — otherwise late SSE
    // plan_step events can arrive during the request and then get wiped by a
    // post-await setActivePlan({…pending}).
    if (approved) {
      setActivePlan({
        planId: prompt.planId,
        summary: nextSummary,
        mode: "agent",
        awaitingConfirm: false,
        tasks: nextTasks,
      });
    } else {
      planPendingRef.current = false;
      setActivePlan(null);
    }
    try {
      await confirmPlan(sid, prompt.planId, {
        approved,
        summary: approved ? nextSummary : undefined,
        tasks: approved
          ? nextTasks.map((t) => ({
              id: t.id,
              title: t.title,
              detail: t.detail || "",
            }))
          : undefined,
      });
      setToast(approved ? t("planApproved") : t("planCancelled"));
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setToast(msg);
      planPendingRef.current = true;
      setPlanConfirm(prompt);
      if (!approved) setActivePlan(null);
    } finally {
      setPlanConfirmSubmitting(false);
    }
  }

  const ctxPct = Math.min(100, Math.round((ctx.tokens / Math.max(1, ctx.limit)) * 100));
  const ctxWarn = ctxPct >= 72;
  const needsWorkspace = bootReady && !activeWs?.path;

  return (
    <div className="shell">
      <div className="wash" aria-hidden />
      <header className="top">
        <div className="top-left">
          <div className="brand">
            <span className="brand-mark brand-mark-anim">
              <IconRobotCube size={30} />
            </span>
            <div className="brand-text">
              <strong>Sidekick</strong>
              <span>{t("tagline")}</span>
            </div>
          </div>
          {Boolean(activeWs?.path) && (
            <>
              <button
                type="button"
                className="chip action iconed"
                onClick={() => openHistoryPanel()}
              >
                <IconClock size={15} />
                <span>{t("history")}</span>
              </button>
              <button type="button" className="chip action iconed" onClick={() => void newChat()}>
                <IconPlus size={15} />
                <span>{t("newChat")}</span>
              </button>
            </>
          )}
        </div>
        <div className="top-right">
          <button
            type="button"
            className="theme-toggle"
            title={theme === "dark" ? t("themeLight") : t("themeDark")}
            aria-label={theme === "dark" ? t("themeLight") : t("themeDark")}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <IconSun /> : <IconMoon />}
          </button>
        </div>
      </header>

      {toast && (
        <div className="toast" onClick={() => setToast("")}>
          {toast}
        </div>
      )}

      {pendingConfirm && (
        <div className="confirm-banner" role="dialog" aria-label="确认操作">
          <div className="confirm-banner-text">
            <strong>{pendingConfirm.title}</strong>
            {pendingConfirm.detail && <span>{pendingConfirm.detail}</span>}
          </div>
          <div className="confirm-banner-actions">
            <button
              type="button"
              className="fe-inline-btn cancel"
              title="取消"
              onClick={() => setPendingConfirm(null)}
            >
              ✕            </button>
            <button
              type="button"
              className="fe-inline-btn ok"
              title={pendingConfirm.confirmLabel || "确认"}
              onClick={() => {
                const action = pendingConfirm;
                setPendingConfirm(null);
                void Promise.resolve(action.run()).catch((e) =>
                  setToast(e instanceof Error ? e.message : String(e)),
                );
              }}
            >
              ✕            </button>
          </div>
        </div>
      )}

      <main className="workbench">
        {!bootReady ? (
          <section className="welcome-gate boot-gate" aria-busy="true" aria-label="Loading">
            <div className="boot-spinner" />
          </section>
        ) : needsWorkspace ? (
          <WelcomeGate
            title={t("welcomeTitle")}
            hint={t("welcomeHint")}
            openLabel={t("openFolder")}
            browsingLabel={t("browsing")}
            recentLabel={t("recentFolders")}
            busy={wsBusy}
            workspaces={workspaces}
            onBrowse={() => void browseAndSetWorkspace()}
            onSelect={(path) => void switchWorkspace(path)}
          />
        ) : (
          <>
        <nav className="activity-rail" aria-label="Sidekick">
          <button
            type="button"
            className="activity-brand"
            title="Sidekick"
            onClick={() => {
              setSidePanel("files");
              setExplorerCollapsed(false);
            }}
          >
            <IconRobotCube size={26} />
          </button>
          <div className="activity-top">
            <button
              type="button"
              className={`activity-btn${sidePanel === "search" && !explorerCollapsed ? " active" : ""}`}
              title={t("navSearch")}
              onClick={() => {
                setSidePanel("search");
                setExplorerCollapsed(false);
              }}
            >
              <IconSearch size={18} />
              <span>{t("navSearch")}</span>
            </button>
            <button
              type="button"
              className={`activity-btn${sidePanel === "files" && !explorerCollapsed ? " active" : ""}`}
              title={t("navFiles")}
              onClick={() => {
                if (sidePanel === "files" && !explorerCollapsed) {
                  setExplorerCollapsed(true);
                } else {
                  setSidePanel("files");
                  setExplorerCollapsed(false);
                }
              }}
            >
              <IconFiles size={18} />
              <span>{t("navFiles")}</span>
            </button>
            <button
              type="button"
              className={`activity-btn${sidePanel === "history" && !explorerCollapsed ? " active" : ""}`}
              title={t("history")}
              onClick={() => {
                if (sidePanel === "history" && !explorerCollapsed) {
                  setExplorerCollapsed(true);
                } else {
                  openHistoryPanel();
                }
              }}
            >
              <IconClock size={18} />
              <span>{t("history")}</span>
            </button>
          </div>
          <div className="activity-bottom">
            <button
              type="button"
              className="activity-btn"
              title={t("navSettings")}
              onClick={() => openSettings()}
            >
              <IconSettings size={18} />
              <span>{t("navSettings")}</span>
            </button>
          </div>
        </nav>

        {!explorerCollapsed && (
          <>
            {sidePanel === "search" ? (
              <div className="side-panel-wrap" style={{ width: explorerWidth }}>
                <FileSearchPanel
                  refreshKey={fsRefresh}
                  onOpenFile={(file, opts) => setDetail(fileToDetail(file, opts))}
                />
              </div>
            ) : sidePanel === "history" ? (
              <div className="side-panel-wrap" style={{ width: explorerWidth }}>
                <HistoryPanel
                  sessions={sessions}
                  activeSessionId={sessionId}
                  page={sessionsPage}
                  totalPages={sessionsTotalPages}
                  total={sessionsTotal}
                  onRefresh={() => void refreshSessions(sessionsPage)}
                  onPageChange={(p) => void refreshSessions(p)}
                  onOpen={(id) => void openSession(id)}
                  onNew={() => void newChat()}
                  onDelete={(id) => removeSession(id)}
                />
              </div>
            ) : (
              <FileExplorer
                rootName={activeWs?.name || "workspace"}
                workspaceAbsPath={activeWs?.path || null}
                collapsed={false}
                width={explorerWidth}
                onToggle={() => setExplorerCollapsed(true)}
                refreshKey={fsRefresh}
                onOpenFile={(file) => setDetail(fileToDetail(file))}
                onDeleted={(path) => {
                  setDetail((d) => {
                    if (d?.type !== "file") return d;
                    if (d.path === path || d.path.startsWith(`${path}/`)) return null;
                    return d;
                  });
                  setFsRefresh((n) => n + 1);
                }}
              />
            )}
            <div
              className="sidebar-resizer"
              onMouseDown={(e) => {
                e.preventDefault();
                resizingRef.current = true;
                document.body.classList.add("resizing-sidebar");
              }}
              title="拖拽调整宽度"
            />
          </>
        )}

        <section className="chat pane">
          {compressState?.active && (
            <div className="compress-banner">
              <div className="compress-banner-text">
                <strong>
                  {compressState.after != null ? t("compressReset") : t("compressResetting")}
                </strong>
                <span>{compressState.message}</span>
              </div>
              <div className="compress-track">
                <div
                  className="compress-fill"
                  style={{
                    width:
                      compressState.after != null
                        ? "100%"
                        : `${Math.min(
                            95,
                            Math.round(
                              (Math.max(1, compressState.attempt) /
                                Math.max(1, compressState.maxAttempts)) *
                                100,
                            ),
                          )}%`,
                  }}
                />
              </div>
              <div className="compress-meta">
                {compressState.after != null
                  ? `${compressState.before} → ${compressState.after}`
                  : `第 ${compressState.attempt || 1}/${compressState.maxAttempts} 轮`}
              </div>
            </div>
          )}
          <div className="thread" ref={threadRef} onScroll={onThreadScroll}>
            {messages.length === 0 && !busy && (
              <div className="empty">
                <div className="empty-hero">
                  <span className="empty-hero-icon">
                    <IconRobotCube size={40} />
                  </span>
                  <h3>{t(greetingKey())}</h3>
                  <p>{t("tagline")}</p>
                </div>
                <div className="suggestions">
                  {buildSuggestions(t).map((s) => (
                    <button key={s.text} type="button" onClick={() => void send(s.text)}>
                      {s.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m) => {
              if (m.role === "tool" && m.tool) {
                const tool = m.tool;
                const active =
                  detail?.type === "tool" && detail.tool.callId === tool.callId;
                const writePrev =
                  tool.name === "write_file" ? writeFilePreview(tool.args) : null;
                if (writePrev?.path && (tool.status === "done" || tool.status === "running" || tool.status === "streaming")) {
                  const meta = fileCardMeta(writePrev.path);
                  return (
                    <button
                      key={m.id}
                      type="button"
                      className={`file-ref-card${active ? " active" : ""}${tool.status !== "done" ? " pending" : ""}`}
                      onClick={() => setDetail({ type: "tool", tool })}
                      title={writePrev.path}
                    >
                      <FileTypeIcon name={meta.name} />
                      <span className="file-ref-body">
                        <strong>{meta.name}</strong>
                        <span>{meta.dir || writePrev.path}</span>
                      </span>
                      {tool.status === "done" ? (
                        <span className="file-ref-status ok">
                          <IconCheck size={14} />
                        </span>
                      ) : (
                        <span className="file-ref-status">…</span>
                      )}
                    </button>
                  );
                }
                const label =
                  tool.summary ||
                  formatToolSummary(tool.name || "", tool.args) ||
                  tool.name ||
                  "tool";
                return (
                  <button
                    key={m.id}
                    type="button"
                    className={`tool-chip ${tool.status}${active ? " active" : ""}`}
                    onClick={() => setDetail({ type: "tool", tool })}
                    title={label}
                  >
                    <span className="tool-chip-mark">
                      {tool.status === "pending"
                        ? "?"
                        : tool.status === "streaming"
                          ? "…"
                          : tool.status === "running"
                            ? "…"
                            : tool.status === "error"
                              ? "!"
                              : "…"}
                    </span>
                    <span className="tool-chip-body">
                      <span className="tool-chip-name">{tool.name || "tool"}</span>
                      <span className="tool-chip-summary">{label}</span>
                    </span>
                    {tool.status === "pending" && (
                      <span className="tool-chip-hint">等待确认</span>
                    )}
                    {tool.status === "streaming" && (
                      <span className="tool-chip-hint">生成中</span>
                    )}
                  </button>
                );
              }
              if (m.role === "subagent" && m.subagent) {
                const s = m.subagent;
                const active =
                  detail?.type === "subagent" && detail.subagent.id === s.id;
                return (
                  <button
                    key={m.id}
                    type="button"
                    className={`subagent-card ${s.status}${active ? " active" : ""}`}
                    onClick={() => setDetail({ type: "subagent", subagent: s })}
                  >
                    <div className="subagent-card-head">
                      <span className="subagent-card-badge">
                        <span className="subagent-card-icon">{roleIcon(s.role || "")}</span>
                        {t("subtaskLabel")}
                        {s.role ? ` · ${s.role}` : ""}
                      </span>
                      <span className="subagent-card-status">
                        {s.status === "running"
                          ? t("subtaskRunning")
                          : s.status === "error"
                            ? t("toolStatusError")
                            : t("toolStatusDone")}
                      </span>
                    </div>
                    <div className="subagent-card-goal">{s.goal}</div>
                    {s.status === "running" && s.activity && (
                      <div className="subagent-card-activity">{s.activity}</div>
                    )}
                    {s.summary && s.status !== "running" && (
                      <pre className="subagent-card-summary">{s.summary}</pre>
                    )}
                  </button>
                );
              }
              return (
                <article
                  key={m.id}
                  className={`bubble ${m.role}${m.streaming ? " streaming" : ""}${
                    editingId === m.id ? " editing" : ""
                  }${m.role === "user" && isSkillInjectMessage(m.content) ? " skill-inject" : ""}`}
                >
                  <div className="bubble-head">
                    <div className="role">
                      <span
                        className={`role-avatar ${
                          m.role === "user" && isSkillInjectMessage(m.content) ? "system" : m.role
                        }`}
                      >
                        {m.role === "user" && isSkillInjectMessage(m.content) ? (
                          <IconCube size={18} />
                        ) : m.role === "user" ? (
                          <IconUser size={18} />
                        ) : m.role === "system" ? (
                          <IconCube size={18} />
                        ) : (
                          <IconRobotCube size={22} />
                        )}
                      </span>
                      <span className="role-label">
                        {m.role === "user" && isSkillInjectMessage(m.content)
                          ? t("skillInjected")
                          : m.role === "user"
                            ? t("you")
                            : m.role === "system"
                              ? t("command")
                              : t("assistant")}
                        {m.streaming
                          ? m.reasoningStreaming
                            ? ` · ${t("thinking")}`
                            : ` · ${t("outputting")}`
                          : ""}
                      </span>
                    </div>
                    {!m.streaming && (m.role === "user" || m.role === "assistant" || m.role === "system") && (
                      <div className="bubble-actions">
                        <button
                          type="button"
                          className="bubble-action"
                          title={t("copy")}
                          onClick={() => void copyBubble(m.id, m.content)}
                        >
                          {copiedId === m.id ? t("copied") : t("copy")}
                        </button>
                        {m.role === "user" &&
                          !isSkillInjectMessage(m.content) &&
                          editingId !== m.id && (
                          <button
                            type="button"
                            className="bubble-action"
                            title={t("edit")}
                            disabled={busy}
                            onClick={() => startEditUser(m.id, m.content)}
                          >
                            {t("edit")}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                  {m.role === "assistant" && (m.reasoning || m.reasoningStreaming) && (
                    <ThinkingBlock
                      content={m.reasoning || ""}
                      streaming={Boolean(m.reasoningStreaming)}
                    />
                  )}
                  {editingId === m.id && m.role === "user" ? (
                    <div className="bubble-edit">
                      <textarea
                        value={editDraft}
                        autoFocus
                        rows={Math.min(12, Math.max(3, editDraft.split("\n").length + 1))}
                        onChange={(e) => setEditDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") {
                            e.preventDefault();
                            cancelEdit();
                          }
                          if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                            e.preventDefault();
                            requestSubmitEdit(m.id);
                          }
                        }}
                      />
                      <div className="bubble-edit-actions">
                        <button
                          type="button"
                          className="bubble-edit-btn cancel"
                          onClick={cancelEdit}
                        >
                          {t("cancel")}
                        </button>
                        <button
                          type="button"
                          className="bubble-edit-btn primary"
                          disabled={!editDraft.trim() || busy}
                          onClick={() => requestSubmitEdit(m.id)}
                        >
                          {t("resend")}
                        </button>
                      </div>
                      <p className="bubble-edit-hint">{t("editHint")}</p>
                    </div>
                  ) : m.role === "assistant" ||
                    m.role === "system" ||
                    (m.role === "user" && isSkillInjectMessage(m.content)) ? (
                    m.content ? (
                      <MarkdownView content={m.content} streaming={m.streaming && !m.reasoningStreaming} />
                    ) : m.streaming && !m.reasoningStreaming ? (
                      <div className="plain muted">…</div>
                    ) : null
                  ) : (
                    <div className="user-bubble-body">
                      {m.attachments && m.attachments.length > 0 && (
                        <ul className="bubble-attach-list">
                          {m.attachments.map((a) => (
                            <li key={`${a.path}:${a.name}`}>
                              <button
                                type="button"
                                className="bubble-attach-chip"
                                title={a.path}
                                onClick={() => {
                                  void readFileContent(a.path)
                                    .then((file) => setDetail(fileToDetail(file)))
                                    .catch((err) =>
                                      setToast(
                                        err instanceof Error ? err.message : String(err),
                                      ),
                                    );
                                }}
                              >
                                <FileTypeIcon name={a.name} />
                                <span className="attach-chip-text">
                                  <strong>{a.name}</strong>
                                  <em>{a.path}</em>
                                </span>
                              </button>
                            </li>
                          ))}
                        </ul>
                      )}
                      {m.content ? <div className="plain">{m.content}</div> : null}
                    </div>
                  )}
                </article>
              );
            })}
            {busy && !messages.some((m) => m.streaming) && (
              <div className="typing-row">
                <span className="typing">
                  {stoppingRef.current
                    ? t("stoppingNow")
                    : queued.length
                      ? t("busyWorkingQueued", String(queued.length))
                      : t("busyWorking")}
                </span>
                <button type="button" className="stop-btn" onClick={() => void stopChat()}>
                  {t("stop")}
                </button>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
          {queued.length > 0 && (
            <div className="message-queue">
              <div className="message-queue-head">
                <span>{t("queuedTitle", String(queued.length))}</span>
                <button type="button" className="mini ghost" onClick={clearQueued}>
                  {t("queueClear")}
                </button>
              </div>
              <ul className="message-queue-list">
                {queued.map((q, i) => (
                  <li key={q.id}>
                    <span className="message-queue-idx">{i + 1}</span>
                    <span className="message-queue-text">
                      {q.userDisplay ||
                        (q.attachments?.length
                          ? q.attachments.map((a) => a.name).join(locale === "en" ? ", " : "、")
                          : q.text)}
                      {q.attachments && q.attachments.length > 0
                        ? ` · ${q.attachments.length}${locale === "en" ? " files" : " 个附件"}`
                        : ""}
                    </span>
                    <button
                      type="button"
                      className="fe-inline-btn cancel"
                      title={t("queueRemove")}
                      onClick={() => removeQueued(q.id)}
                    >
                      ✕                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {activePlan &&
            activePlan.mode === "agent" &&
            !planConfirm &&
            activePlan.tasks.some(
              (t) => t.status === "pending" || t.status === "running",
            ) && (
            <TaskPlanPanel
              plan={activePlan}
              titleLabel={t("taskPlanTitle")}
              subtitle={t(
                "taskPlanProgress",
                String(activePlan.tasks.filter((x) => x.status === "done").length),
                String(activePlan.tasks.length),
              )}
              collapseLabel={t("planCollapse")}
              expandLabel={t("planExpand")}
            />
          )}
          {planConfirm && (
            <PlanConfirmDialog
              key={planConfirm.planId}
              summary={planConfirm.summary}
              tasks={planConfirm.tasks}
              titleLabel={t("planConfirmNeeded")}
              dialogLabel={t("planDialog")}
              approveLabel={t("planApprove")}
              rejectLabel={t("planReject")}
              addTaskLabel={t("planAddTask")}
              submitting={planConfirmSubmitting}
              onApprove={(draft) => void resolvePlanConfirm(true, draft)}
              onReject={() => void resolvePlanConfirm(false)}
            />
          )}
          {approval && (
            <div className="inline-approval" role="dialog" aria-label={t("approvalDialog")}>
              <div className="inline-approval-top">
                <div className="inline-approval-head">
                  <div>
                    <div className="inline-approval-title">{t("approvalNeeded")}</div>
                    <div className="inline-approval-tool">{approval.name}</div>
                  </div>
                  <button
                    type="button"
                    className="icon-btn inline-approval-dismiss"
                    title={t("approvalDismiss")}
                    onClick={() => void resolveApproval(false)}
                  >
                    ✕                  </button>
                </div>
                <p className="inline-approval-summary">{approval.summary}</p>
              </div>
              {isFileMutatingTool(approval.name) ? (
                <div className="inline-approval-preview">
                  <DiffReview
                    diff={approvalDiff}
                    loading={approvalDiffLoading}
                    compact
                    title={t("diffPreview")}
                    newFileLabel={t("diffNewFile")}
                    truncatedLabel={t("diffTruncated")}
                    emptyLabel={t("diffEmpty")}
                    alreadyAppliedLabel={t("diffAlreadyApplied")}
                    snippetLabel={t("diffSnippet")}
                  />
                </div>
              ) : (
                <pre className="code-fence inline-approval-args">
                  {formatArgs(approval.args)}
                </pre>
              )}
              <div className="inline-approval-actions">
                <button
                  type="button"
                  className="approval-btn reject"
                  onClick={() => void resolveApproval(false)}
                >
                  {t("approvalReject")}
                </button>
                <button
                  type="button"
                  className="approval-btn once"
                  onClick={() => void resolveApproval(true, false)}
                >
                  {t("approvalOnce")}
                </button>
                <button
                  type="button"
                  className="approval-btn allow"
                  onClick={() => void resolveApproval(true, true)}
                >
                  {t("approvalAllowClass")}
                </button>
              </div>
            </div>
          )}
          {askPrompt && (
            <AskDialog
              question={askPrompt.question}
              options={askPrompt.options}
              allowCustom={askPrompt.allowCustom}
              customLabel={askPrompt.customLabel}
              choice={askChoice}
              otherText={askOtherText}
              submitting={askSubmitting}
              titleLabel={t("askNeeded")}
              dialogLabel={t("askDialog")}
              submitLabel={t("askSubmit")}
              otherPlaceholder={t("askOtherPlaceholder")}
              onPick={(key) => {
                setAskChoice(key);
                void resolveAsk(key);
              }}
              onOtherChange={(text) => {
                setAskChoice(ASK_CUSTOM_KEY);
                setAskOtherText(text);
              }}
              onOtherFocus={() => setAskChoice(ASK_CUSTOM_KEY)}
              onSubmitCustom={() => void resolveAsk(ASK_CUSTOM_KEY, askOtherText)}
            />
          )}
          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <div className="composer-row">
            <div className="composer-input-wrap">
              {slashOpen && (
                <SlashMenu
                  items={slashItems}
                  activeIndex={Math.min(slashIndex, Math.max(0, slashItems.length - 1))}
                  onHover={setSlashIndex}
                  onSelect={applySlashItem}
                />
              )}
              {atFileOpen && !slashOpen && (
                <AtFileMenu
                  items={atFileHits}
                  activeIndex={Math.min(atFileIndex, Math.max(0, atFileHits.length - 1))}
                  loading={atFileLoading}
                  emptyLabel={t("atFileEmpty")}
                  menuLabel={t("atFileMenu")}
                  onHover={setAtFileIndex}
                  onSelect={(item) => void applyAtFile(item)}
                />
              )}
              <div className="composer-input-inner">
              <textarea
                ref={composerRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onCompositionEnd={(e) => {
                  // IME 确认后再次同步，避免中文输入法吞字。/
                  setInput(e.currentTarget.value);
                }}
                placeholder={busy ? t("composerBusy") : t("composerPlaceholder")}
                rows={3}
                onKeyDown={(e) => {
                  // Force-open slash menu when user types /
                  if (
                    (e.key === "/" || e.key === "／") &&
                    !e.ctrlKey &&
                    !e.metaKey &&
                    !e.altKey &&
                    !input.trim()
                  ) {
                    // Let the character insert; React state will open menu
                    return;
                  }
                  if (slashOpen && slashItems.length > 0) {
                    if (e.key === "ArrowDown") {
                      e.preventDefault();
                      setSlashIndex((i) => (i + 1) % slashItems.length);
                      return;
                    }
                    if (e.key === "ArrowUp") {
                      e.preventDefault();
                      setSlashIndex(
                        (i) => (i - 1 + slashItems.length) % slashItems.length,
                      );
                      return;
                    }
                    if (e.key === "Tab") {
                      e.preventDefault();
                      applySlashItem(
                        slashItems[Math.min(slashIndex, slashItems.length - 1)],
                      );
                      return;
                    }
                    if (e.key === "Escape") {
                      e.preventDefault();
                      setInput("");
                      return;
                    }
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      const cur = input.trim();
                      if (/\s/.test(cur)) {
                        void send();
                        return;
                      }
                      applySlashItem(
                        slashItems[Math.min(slashIndex, slashItems.length - 1)],
                      );
                      return;
                    }
                  }
                  if (atFileOpen && atFileHits.length > 0) {
                    if (e.key === "ArrowDown") {
                      e.preventDefault();
                      setAtFileIndex((i) => (i + 1) % atFileHits.length);
                      return;
                    }
                    if (e.key === "ArrowUp") {
                      e.preventDefault();
                      setAtFileIndex(
                        (i) => (i - 1 + atFileHits.length) % atFileHits.length,
                      );
                      return;
                    }
                    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                      e.preventDefault();
                      void applyAtFile(
                        atFileHits[Math.min(atFileIndex, atFileHits.length - 1)],
                      );
                      return;
                    }
                    if (e.key === "Escape") {
                      e.preventDefault();
                      setInput(stripTrailingAtQuery(input));
                      return;
                    }
                  }
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send();
                  }
                }}
              />
              <div className="composer-toolbar">
                <div className="composer-tools">
                  <div className="mode-toggle" role="group" aria-label="chat mode">
                    <button
                      type="button"
                      className={`mode-btn${chatMode === "plan" ? " active" : ""}`}
                      title={t("modePlanHint")}
                      disabled={busy}
                      onClick={() => setChatMode("plan")}
                    >
                      {t("modePlan")}
                    </button>
                    <button
                      type="button"
                      className={`mode-btn${chatMode === "agent" ? " active" : ""}`}
                      title={t("modeAgentHint")}
                      disabled={busy}
                      onClick={() => setChatMode("agent")}
                    >
                      {t("modeAgent")}
                    </button>
                  </div>
                  <button
                    type="button"
                    className="composer-tool"
                    title={t("composerAt")}
                    onClick={() => {
                      setInput((v) => {
                        if (atFileMenuQuery(v) != null) return v;
                        if (!v || /[\s\n]$/.test(v)) return `${v}@`;
                        return `${v} @`;
                      });
                      requestAnimationFrame(() => composerRef.current?.focus());
                    }}
                  >
                    <IconAt size={16} />
                  </button>
                  <button
                    type="button"
                    className="composer-tool label"
                    title={t("composerAttachHint")}
                    disabled={attachBusy}
                    onClick={() => attachInputRef.current?.click()}
                  >
                    <IconPlus size={16} />
                    <span>{t("composerAttach")}</span>
                  </button>
                  <input
                    ref={attachInputRef}
                    type="file"
                    multiple
                    hidden
                    onChange={(e) => void addAttachments(e.target.files)}
                  />
                </div>
                <div className="composer-actions">
                  {busy ? (
                    <>
                      {input.trim() || attachments.length ? (
                        <button type="submit" className="composer-send queue">
                          {t("queue")}
                        </button>
                      ) : (
                        <button type="button" className="composer-send stop" onClick={() => void stopChat()}>
                          {t("stop")}
                        </button>
                      )}
                    </>
                  ) : (
                    <button
                      type="submit"
                      className="composer-send"
                      disabled={!input.trim() && attachments.length === 0}
                    >
                      <IconSend size={15} />
                      <span>{t("send")}</span>
                    </button>
                  )}
                </div>
              </div>
              {attachments.length > 0 && (
                <ul className="attach-list">
                  {attachments.map((a) => (
                    <li key={a.id} className="attach-chip">
                      <FileTypeIcon name={a.name} />
                      <span className="attach-chip-text">
                        <strong>{a.name}</strong>
                        <em>{a.path}</em>
                      </span>
                      <button
                        type="button"
                        className="attach-remove"
                        title={t("attachRemove")}
                        onClick={() =>
                          setAttachments((prev) => prev.filter((x) => x.id !== a.id))
                        }
                      >
                        <IconX size={12} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              </div>
            </div>
            </div>
            <div className="status-bar" aria-label="会话状态">
              <div
                className={`ctx-meter ${ctxWarn ? "warn" : ""}`}
                title={`${t("context")} ${ctx.tokens} / ${ctx.limit} tokens`}
              >
                <div className="ctx-meter-bar" style={{ width: `${ctxPct}%` }} />
                <span className="ctx-meter-label">
                  {t("context")} {ctx.tokens}/{ctx.limit} · {ctxPct}%
                </span>
              </div>
              <button
                type="button"
                className="chip muted status-link"
                title={activeWs?.path || t("selectWorkspace")}
                disabled={wsBusy}
                onClick={() => openSettings("workspace")}
              >
                <span className="ws-switcher-label">
                  {activeWs?.name || t("selectWorkspace")}
                </span>
              </button>
              <ModelSwitcher
                setup={model}
                locale={locale}
                role={modelSwitchRole}
                onRoleChange={setModelSwitchRole}
                onSelect={(role, providerId, modelId) =>
                  void switchModelRole(role, providerId, modelId)
                }
                onOpenSettings={() => openSettings("model")}
                t={t}
              />
            </div>
          </form>        </section>

        {detail && (
          <>
            <div
              className="sidebar-resizer detail-resizer"
              onMouseDown={() => {
                resizingDetailRef.current = true;
                document.body.classList.add("resizing-sidebar");
              }}
              title="拖拽调整预览宽度"
            />
            <aside className="detail-panel" style={{ width: detailWidth }}>
            <div className="detail-head">
              <h3>
                {detail.type === "tool"
                  ? `${t("detailTool")} · ${detail.tool.name}`
                  : detail.type === "subagent"
                    ? `${t("detailSubagent")} · ${detail.subagent.role || "leaf"}`
                    : detail.path}
              </h3>
              <div className="detail-actions">
                {detail.type === "file" &&
                  detail.kind === "text" &&
                  detail.highlightQuery &&
                  !detail.forceEdit &&
                  !detail.dirty && (
                  <button
                    type="button"
                    className="mini"
                    onClick={() => setDetail({ ...detail, forceEdit: true })}
                  >
                    {locale === "en" ? "Edit" : "编辑"}
                  </button>
                )}
                {detail.type === "file" && detail.editable && (
                  <button
                    type="button"
                    className="mini"
                    disabled={!detail.dirty}
                    onClick={() => void saveDetailFile()}
                  >
                    {locale === "en" ? "Save" : "保存"}
                  </button>
                )}
                {detail.type === "file" && detail.rawUrl && detail.kind !== "text" && (
                  <a className="mini linkish" href={detail.rawUrl} target="_blank" rel="noreferrer">
                    {locale === "en" ? "Open / Download" : "打开/下载"}
                  </a>
                )}
                <button type="button" className="icon-btn" onClick={() => setDetail(null)}>
                  {t("detailClose")}
                </button>
              </div>
            </div>
            {detail.type === "tool" ? (
              <div className="detail-body">
                <div className="detail-meta">
                  {t("detailStatus")}
                  {detail.tool.status === "streaming"
                    ? t("toolStatusStreaming")
                    : detail.tool.status === "running" || detail.tool.status === "pending"
                      ? t("toolStatusRunning")
                      : detail.tool.status === "error"
                        ? t("toolStatusError")
                        : detail.tool.status === "done"
                          ? t("toolStatusDone")
                          : detail.tool.status}
                </div>
                {detail.tool.name && isFileMutatingTool(detail.tool.name) ? (
                  <>
                    <h4>{t("diffPreview")}</h4>
                    <DiffReview
                      diff={detailDiff}
                      loading={detailDiffLoading}
                      title={t("diffPreview")}
                      newFileLabel={t("diffNewFile")}
                      truncatedLabel={t("diffTruncated")}
                      emptyLabel={t("diffEmpty")}
                      alreadyAppliedLabel={t("diffAlreadyApplied")}
                      snippetLabel={t("diffSnippet")}
                    />
                    {detail.tool.status === "streaming" ? (
                      <>
                        <h4>{t("detailArgs")}</h4>
                        <pre className="code-fence">
                          {detail.tool.argsRaw || formatArgs(detail.tool.args)}
                        </pre>
                      </>
                    ) : null}
                  </>
                ) : (
                  <>
                    <h4>{t("detailArgs")}</h4>
                    <pre className="code-fence">
                      {detail.tool.argsRaw && detail.tool.status === "streaming"
                        ? detail.tool.argsRaw
                        : formatArgs(detail.tool.args)}
                    </pre>
                  </>
                )}
                <h4>{t("detailOutput")}</h4>
                <pre className="code-fence">
                  {detail.tool.result ||
                    (detail.tool.status === "streaming"
                      ? t("toolArgsStreaming")
                      : detail.tool.status === "running" || detail.tool.status === "pending"
                        ? t("toolRunning")
                        : t("toolNoOutput"))}
                </pre>
              </div>
            ) : detail.type === "subagent" ? (
              <div className="detail-body subagent-detail">
                <div className="detail-meta">
                  {detail.subagent.status === "running"
                    ? t("toolStatusRunning")
                    : detail.subagent.status === "error"
                      ? t("toolStatusFailStop")
                      : t("subagentDone")}
                  {detail.subagent.activity ? ` · ${detail.subagent.activity}` : ""}
                </div>
                <p className="subagent-detail-goal">{detail.subagent.goal}</p>
                <div className="subagent-detail-thread">
                  {(detail.subagent.transcript || []).length === 0 && (
                    <p className="hint">{t("subagentWaiting")}</p>
                  )}
                  {(detail.subagent.transcript || []).map((item) => {
                    if (item.kind === "assistant") {
                      return (
                        <article
                          key={item.id}
                          className={`bubble assistant${item.streaming ? " streaming" : ""}`}
                        >
                          <div className="role">
                            {t("subagentLabel")}
                            {item.streaming
                              ? item.reasoningStreaming
                                ? ` · ${t("thinking")}`
                                : ` · ${t("outputting")}`
                              : ""}
                          </div>
                          {(item.reasoning || item.reasoningStreaming) && (
                            <ThinkingBlock
                              content={item.reasoning || ""}
                              streaming={Boolean(item.reasoningStreaming)}
                            />
                          )}
                          {item.text ? (
                            <MarkdownView
                              content={item.text}
                              streaming={item.streaming && !item.reasoningStreaming}
                            />
                          ) : null}
                        </article>
                      );
                    }
                    const tool = item.tool;
                    return (
                      <div
                        key={item.id}
                        className={`tool-chip ${tool.status}`}
                        title={tool.summary}
                      >
                        <span className="tool-chip-mark">
                          {tool.status === "pending"
                            ? "?"
                            : tool.status === "streaming" || tool.status === "running"
                              ? "…"
                              : tool.status === "error"
                                ? "!"
                                : "…"}
                        </span>
                        <span className="tool-chip-body">
                          <span className="tool-chip-name">{tool.name}</span>
                          <span className="tool-chip-summary">{tool.summary}</span>
                        </span>
                        {tool.result && (
                          <pre className="subagent-tool-result">{tool.result.slice(0, 2000)}</pre>
                        )}
                      </div>
                    );
                  })}
                </div>
                {detail.subagent.summary && detail.subagent.status !== "running" && (
                  <>
                    <h4>{t("subagentFinalSummary")}</h4>
                    <pre className="code-fence">{detail.subagent.summary}</pre>
                  </>
                )}
              </div>
            ) : (
              <div className="detail-body file-preview">
                <div className="detail-meta">
                  {detail.kind}
                  {detail.mime ? ` · ${detail.mime}` : ""}
                  {detail.size != null ? ` · ${formatBytes(detail.size)}` : ""}
                </div>
                {detail.kind === "image" && detail.rawUrl && (
                  <div className="media-frame">
                    <img src={detail.rawUrl} alt={detail.path} />
                  </div>
                )}
                {detail.kind === "pdf" && detail.rawUrl && (
                  <iframe className="pdf-frame" title={detail.path} src={detail.rawUrl} />
                )}
                {detail.kind === "audio" && detail.rawUrl && (
                  <audio className="media-player" controls src={detail.rawUrl} />
                )}
                {detail.kind === "video" && detail.rawUrl && (
                  <video className="media-player" controls src={detail.rawUrl} />
                )}
                {detail.kind === "document" && (
                  <div className="office-preview">
                    <p className="hint">
                      {detail.message || "文本预览（非完整排版）"}。需要原文件请点「打开/下载」。                    </p>
                    <pre className="code-fence office-text">
                      {detail.preview || "（无文字内容）"}
                    </pre>
                  </div>
                )}
                {detail.kind === "unsupported" && (
                  <div className="unsupported-preview">
                    <p className="hint unsupported-msg">
                      {detail.message || "暂不支持预览此文件"}
                    </p>
                    {detail.rawUrl && (
                      <p className="hint">可使用「打开/下载」在系统中查看原文件。</p>
                    )}
                  </div>
                )}
                {detail.kind === "text" &&
                  detail.highlightQuery &&
                  !detail.forceEdit &&
                  !detail.dirty ? (
                    <FileHighlightView
                      content={detail.content}
                      query={detail.highlightQuery}
                      focusLine={detail.focusLine}
                    />
                  ) : detail.kind === "text" ? (
                    <div className="file-editor">
                      <textarea
                        value={detail.content}
                        readOnly={!detail.editable}
                        onChange={(e) =>
                          setDetail({ ...detail, content: e.target.value, dirty: true })
                        }
                      />
                    </div>
                  ) : null}
              </div>
            )}
          </aside>
          </>
        )}
          </>
        )}
      </main>

      {editRestorePrompt && (
        <div
          className="modal-backdrop"
          onClick={() => setEditRestorePrompt(null)}
          role="presentation"
        >
          <div
            className="modal edit-restore-modal"
            role="dialog"
            aria-modal="true"
            aria-label={t("editRestoreTitle")}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-head">
              <h2>{t("editRestoreTitle")}</h2>
              <button type="button" onClick={() => setEditRestorePrompt(null)}>
                {t("close")}
              </button>
            </div>
            <div className="modal-body">
              <p className="hint">{t("editRestoreBody")}</p>
              <div className="edit-restore-actions">
                <button
                  type="button"
                  className="bubble-edit-btn cancel"
                  onClick={() => setEditRestorePrompt(null)}
                >
                  {t("cancel")}
                </button>
                <button
                  type="button"
                  className="bubble-edit-btn"
                  onClick={() => void submitEdit(editRestorePrompt.msgId, false)}
                >
                  {t("editRestoreChatOnly")}
                </button>
                <button
                  type="button"
                  className="bubble-edit-btn primary"
                  onClick={() => void submitEdit(editRestorePrompt.msgId, true)}
                >
                  {t("editRestoreWithFiles")}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {settingsOpen && (
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget) setSettingsOpen(false);
          }}
        >
          <div className="modal" role="dialog" aria-modal="true" aria-label={t("settings")}>
            <div className="modal-head">
              <h2>{t("settings")}</h2>
              <div className="modal-head-actions">
                <button
                  type="button"
                  className="theme-toggle"
                  title={theme === "dark" ? t("themeLight") : t("themeDark")}
                  onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                >
                  {theme === "dark" ? <IconSun /> : <IconMoon />}
                </button>
                <button type="button" className="icon-btn" onClick={() => setSettingsOpen(false)}>
                  {t("close")}
                </button>
              </div>
            </div>
            <div className="modal-tabs">
              {(
                [
                  ["workspace", t("tabWorkspace")],
                  ["model", t("tabModel")],
                  ["appearance", t("tabAppearance")],
                  ["memory", t("tabMemory")],
                  ["runtime", t("tabRuntime")],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  className={settingsTab === id ? "active" : ""}
                  onClick={() => setSettingsTab(id)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="modal-body">
              {settingsTab === "workspace" && (
                <div className="settings-pane">
                  <h3>{t("workspaceTitle")}</h3>
                  <p className="hint path-line">{activeWs?.path || t("workspaceNone")}</p>
                  <div className="ws-actions">
                    <button
                      type="button"
                      className="primary"
                      disabled={wsBusy}
                      onClick={() => void browseAndSetWorkspace()}
                    >
                      {wsBusy ? t("browsing") : t("openFolder")}
                    </button>
                  </div>
                  {workspaces.length > 0 && (
                    <>
                      <h3 style={{ marginTop: 20 }}>{t("recentFolders")}</h3>
                      <ul className="item-list">
                        {workspaces.map((w) => (
                          <li key={w.path}>
                            <div>
                              <strong>{w.name}</strong>
                              <span>{w.path}</span>
                            </div>
                            <button
                              type="button"
                              className="mini"
                              disabled={activeWs?.path === w.path || wsBusy}
                              onClick={() => void switchWorkspace(w.path)}
                            >
                              {activeWs?.path === w.path ? t("inUse") : t("switch")}
                            </button>
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}

              {settingsTab === "model" && model && (
                <ModelSettings
                  setup={model}
                  locale={locale}
                  saving={modelSaving}
                  onChange={setModel}
                  onSave={(next, opts) => void applyModel(next, opts)}
                  t={t}
                />
              )}

              {settingsTab === "appearance" && (
                <div className="settings-pane">
                  <h3>{t("appearanceTitle")}</h3>
                  <div className="appearance-row">
                    <span className="appearance-label">{t("language")}</span>
                    <div className="seg-control" role="group" aria-label={t("language")}>
                      <button
                        type="button"
                        className={locale === "zh" ? "active" : ""}
                        onClick={() => setLocale("zh")}
                      >
                        中文
                      </button>
                      <button
                        type="button"
                        className={locale === "en" ? "active" : ""}
                        onClick={() => setLocale("en")}
                      >
                        English
                      </button>
                    </div>
                  </div>
                  <div className="appearance-row">
                    <span className="appearance-label">{t("theme")}</span>
                    <div className="seg-control" role="group" aria-label={t("theme")}>
                      <button
                        type="button"
                        className={theme === "light" ? "active" : ""}
                        onClick={() => setTheme("light")}
                        title={t("themeLight")}
                      >
                        <IconSun /> {t("themeLight")}
                      </button>
                      <button
                        type="button"
                        className={theme === "dark" ? "active" : ""}
                        onClick={() => setTheme("dark")}
                        title={t("themeDark")}
                      >
                        <IconMoon /> {t("themeDark")}
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {settingsTab === "memory" && (
                <div className="settings-pane memory-pane">
                  <h3>MEMORY.md</h3>
                  <textarea value={memory} onChange={(e) => setMemory(e.target.value)} />
                  <button
                    type="button"
                    className="primary"
                    onClick={() =>
                      void saveMemory(memory).then(() => setToast(t("memorySaved")))
                    }
                  >
                    {t("saveMemory")}
                  </button>
                </div>
              )}

              {settingsTab === "runtime" && (
                <div className="settings-pane">
                  <h3>{t("runtimeSubs")}</h3>
                  {subs.length === 0 ? (
                    <p className="hint">{t("runtimeSubsHint")}</p>
                  ) : (
                    <ul className="sub-tree">
                      {subs.map((s) => (
                        <li key={s.id} className={s.status}>
                          <div className="sub-goal">{s.goal}</div>
                          <div className="sub-meta">
                            {s.status}
                            {s.activity ? ` · ${s.activity}` : ""}
                          </div>
                          {s.summary && <pre className="sub-sum">{s.summary}</pre>}
                        </li>
                      ))}
                    </ul>
                  )}
                  <h3>{t("runtimeEvents")}</h3>
                  <ul className="live-log">
                    {live.length === 0 ? (
                      <li className="hint">{t("runtimeEventsHint")}</li>
                    ) : (
                      live.map((l) => (
                        <li key={l.id}>
                          <code>{l.kind}</code> {l.text}
                        </li>
                      ))
                    )}
                  </ul>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
