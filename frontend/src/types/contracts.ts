/**
 * V6 public contracts deliberately describe the natural interview, rather
 * than exposing the internal observation strategy to the participant UI.
 */
export type SessionPhase =
  | "interviewing"
  | "finalizing"
  | "completed"
  | "exited"
  | "safety_stopped";

export type InputMode = "text" | "voice" | "voice_edited";
export type InteractionKind = "answer" | "clarification";
export type EvidenceStatus = "sufficient" | "limited" | "unmeasured";
export type InterviewerState = "listening" | "thinking" | "speaking";
export type FinishReason = "enough_understanding" | "natural_closure" | "user_requested" | "safety_stopped" | "technical_limit" | null;
export type NaturalSessionAction = "continue" | "finish";

export interface ParticipantProfile {
  display_name?: string;
}

export interface DialogueTurn {
  id?: number | string;
  turn_index: number;
  role: "user" | "assistant" | "system";
  content: string;
  client_turn_id?: string | null;
  input_mode?: InputMode | null;
  answer_duration_ms?: number | null;
  phase?: SessionPhase | string;
  quality_flags?: string[];
  created_at?: string;
}

export interface SessionSnapshot {
  uuid: string;
  phase: SessionPhase;
  consent_version?: string;
  consent_accepted_at?: string | null;
  participant?: ParticipantProfile;
  turns: DialogueTurn[];
  /** Compatibility alias; V6.1 responses also expose valid_answer_count. */
  user_answer_count?: number;
  /** Server-authoritative count; clarification, safety and exit turns are excluded. */
  valid_answer_count?: number;
  interview_protocol_version?: string;
  minimum_valid_answers?: number;
  maximum_user_answers?: number;
  remaining_required_answers?: number;
  can_finalize?: boolean;
  transcript_fingerprint?: string | null;
  transcript_frozen_at?: string | null;
  finalization_state?: string | null;
  report_available?: boolean;
  ended_early?: boolean;
  exit_reason?: string | null;
  manual_review_recommended?: boolean;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
}

export interface CreateSessionRequest {
  consent_version: string;
  consent_given: true;
  participant: ParticipantProfile;
}

export interface CreateSessionResponse {
  session: SessionSnapshot;
  initial_turn?: DialogueTurn;
}

export interface TurnRequest {
  content: string;
  client_turn_id: string;
  input_mode: InputMode;
  interaction_kind?: InteractionKind;
  answer_duration_ms: number;
  /** Client-only diagnostics; no participant content is added here. */
  technical_anomaly?: string | null;
}

export type StreamEventType =
  | "user_turn_saved"
  | "agent_started"
  | "agent_delta"
  | "agent_completed"
  | "session_finalizing"
  | "error";

export interface AgentCompletedData {
  turn: DialogueTurn;
  session_action: NaturalSessionAction;
  finish_reason: FinishReason;
  speech_url?: string | null;
  session?: SessionSnapshot;
}

export interface TurnStreamEvent {
  event: StreamEventType;
  data?: unknown;
  delta?: string;
  message?: string;
  code?: string;
}

export interface ReportEvidence {
  quote: string;
  source_type?: "user" | string;
  turn_index?: number;
}

export interface ReportDimension {
  dimension_key: string;
  dimension_name: string;
  status: EvidenceStatus;
  score: number | null;
  reason: string;
  observation?: string;
  strength?: string;
  suggestion: string;
  evidences: ReportEvidence[];
}

export interface AssessmentReport {
  session_uuid: string;
  generated_at?: string;
  summary?: string;
  dimensions: ReportDimension[];
  strengths: string[];
  priorities: string[];
  disclaimer?: string;
  transcript_fingerprint?: string;
  experimental_notice?: string;
  manual_review_recommended?: boolean;
}

export interface FinalizeResponse {
  session: SessionSnapshot;
  report?: AssessmentReport | null;
}

/** Administrative contracts stay intentionally separate from participant UI. */
export interface AdminUser {
  username: string;
  display_name: string;
}

export interface DashboardRecentSession extends AdminSessionSummary {}

