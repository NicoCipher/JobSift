/** Entirely fictional development evidence. No runtime sourcing or domain evaluation. */
import type {
  ApiMeta,
  BriefRevision,
  Capabilities,
  Client,
  Diagnostics,
  Fact,
  HistoryEntry,
  JobGroupDetail,
  MatchEvidence,
  PostingSummary,
  Metric,
  RunDetail,
  Session,
} from "../contracts/service";
export const CLIENT_ID = "example-client";
export const DESTINATION_ID = "example-destination";
export const reported = <T>(value: T): Fact<T> => ({
  value,
  availability: "reported",
});
export const notReported = <T>(): Fact<T> => ({
  value: null,
  availability: "not_reported",
});
export const metric = (
  value: number | null,
  unit: string,
  definition: string,
): Metric =>
  value === null
    ? { ...notReported<number>(), unit, definition }
    : { ...reported(value), unit, definition };
export const capabilities: Capabilities = Object.fromEntries(
  [
    "can_edit_outcome",
    "can_create_brief_revision",
    "can_activate_brief",
    "can_start_run",
    "can_retry_run",
    "can_cancel_run",
    "can_view_raw_provenance",
    "can_manage_clients",
  ].map((key) => [key, { allowed: false, reason: "not_implemented" }]),
);
for (const key of [
  "can_read_jobs",
  "can_read_runs",
  "can_read_history",
  "can_read_diagnostics",
  "can_read_briefs",
])
  capabilities[key] = { allowed: true, reason: null };
