import { FileExplorer } from "./FileExplorer";
import { FileSearchPanel } from "./FileSearchPanel";
import { HistoryPanel } from "./HistoryPanel";
import { IconClock, IconFiles, IconSearch, IconSettings } from "./icons";
import { IconRobotCube } from "./IconRobotCube";
import type { SessionItem } from "../api";
import { fileToDetail } from "../utils/chatHelpers";
import type { MsgKey } from "../i18n";

export type ActivitySidebarProps = {
  t: (key: MsgKey, ...args: string[]) => string;
  sidePanel: "files" | "search" | "history";
  setSidePanel: (panel: "files" | "search" | "history") => void;
  explorerCollapsed: boolean;
  setExplorerCollapsed: (v: boolean) => void;
  explorerWidth: number;
  fsRefresh: number;
  activeWs: { path: string; name: string } | null;
  sessions: SessionItem[];
  sessionId: string | null;
  sessionsPage: number;
  sessionsTotalPages: number;
  sessionsTotal: number;
  onOpenHistoryPanel: () => void;
  onRefreshSessions: (page?: number) => void;
  onOpenSession: (id: string) => void;
  onNewChat: () => void;
  onDeleteSession: (id: string) => Promise<void>;
  onOpenSettings: () => void;
  onOpenFile: (file: Parameters<typeof fileToDetail>[0], opts?: Parameters<typeof fileToDetail>[1]) => void;
  onFileDeleted: (path: string) => void;
  onResizeStart: () => void;
};

export function ActivitySidebar({
  t,
  sidePanel,
  setSidePanel,
  explorerCollapsed,
  setExplorerCollapsed,
  explorerWidth,
  fsRefresh,
  activeWs,
  sessions,
  sessionId,
  sessionsPage,
  sessionsTotalPages,
  sessionsTotal,
  onOpenHistoryPanel,
  onRefreshSessions,
  onOpenSession,
  onNewChat,
  onDeleteSession,
  onOpenSettings,
  onOpenFile,
  onFileDeleted,
  onResizeStart,
}: ActivitySidebarProps) {
  return (
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
                onOpenHistoryPanel();
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
            onClick={onOpenSettings}
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
                onOpenFile={(file, opts) => onOpenFile(file, opts)}
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
                onRefresh={() => void onRefreshSessions(sessionsPage)}
                onPageChange={(p) => void onRefreshSessions(p)}
                onOpen={(id) => void onOpenSession(id)}
                onNew={() => void onNewChat()}
                onDelete={(id) => onDeleteSession(id)}
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
              onOpenFile={(file) => onOpenFile(file)}
              onDeleted={onFileDeleted}
            />
          )}
          <div
            className="sidebar-resizer"
            onMouseDown={(e) => {
              e.preventDefault();
              onResizeStart();
            }}
            title="拖拽调整宽度"
          />
        </>
      )}
    </>
  );
}
