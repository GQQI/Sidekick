import { useEffect, useState } from "react";
import type { ActivePlan, PlanTaskStatus } from "../types/plan";

export type { ActivePlan, PlanTask, PlanTaskStatus } from "../types/plan";

type Props = {
  plan: ActivePlan;
  subtitle: string;
  titleLabel: string;
  collapseLabel?: string;
  expandLabel?: string;
};

function statusIcon(status: PlanTaskStatus) {
  if (status === "done") return "✓";
  if (status === "running") return "◐";
  if (status === "error") return "✕";
  if (status === "cancelled") return "—";
  return "○";
}

export function TaskPlanPanel({
  plan,
  subtitle,
  titleLabel,
  collapseLabel = "收起",
  expandLabel = "展开",
}: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const running = plan.tasks.some((t) => t.status === "running");

  useEffect(() => {
    if (running) setCollapsed(false);
  }, [running]);

  return (
    <div
      className={`task-plan${collapsed ? " collapsed" : ""}`}
      role="region"
      aria-label={titleLabel}
    >
      <button
        type="button"
        className="task-plan-head"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed((v) => !v)}
      >
        <span className="task-plan-head-main">
          <strong>{plan.summary || titleLabel}</strong>
          <span className="task-plan-sub">{subtitle}</span>
        </span>
        <span className="task-plan-toggle" aria-hidden>
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="sr-only">{collapsed ? expandLabel : collapseLabel}</span>
      </button>
      {!collapsed && (
        <ol className="task-plan-list">
          {plan.tasks.map((task, i) => (
            <li
              key={task.id || i}
              className={`task-plan-item status-${task.status}`}
              title={task.detail || ""}
            >
              <span className="task-plan-check" aria-hidden>
                {statusIcon(task.status)}
              </span>
              <span className="task-plan-text">
                <span className="task-plan-title">{task.title}</span>
                {task.detail ? <span className="task-plan-detail">{task.detail}</span> : null}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
