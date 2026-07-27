/** Shared chat / plan types used across App and panels. */

export type PlanTaskStatus = "pending" | "running" | "done" | "error" | "cancelled";

export type PlanTask = {
  id: string;
  title: string;
  detail?: string;
  status: PlanTaskStatus;
};

export type ActivePlan = {
  planId: string;
  summary: string;
  mode: "plan" | "agent";
  awaitingConfirm?: boolean;
  tasks: PlanTask[];
};

export type PlanConfirmState = {
  planId: string;
  sessionId: string;
  summary: string;
  tasks: PlanTask[];
};
