/** JOB-4 subset. These wire types carry evidence; they do not produce decisions. */
export type Fact<T> =
  | { availability: "reported"; value: T }
  | { availability: "not_reported" | "unknown"; value: null };
export type Metric = Fact<number> & { unit: string; definition: string };
export type Capability =
  | { allowed: true; reason: null }
  | {
      allowed: false;
      reason:
        | "not_implemented"
        | "not_authorized"
        | "evidence_unavailable"
        | "state_conflict";
    };
export type Capabilities = Record<string, Capability>;
export type Decision =
  "strong_match" | "possible_match" | "needs_review" | "reject";
export type ScopeMeta = {
  client_id: string | null;
  brief_revision_id: string | null;
  run_id: string | null;
  destination_id: string | null;
  cohort_id: string | null;
};
export type ApiMeta = {
  request_id: string;
  scope: ScopeMeta;
  observed_at: string | null;
  served_at: string;
  data_state: "available" | "stale" | "unavailable";
  completeness: "complete" | "partial" | "unknown";
  source_failures: {
    target_identity: string;
    status: string;
    evidence_ref: string;
  }[];
  snapshot_id: string | null;
  limitations: string[];
  supported_filters?: string[];
  supported_sorts?: string[];
};
export type PageInfo = {
  limit: 20 | 40 | 80;
  next_cursor: string | null;
  previous_cursor: string | null;
  known_total: Metric;
  snapshot_id: string;
  expires_at: string;
};
export type ApiResponse<T> = { data: T; meta: ApiMeta };
export type ListResponse<T> = ApiResponse<T[]> & { page: PageInfo };
export type ApiErrorCode =
  | "NOT_FOUND"
  | "INVALID_CURSOR"
  | "SNAPSHOT_EXPIRED"
  | "VALIDATION_ERROR"
  | "EVIDENCE_SCOPE_UNAVAILABLE"
  | "EVIDENCE_UNAVAILABLE"
  | "REPRESENTATION_UNAVAILABLE"
  | "UNAUTHENTICATED"
  | "FORBIDDEN"
  | "CONFLICT"
  | "STALE_REVISION"
  | "SOURCE_UNAVAILABLE"
  | "RATE_LIMITED"
  | "INTERNAL_ERROR"
  | "CAPABILITY_NOT_IMPLEMENTED";
export type ApiError = {
  error: {
    code: ApiErrorCode;
    message: string;
    request_id: string;
    retryable: boolean;
    details: Record<string, string>;
  };
};
export type ApplicationDestination =
  | {
      application_url: string;
      canonical_url: string | null;
      application_url_kind: "direct_apply" | "vacancy_page";
    }
  | {
      application_url: null;
      canonical_url: string | null;
      application_url_kind: "unavailable";
    };
export type MatchEvidence = {
  decision: Decision;
  matched_reasons: string[];
  rejection_reasons: string[];
  review_reasons: Fact<string[]>;
  matched_role: Fact<string>;
  preferred_term_hits: Fact<string[]>;
  matcher_version: string;
  evaluated_at: string;
  brief_revision_id: string | null;
  run_id: string | null;
};
export type PostingSummary = {
  resource_type: "posting";
  posting_id: string;
  source: string;
  source_board_id: string;
  source_job_id: string;
  title: string;
  company: string;
  location_text: string | null;
  remote_status: "remote" | "hybrid" | "onsite" | "unknown";
  first_seen_at: string | null;
  last_seen_at: string | null;
  application_destination: ApplicationDestination;
  match: MatchEvidence | null;
};
export type DeliveryState = {
  destination_id: string | null;
  previously_delivered: Fact<boolean>;
  delivery_records: string[];
  historical_suppression: Fact<boolean>;
  historical_evidence_refs: string[];
  fresh_for_delivery: Fact<boolean>;
  non_delivery_reasons: Fact<string[]>;
};
export type OutcomeSummary = Fact<"applied" | "not_applied" | "unknown"> & {
  resolution: "single" | "conflicting" | "not_recorded";
  evidence_refs: string[];
};
export type JobGroupSummary = {
  resource_type: "delivery_group";
  delivery_group_id: string;
  representative_posting: PostingSummary | null;
  representative_basis: Fact<string>;
  member_count: Metric;
  member_completeness: "complete" | "partial" | "unknown";
  match: MatchEvidence | null;
  delivery_state: DeliveryState;
  outcome_summary: OutcomeSummary;
  detail_url: string;
};
export type JobGroupDetail = JobGroupSummary & {
  members: PostingSummary[];
  members_url: string | null;
  description_text: string;
  grouping_evidence: Fact<string[]>;
  provenance: {
    evidence_ref: string;
    brief_revision_id: string | null;
    run_id: string | null;
    source_target: string;
    first_seen_at: string | null;
    last_seen_at: string | null;
  };
  capabilities: Capabilities;
};
export type Session = {
  operator_id: string;
  display_name: string;
  authorization_version: string;
  client_scopes: { client_id: string; capabilities: Capabilities }[];
};
export type Client = {
  client_id: string;
  display_name: string;
  destinations: { destination_id: string; display_name: string }[];
};
export type BriefRevision = {
  brief_id: string;
  brief_revision_id: string;
  client_id: string;
  schema_version: string;
  revision_label: string;
  content_sha256: string;
  created_at: string | null;
  registered_at: string;
  rules: {
    target_roles: string[];
    target_market: {
      countries: string[];
      intent: string;
      unknown_policy: string;
    };
    work_mode: { modes: string[]; intent: string; unknown_policy: string };
    preferred_terms: string[];
    excluded_titles: string[];
    excluded_seniority: string[];
    must_have_terms: string[];
    avoid_terms: string[];
    management_roles: string;
    employment_type: {
      types: string[];
      intent: string;
      unknown_policy: string;
    };
    work_eligibility: {
      countries: string[];
      intent: string;
      unknown_policy: string;
    };
    max_required_experience_years: number | null;
    candidate_residence: string | null;
    notes: string | null;
  };
  binding: Fact<string>;
  capabilities: Capabilities;
};
export type RunDetail = {
  run_id: string;
  plan_id: string;
  brief_revision_id: string | null;
  status: "success" | "partial" | "failure";
  started_at: string;
  completed_at: string;
  metrics: Record<string, Metric>;
  targets: {
    target_identity: string;
    source: string;
    status: string;
    explanation: string;
  }[];
  capabilities: Capabilities;
};
export type Diagnostics = {
  cohort_id: string;
  brief_revision_id: string;
  evidence_label: string;
  rows: {
    key: string;
    label: string;
    metric: Metric;
    denominator: Metric | null;
    records_url: string | null;
  }[];
};
export type HistoryEntry = {
  history_entry_id: string;
  event_type: "imported_history" | "delivery_event";
  operator_status: "applied" | "not_applied" | "unknown";
  title: string;
  company: string;
  destination_id: string | null;
  recorded_at: string;
  evidence_ref: string;
};
