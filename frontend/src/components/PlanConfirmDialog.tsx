import type { PlanTask } from "./TaskPlanPanel";

type Props = {
  summary: string;
  tasks: PlanTask[];
  titleLabel: string;
  dialogLabel: string;
  approveLabel: string;
  rejectLabel: string;
  submitting: boolean;
  onApprove: () => void;
  onReject: () => void;
};

/** Inline dialog: review a generated plan before execution. */
export function PlanConfirmDialog({
  summary,
  tasks,
  titleLabel,
  dialogLabel,
  approveLabel,
  rejectLabel,
  submitting,
  onApprove,
  onReject,
}: Props) {
  return (
    <div className="inline-plan" role="dialog" aria-label={dialogLabel}>
      <div className="inline-plan-top">
        <div className="inline-plan-title">{titleLabel}</div>
        <p className="inline-plan-summary">{summary}</p>
      </div>
      <ol className="inline-plan-list">
        {tasks.map((task, i) => (
          <li key={task.id || i} className="inline-plan-item">
            <span className="inline-plan-idx">{i + 1}</span>
            <span className="inline-plan-text">
              <span className="inline-plan-task-title">{task.title}</span>
              {task.detail ? (
                <span className="inline-plan-task-detail">{task.detail}</span>
              ) : null}
            </span>
          </li>
        ))}
      </ol>
      <div className="inline-plan-actions">
        <button
          type="button"
          className="approval-btn reject"
          disabled={submitting}
          onClick={onReject}
        >
          {rejectLabel}
        </button>
        <button
          type="button"
          className="approval-btn allow"
          disabled={submitting}
          onClick={onApprove}
        >
          {approveLabel}
        </button>
      </div>
    </div>
  );
}