export interface DashboardOverview {
  measurement: {
    total_sessions: number;
    completed_sessions: number;
    active_sessions: number;
    completion_rate: number;
    phase_counts: Record<SessionPhase, number>;
  };
  review_queue: {
    pending: number;
    in_review: number;
    approved: number;
    needs_followup: number;
    manual_review_recommended: number;
    expert_scored_sessions: number;
  };
  pipeline_health: {
    reports_generated: number;
    scoring_failures: number;
    failed_traces: number;
    repaired_traces: number;
    technical_anomalies: number;
  };
  recent_sessions: DashboardRecentSession[];
}

export interface AgentTrace {
  id?: number | string;
  turn_index?: number;
  module?: string;
  action?: string;
  model?: string;
  requested_model?: string | null;
  actual_model?: string | null;
  response_id?: string | null;
  request_id?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
  transport_retry_count?: number | null;
  prompt_version?: string;
  prompt_template_id?: string | null;
  renderer_status?: "accepted" | "repaired" | "failed" | string;
  fallback_used?: boolean;
  fallback_reason?: string | null;
  repair_used?: boolean;
  output_contract?: {
    session_action?: NaturalSessionAction;
    finish_reason?: FinishReason;
    quality_flags?: string[];
  } | null;
  latency_ms?: number | null;
  created_at?: string;
}

export interface ScoringRun {
  id?: number | string;
  attempt_number: number;
  status: "processing" | "failed" | "completed";
  transcript_fingerprint: string;
  model?: string;
  requested_model?: string | null;
  actual_model?: string | null;
  response_id?: string | null;
  request_id?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
  transport_retry_count?: number | null;
  prompt_template_id?: string | null;
  prompt_version?: string | null;
  repair_used?: boolean;
  error?: string | null;
  manual_review_recommended?: boolean;
  result_data?: unknown;
  created_at?: string;
  completed_at?: string | null;
}

export interface TechnicalAnomaly {
  turn_index?: number | null;
  category: string;
  detail: string;
  recoverable?: boolean;
  created_at?: string;
}

export type ReviewStatus = "pending" | "in_review" | "approved" | "needs_followup";

export interface ExpertScore {
  dimension_key: string;
  score: number | null;
  comment: string;
}

export interface AdminSessionSummary {
  uuid: string;
  phase: SessionPhase;
  display_name?: string;
  user_answer_count?: number;
  manual_review_recommended?: boolean;
  review_status?: ReviewStatus;
  created_at?: string;
  updated_at?: string;
}

export interface AdminEvidenceItem {
  dimension_key: string;
  quote: string;
  turn_index?: number;
  source_type?: "user" | string;
  status?: EvidenceStatus;
  active_for_scoring?: boolean;
}

export interface AdminSessionDetail extends SessionSnapshot {
  review_status?: ReviewStatus;
  review_notes?: string;
  reviewer?: string;
  reviewed_at?: string | null;
  evidence_items?: AdminEvidenceItem[];
  traces?: AgentTrace[];
  scoring_runs?: ScoringRun[];
  technical_anomalies?: Array<string | TechnicalAnomaly>;
  expert_scores?: ExpertScore[];
  report?: AssessmentReport | null;
}

export interface PagedSessions {
  items: AdminSessionSummary[];
  total: number;
}

export const DIMENSIONS = [
  { key: "problem_definition", name: "问题界定" },
  { key: "evidence_evaluation", name: "证据评估" },
  { key: "reasoning_argumentation", name: "推理与论证" },
  { key: "multiple_perspectives", name: "多元视角" },
  { key: "integrative_decision", name: "综合决策" },
  { key: "dynamic_adjustment", name: "动态调整" },
] as const;

export function answerCount(session: SessionSnapshot): number {
  const authoritative = session.valid_answer_count ?? session.user_answer_count;
  if (typeof authoritative === "number" && Number.isFinite(authoritative)) {
    return Math.max(0, Math.floor(authoritative));
  }

  // A count inferred from an incomplete/legacy response must never unlock report
  // generation by treating every user turn as evidence.  Only turns explicitly
  // classified by the server as valid answers are safe to count; clarification,
  // safety and exit turns therefore remain visible without advancing progress.
  return session.turns.filter((turn) => (
    turn.role === "user" && turn.quality_flags?.includes("valid_answer")
  )).length;
}