export const session: Session = {
  operator_id: "example-operator",
  display_name: "Demo operator",
  authorization_version: "example-grants-1",
  client_scopes: [{ client_id: CLIENT_ID, capabilities }],
};
export const client: Client = {
  client_id: CLIENT_ID,
  display_name: "Example client",
  destinations: [
    {
      destination_id: DESTINATION_ID,
      display_name: "Example delivery destination",
    },
  ],
};
export const meta: ApiMeta = {
  request_id: "example-request",
  scope: {
    client_id: CLIENT_ID,
    brief_revision_id: "example-revision-v2",
    run_id: "example-run",
    destination_id: DESTINATION_ID,
    cohort_id: "example-cohort",
  },
  observed_at: "2026-09-10T10:05:00Z",
  served_at: "2026-09-11T00:00:00Z",
  data_state: "available",
  completeness: "partial",
  source_failures: [
    {
      target_identity: "workday:example-host:example-tenant:example-site",
      status: "network_failure",
      evidence_ref: "example-source-failure",
    },
  ],
  snapshot_id: null,
  limitations: ["fictional_development_evidence", "partial_source_coverage"],
  supported_filters: ["q", "decision", "source"],
  supported_sorts: ["decision"],
};
const titles = [
  "Technical Support Specialist",
  "IT Support Specialist",
  "Service Desk Analyst",
  "Desktop Support Technician",
  "Technical Support Engineer",
  "Help Desk Technician",
  "Junior Systems Administrator",
  "Support Engineer",
];
const companies = [
  "Alder Systems",
  "Northline Labs",
  "Fieldwork Software",
  "Meridian Tools",
  "Cedar Networks",
  "Harbor Systems",
  "Slate Software",
  "Waypoint Labs",
  "Common Ground",
  "Loom Networks",
  "Pine Engineering",
  "Signal Works",
  "Elm Platforms",
];
// Fixture authoring recipe only: decisions are assigned, not evaluated from title/content.
// 44 eligible-category examples guarantee a second 40-row page; 8 diagnostic cases are explicit filters.
export const groups: JobGroupDetail[] = Array.from(
  { length: 52 },
  (_, index) => {
    const id = `example-group-${String(index + 1).padStart(2, "0")}`;
    const decision =
      index < 20
        ? "strong_match"
        : index < 44
          ? "possible_match"
          : index < 48
            ? "needs_review"
            : "reject";
    const match: MatchEvidence = {
      decision,
      matched_reasons:
        decision === "reject"
          ? []
          : [
              "Role title matches the configured vocabulary.",
              "The posting names the United States as its target market.",
            ],
      rejection_reasons:
        decision === "reject"
          ? ["The posting requires an excluded management role."]
          : [],
      review_reasons:
        decision === "needs_review"
          ? reported(["The source does not establish a work mode."])
          : notReported<string[]>(),
      matched_role: notReported<string>(),
      preferred_term_hits: notReported<string[]>(),
      matcher_version: "example-deterministic",
      evaluated_at: "2026-09-10T10:01:00Z",
      brief_revision_id: "example-revision-v2",
      run_id: "example-run",
    } as const;
    const unavailable = index === 2 || decision === "needs_review";
    const posting: PostingSummary = {
      resource_type: "posting",
      posting_id: `example-posting-${index + 1}`,
      source: ["greenhouse", "ashby", "lever"][index % 3],
      source_board_id: "example-board",
      source_job_id: `example-provider-${index + 1}`,
      title: titles[index % titles.length],
      company: companies[index % companies.length],
      location_text: unavailable ? null : "United States",
      remote_status: decision === "needs_review" ? "unknown" : "remote",
      first_seen_at: "2026-09-10T10:00:00Z",
      last_seen_at: "2026-09-10T10:00:00Z",
      application_destination:
        index === 2
          ? {
              application_url: null,
              canonical_url: null,
              application_url_kind: "unavailable",
            }
          : {
              application_url: `https://jobs.example.com/vacancy/${index + 1}${index % 2 === 0 ? "/application" : ""}`,
              canonical_url: `https://jobs.example.com/vacancy/${index + 1}`,
              application_url_kind:
                index % 2 === 0 ? "direct_apply" : "vacancy_page",
            },
      match,
    } as const;
    const members =
      index === 0
        ? [
            posting,
            {
              ...posting,
              posting_id: "example-posting-alternate",
              source: "ashby",
              source_job_id: "example-alternate",
            },
          ]
        : [posting];
    return {
      resource_type: "delivery_group",
      delivery_group_id: id,
      representative_posting: posting,
      representative_basis: reported(
        index >= 44 ? "recorded_delivery" : "selection_in_scope",
      ),
      member_count: metric(
        members.length,
        "postings",
        "authorized_group_members",
      ),
      member_completeness: "complete",
      match,
      delivery_state: {
        destination_id: DESTINATION_ID,
        previously_delivered: reported(index === 1 || index >= 44),
        delivery_records:
          index === 1 || index >= 44 ? ["example-delivery-event"] : [],
        historical_suppression:
          index === 2
            ? { value: null, availability: "unknown" }
            : reported(index === 3),
        historical_evidence_refs: index === 3 ? ["example-history-import"] : [],
        fresh_for_delivery: notReported<boolean>(),
        non_delivery_reasons:
          index === 1
            ? reported([
                "Previously delivered to Example delivery destination on Sep 8, 2026.",
              ])
            : index === 3
              ? reported([
                  "Previously surfaced in imported history; matched by exact source identity.",
                ])
              : notReported<string[]>(),
      },
      outcome_summary:
        index === 1
          ? {
              ...reported("applied" as const),
              resolution: "single",
              evidence_refs: ["example-outcome"],
            }
          : index === 3
            ? {
                ...reported("not_applied" as const),
                resolution: "single",
                evidence_refs: ["example-history-import"],
              }
            : {
                ...notReported<"applied" | "not_applied" | "unknown">(),
                resolution: "not_recorded",
                evidence_refs: [],
              },
      detail_url: `/api/v1/clients/${CLIENT_ID}/jobs/groups/${id}`,
      members: [...members],
      members_url: null,
      description_text:
        "Fictional vacancy for interface review. The role supports users, investigates technical incidents, and maintains clear resolution notes. This is not a live vacancy or an invitation to apply.",
      grouping_evidence:
        index === 0
          ? reported([
              "Exact vacancy URL evidence — fictional dedupe-v1 fixture.",
            ])
          : notReported<string[]>(),
      provenance: {
        evidence_ref: `example-evidence-${index + 1}`,
        brief_revision_id: "example-revision-v2",
        run_id: "example-run",
        source_target: `${posting.source}:example-board`,
        first_seen_at: posting.first_seen_at,
        last_seen_at: posting.last_seen_at,
      },
      capabilities,
    };
  },
);
export const brief: BriefRevision = {
  brief_id: "example-brief",
  brief_revision_id: "example-revision-v2",
  client_id: CLIENT_ID,
  schema_version: "operator-style-sourcing-brief-v1",
  revision_label: "V2",
  content_sha256: "a".repeat(64),
  created_at: null,
  registered_at: "2026-09-10T10:00:00Z",
  rules: {
    target_roles: titles,
    target_market: {
      countries: ["United States"],
      intent: "must",
      unknown_policy: "review",
    },
    work_mode: { modes: ["remote"], intent: "must", unknown_policy: "review" },
    preferred_terms: ["Networking", "Linux", "Troubleshooting"],
    excluded_titles: ["Customer Support Manager", "Sales Support"],
    excluded_seniority: [],
    must_have_terms: [],
    avoid_terms: [],
    management_roles: "avoid",
    employment_type: { types: [], intent: "ignore", unknown_policy: "review" },
    work_eligibility: {
      countries: [],
      intent: "ignore",
      unknown_policy: "review",
    },
    max_required_experience_years: null,
    candidate_residence: null,
    notes: "Fictional development brief.",
  },
  binding: notReported<string>(),
  capabilities,
};
export const run: RunDetail = {
  run_id: "example-run",
  plan_id: "example-plan",
  brief_revision_id: "example-revision-v2",
  status: "partial",
  started_at: "2026-09-10T10:00:00Z",
  completed_at: "2026-09-10T10:05:00Z",
  metrics: {
    received: metric(52, "postings", "received_in_available_results"),
    matched: metric(44, "postings", "strong_or_possible_decisions"),
    rejected: metric(8, "postings", "legacy_non_delivery_eligible_decisions"),
    exported: metric(0, "exports", "csv_rows_appended"),
    normalized: metric(null, "postings", "normalized_postings"),
    requests: metric(null, "requests", "provider_requests"),
  },
  targets: [
    {
      target_identity: "greenhouse:example-board",
      source: "Greenhouse",
      status: "success",
      explanation: "Results retained.",
    },
    {
      target_identity: "ashby:example-board",
      source: "Ashby",
      status: "success",
      explanation: "Results retained.",
    },
    {
      target_identity: "lever:example-board",
      source: "Lever",
      status: "success",
      explanation: "Results retained.",
    },
    {
      target_identity: "workday:example-host:example-tenant:example-site",
      source: "Workday",
      status: "network_failure",
      explanation: "Network failure; coverage is unavailable for this target.",
    },
  ],
  capabilities,
};
export const diagnostics: Diagnostics = {
  cohort_id: "example-frozen-baseline",
  brief_revision_id: "example-revision-v2",
  evidence_label:
    "Frozen V2 equivalent baseline — evidence fixture, not live inventory",
  rows: [
    {
      key: "eligible",
      label: "Eligible postings",
      metric: metric(62, "postings", "frozen_v2_eligible_postings"),
      denominator: metric(48710, "postings", "frozen_evaluated_postings"),
      records_url: null,
    },
    {
      key: "equivalent",
      label: "Equivalent fresh groups",
      metric: metric(56, "groups", "frozen_v2_equivalent_fresh_groups"),
      denominator: null,
      records_url: null,
    },
  ],
};
export const history: HistoryEntry[] = [
  {
    history_entry_id: "example-history-import",
    event_type: "imported_history",
    operator_status: "not_applied",
    title: "Desktop Support Technician",
    company: "Meridian Tools",
    destination_id: null,
    recorded_at: "2026-09-08T10:00:00Z",
    evidence_ref: "Example workbook · Jobs · row 2; exact source identity",
  },
  {
    history_entry_id: "example-delivery-event",
    event_type: "delivery_event",
    operator_status: "unknown",
    title: "IT Support Specialist",
    company: "Northline Labs",
    destination_id: DESTINATION_ID,
    recorded_at: "2026-09-08T10:00:00Z",
    evidence_ref: "Example recorded delivery; not an application event",
  },
];
