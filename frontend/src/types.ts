import type { RunNote } from './components/RunNotes';
import type { RunParameter } from './runForms';

/* ─── Types ───────────────────────────────────────────── */
export type Run = {
  id: number;
  workflow_id: number;
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  trigger_type: string;
  run_key?: string | null;
  parameters_json?: Record<string, unknown> | null;
  task_runs?: TaskRun[];
  // v0.5: GET /runs/{id} answers WorkflowRunDetail, which embeds the note
  // thread and the resume/SLA bookkeeping.
  notes?: RunNote[];
  resume_count?: number;
  sla_breached_at?: string | null;
};

export type TaskRun = {
  id: number;
  workflow_run_id: number;
  task_id: number;
  task_name?: string | null;
  task_type?: string | null;
  status: string;
  attempt: number;
  started_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
  exit_code?: number | null;
  error_message?: string | null;
  rendered_command?: string | null;
  // v0.5: an approval gate is a TaskRun with no logs. created_at is when the
  // gate opened, which is what the card counts up from.
  created_at?: string;
  resume_index?: number;
  approval_note?: string | null;
  approved_at?: string | null;
};

export type Workflow = {
  id: number;
  name: string;
  description?: string | null;
  schedule_cron?: string | null;
  schedule_timezone?: string | null;
  enabled: boolean;
  max_concurrent_runs: number;
  project_id?: number | null;
  default_environment_id?: number | null;
  notify_webhook_url?: string | null;
  webhook_error?: string | null;
  webhook_error_at?: string | null;
  auto_pause_failures?: number | null;
  // v0.5 operator state — written by the snooze endpoints, never by WorkflowIn.
  snooze_until?: string | null;
  snooze_pauses_runs?: boolean;
  snoozed?: boolean;
  // v0.5 configuration — part of WorkflowIn, so both modals must round-trip it.
  missed_run_grace_minutes?: number | null;
  sla_minutes?: number | null;
  /** The named resource this workflow serialises on. NULL is no locking, and
   *  the mode is inert without it — see LockField. Also WorkflowIn keys, so the
   *  same round-trip rule applies. */
  lock_resource?: string | null;
  lock_mode?: 'shared' | 'exclusive';
  /** The run form: typed parameters "Run with…" asks for. A WorkflowIn key. */
  run_parameters_json?: RunParameter[] | null;
};

export type Project = {
  id: number;
  name: string;
  description?: string | null;
  root_path: string;
  default_environment_id?: number | null;
};

export type Env = {
  id: number;
  name: string;
  env_type: string;
  executable?: string | null;
  conda_env?: string | null;
  env_vars_json?: Record<string, unknown> | null;
  description?: string | null;
  managed: boolean;
  status: string;
  base_executable?: string | null;
  packages_json: string[];
  active_packages_json: string[];
  python_version?: string | null;
  build_log?: string | null;
  last_error?: string | null;
};

export type Task = {
  id: number;
  workflow_id?: number | null;
  project_id?: number | null;
  environment_id?: number | null;
  name: string;
  task_type: string;
  command?: string | null;
  script_path?: string | null;
  notebook_path?: string | null;
  sql_path?: string | null;
  cwd?: string | null;
  depends_on_json: string[];
  parameters_json?: Record<string, unknown> | null;
  retries: number;
  retry_delay_seconds: number;
  timeout_seconds?: number | null;
  requires_approval?: boolean;
  approval_prompt?: string | null;
};

export type DailyStat = { date: string; success: number; failed: number; other: number };

export type Summary = {
  running: number; queued: number; waiting: number; live: number;
  runs_24h: number; succeeded_24h: number; failed_24h: number;
  avg_duration_24h: number | null;
  done_7d: number; success_7d: number; success_rate_7d: number | null;
};
