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
export type EvidenceStatus = "sufficient" | "limited" | "unmeasured";
export type InterviewerState = "listening" | "thinking" | "speaking";
export type FinishReason = "enough_understanding" | "natural_closure" | "user_requested" | "safety_stopped" | null;
export type NaturalSessionAction = "continue" | "suggest_finish" | "finish";
export type ClosureFinishReason = "enough_understanding" | "natural_closure";

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
  session_action?: NaturalSessionAction | null;
  finish_reason?: FinishReason;
  created_at?: string;
}

export interface ClosureSuggestion {
  closure_turn_id: number;
  transcript_fingerprint: string;
  finish_reason: ClosureFinishReason;
  session_uuid?: string;
}

export interface SessionSnapshot {
  uuid: string;
  phase: SessionPhase;
  consent_version?: string;
  consent_accepted_at?: string | null;
  participant?: ParticipantProfile;
  current_turn?: DialogueTurn | null;
  turns: DialogueTurn[];
  user_answer_count?: number;
  technical_turn_cap?: number;
  technical_turn_cap_reached?: boolean;
  transcript_fingerprint?: string | null;
  finalization_state?: string | null;
  closure_suggestion?: ClosureSuggestion | null;
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
  answer_duration_ms: number;
  /** Client-only diagnostics; no participant content is added here. */
  technical_anomaly?: string | null;
}

export type StreamEventType =
  | "user_turn_saved"
  | "agent_started"
  | "heartbeat"
  | "agent_delta"
  | "session_closure_suggested"
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

export interface AcceptClosureSuggestionRequest {
  expected_transcript_fingerprint: string;
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

export interface FinalizeSessionRequest {
  evidence_check_id?: number;
  expected_transcript_fingerprint?: string;
  allow_incomplete?: boolean;
}

export type ReportReadinessStatus = "ready" | "insufficient" | "checking" | "failed";

/**
 * Participant-safe report readiness result.
 *
 * Dimension names, scores, quotes, and internal scoring details deliberately
 * stay out of this contract. The exact check id and transcript fingerprint
 * are an optimistic-concurrency boundary for finalizing this same snapshot.
 */
export interface ReportReadinessResponse {
  status: ReportReadinessStatus;
  ready: boolean | null;
  cached: boolean;
  check_id: number;
  transcript_fingerprint: string;
  minimum_turns_required?: number;
  minimum_turns_met?: boolean;
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

export type EvidenceAttributionOwner =
  | "participant_owned"
  | "external_quoted"
  | "external_paraphrased"
  | "uncertain";
export type EvidenceAttributionRelation =
  | "own_reasoning"
  | "endorses"
  | "critiques"
  | "rejects"
  | "quotes_only"
  | "asks_or_requests";
export type EvidenceElicitationLevel = "spontaneous" | "open_probe" | "focused_probe" | "strong_scaffold";
export type EvidenceEligibilityStatus = "eligible" | "context_only" | "manual_review";
export type EvidenceAttributionValidationStatus =
  | "pending"
  | "validated"
  | "rejected"
  | "manual_review"
  | "legacy_unclassified";

/**
 * Administrator-only source attribution for an exact participant-input span.
 * This contract must not be added to SessionSnapshot or participant report APIs.
 */
export interface EvidenceAttribution {
  id?: number | string;
  span_id?: number | string;
  user_turn_id?: number | string;
  readiness_check_id?: number | string | null;
  turn_index: number;
  quote: string;
  start: number;
  end: number;
  text_hash?: string;
  owner: EvidenceAttributionOwner;
  relation: EvidenceAttributionRelation;
  source_label?: string | null;
  elicitation_level: EvidenceElicitationLevel;
  confidence?: number | null;
  reason?: string | null;
  /** Canonical V6.2.1 wire field. */
  eligibility?: EvidenceEligibilityStatus;
  /** Accepted during the rollout transition for compatible API producers. */
  eligibility_status?: EvidenceEligibilityStatus;
  validation_status?: EvidenceAttributionValidationStatus;
  validation_reason?: string | null;
  final_scoring_dimension_keys?: string[];
  snapshot_used_dimension_keys?: string[];
  /** Legacy merged usage field; prefer the two explicit fields above. */
  used_dimension_keys?: string[];
  eliciting_question?: string | null;
  transcript_fingerprint?: string;
  asset_fingerprint?: string;
  prompt_template_id?: string;
  prompt_version?: string;
  schema_version?: string;
  created_at?: string;
}

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
  quote_start?: number;
  quote_end?: number;
  confidence?: number | null;
  attribution_span_id?: number | string | null;
  readiness_check_id?: number | string | null;
  validation_status?: EvidenceAttributionValidationStatus;
  validation_reason?: string | null;
}

export interface AdminSessionDetail extends SessionSnapshot {
  review_status?: ReviewStatus;
  review_notes?: string;
  reviewer?: string;
  reviewed_at?: string | null;
  evidence_attributions?: EvidenceAttribution[];
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
  return session.user_answer_count ?? session.turns.filter((turn) => turn.role === "user").length;
}
