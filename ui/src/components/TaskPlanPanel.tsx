import { useState } from "react";
import type { ActivePlan, PlanTaskStatus } from "../types/plan";

export type { ActivePlan, PlanTask, PlanTaskStatus } from "../types/plan";

type Props = {
  plan: ActivePlan;
  subtitle: string;
  titleLabel: string;
  collapseLabel?: string;
  expandLabel?: string;
};

function checkClass(status: PlanTaskStatus): string {
  if (status === "done") return "done";
  if (status === "running") return "running";
  if (status === "error") return "error";
  if (status === "cancelled") return "cancelled";
  return "pending";
}

export function TaskPlanPanel({
  plan,
  subtitle,
  titleLabel,
  collapseLabel = "收起",
  expandLabel = "展开",
}: Props) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div
      className={`plan-doc task-plan${collapsed ? " collapsed" : ""}`}
      role="region"
      aria-label={titleLabel}
    >
      <button
        type="button"
        className="plan-doc-head task-plan-head"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed((v) => !v)}
      >
        <span className="plan-doc-head-main">
          <span className="plan-doc-badge">{titleLabel}</span>
          <strong className="plan-doc-heading">{plan.summary || titleLabel}</strong>
          <span className="plan-doc-overview">{subtitle}</span>
        </span>
        <span className="plan-doc-toggle" aria-hidden>
          {collapsed ? "▸" : "▾"}
        </span>
        <span className="sr-only">{collapsed ? expandLabel : collapseLabel}</span>
      </button>
      {!collapsed && (
        <ul className="plan-doc-list">
          {plan.tasks.map((task, i) => (
            <li
              key={task.id || i}
              className={`plan-doc-item status-${task.status}`}
              title={task.detail || ""}
            >
              <span className={`plan-check ${checkClass(task.status)}`} aria-hidden />
              <span className="plan-doc-text">
                <span className="plan-doc-task-title">{task.title}</span>
                {task.detail ? (
                  <span className="plan-doc-task-detail">{task.detail}</span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
