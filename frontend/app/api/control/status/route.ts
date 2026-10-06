import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const workflows = {
  inventory: "refresh-live-inventory.yml",
  delivery: "client-delivery-control.yml",
  configure: "configure-client-delivery-profile.yml",
} as const;

type GithubRun = {
  id: number;
  event: string;
  status: string;
  conclusion: string | null;
  created_at: string;
  updated_at: string;
  html_url: string;
  run_number: number;
  head_sha: string;
};

type GithubWorkflow = {
  id: number;
  name: string;
  state: string;
  html_url: string;
};

type GithubJob = {
  id: number;
  name: string;
  status: string;
  conclusion: string | null;
};

type WorkflowRun = {
  id: number;
  run_number: number;
  event: string;
  status: string;
  conclusion: string | null;
  created_at: string;
  updated_at: string;
  url: string;
  head_sha: string;
};

type FunnelSnapshot = {
  overall: Record<string, number>;
  age_buckets: Record<string, number>;
  delivery: Record<string, number>;
};

type ProfileSnapshot = {
  action: string;
  profile_id: string;
  destination_name: string | null;
  profile_status: string | null;
  delivery_mode: string | null;
  daily_quota: number | null;
  sheet_status: string | null;
  delivered_today: number | null;
  batch_id: string | null;
  batch_status: string | null;
  requested_quota: number | null;
  selected_count: number | null;
  shortfall: number | null;
  fresh_eligible_employers: number | null;
  match_eligible_postings: number | null;
  needs_review_postings: number | null;
  selection_eligible_postings: number | null;
  stale_posting_suppressed_groups: number | null;
  company_cap_suppressed_groups: number | null;
  client_funnel: FunnelSnapshot | null;
};

type OperatorState = {
  schema_version: string;
  profiles: unknown[];
  truncated: boolean;
};

function githubHeaders(token: string) {
  return {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "JobSift-operator-control",
    Authorization: `Bearer ${token}`,
  };
}

async function github<T>(path: string, token: string): Promise<T> {
  const response = await fetch(`https://api.github.com${path}`, {
    headers: githubHeaders(token),
    cache: "no-store",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error("GitHub control status is unavailable.");
  return (await response.json()) as T;
}

async function githubText(path: string, token: string): Promise<string> {
  const response = await fetch(`https://api.github.com${path}`, {
    headers: githubHeaders(token),
    cache: "no-store",
    redirect: "follow",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error("GitHub run details are unavailable.");
  return response.text();
}

async function workflowStatus(workflow: string, token: string) {
  const [definition, runs] = await Promise.all([
    github<GithubWorkflow>(
      `/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(workflow)}`,
      token,
    ),
    github<{ workflow_runs: GithubRun[] }>(
      `/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(workflow)}/runs?per_page=8`,
      token,
    ),
  ]);
  return {
    name: definition.name,
    state: definition.state,
    url: definition.html_url,
    runs: runs.workflow_runs.map((run) => ({
      id: run.id,
      run_number: run.run_number,
      event: run.event,
      status: run.status,
      conclusion: run.conclusion,
      created_at: run.created_at,
      updated_at: run.updated_at,
      url: run.html_url,
      head_sha: run.head_sha,
    })),
  };
}

function integer(value: unknown): number | null {
  return Number.isInteger(value) ? (value as number) : null;
}

function textValue(value: unknown, max = 160): string | null {
  return typeof value === "string" && value.trim()
    ? value.trim().slice(0, max)
    : null;
}

function numericRecord(value: unknown, allowed: readonly string[]): Record<string, number> {
  if (!value || typeof value !== "object") return {};
  const source = value as Record<string, unknown>;
  return Object.fromEntries(
    allowed.flatMap((key) => {
      const parsed = integer(source[key]);
      return parsed === null ? [] : [[key, parsed]];
    }),
  );
}

function sanitizeFunnel(value: unknown): FunnelSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const source = value as Record<string, unknown>;
  const overallSource =
    source.overall && typeof source.overall === "object"
      ? (source.overall as Record<string, unknown>)
      : {};
  return {
    overall: numericRecord(overallSource, [
      "retained_evaluated",
      "retained_confirmed_matches",
      "retained_needs_review_matches",
      "retained_rejected",
      "fresh_0_24h",
      "title_matched_0_24h",
      "target_market_survived_0_24h",
      "remote_survived_0_24h",
      "other_rules_survived_0_24h",
      "confirmed_matches_0_24h",
      "needs_review_matches_0_24h",
      "rejected_0_24h",
    ]),
    age_buckets: numericRecord(overallSource.match_age_buckets, [
      "age_0_24h",
      "age_24_48h",
      "age_48_72h",
      "age_over_72h",
      "unknown_age",
      "invalid_time",
    ]),
    delivery: numericRecord(source.delivery, [
      "historically_suppressed_groups",
      "previously_delivered_groups",
      "duplicate_postings_collapsed",
      "fresh_eligible_groups",
      "fresh_eligible_employers",
      "employer_cooldown_suppressed_groups",
      "stale_posting_suppressed_groups",
      "unknown_age_suppressed_groups",
      "invalid_time_suppressed_groups",
      "company_cap_suppressed_groups",
      "selected_count",
      "shortfall",
    ]),
  };
}

function validProfileId(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{16}$/.test(value);
}

function validBatchId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      value,
    )
  );
}

function authoritativeProfile(value: unknown): ProfileSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const source = value as Record<string, unknown>;
  if (!validProfileId(source.profile_id)) return null;
  const pending =
    source.pending_batch && typeof source.pending_batch === "object"
      ? (source.pending_batch as Record<string, unknown>)
      : null;
  const counts =
    pending?.counts && typeof pending.counts === "object"
      ? (pending.counts as Record<string, unknown>)
      : {};
  const batchId = validBatchId(pending?.batch_id) ? pending.batch_id : null;
  return {
    action: batchId ? "awaiting_release" : "ready",
    profile_id: source.profile_id,
    destination_name: textValue(source.destination_name),
    profile_status: textValue(source.profile_status, 32),
    delivery_mode: textValue(source.delivery_mode, 32),
    daily_quota: integer(source.daily_quota),
    sheet_status: textValue(source.sheet_status, 32),
    delivered_today: integer(source.delivered_today),
    batch_id: batchId,
    batch_status: textValue(pending?.status, 32),
    requested_quota: integer(pending?.requested_quota),
    selected_count: integer(pending?.selected_count),
    shortfall: integer(pending?.shortfall),
    fresh_eligible_employers: integer(counts.fresh_eligible_employers),
    match_eligible_postings: integer(counts.match_eligible_postings),
    needs_review_postings: integer(counts.needs_review_postings),
    selection_eligible_postings: integer(counts.selection_eligible_postings),
    stale_posting_suppressed_groups: integer(counts.stale_posting_suppressed_groups),
    company_cap_suppressed_groups: integer(counts.company_cap_suppressed_groups),
    client_funnel: null,
  };
}

function parseAuthoritativeState(logs: string): ProfileSnapshot[] {
  const ansi = /\u001b\[[0-9;]*m/g;
  const marker = "JOBSIFT_OPERATOR_STATE=";
  const lines = logs.replace(ansi, "").split("\n").reverse();
  for (const line of lines) {
    const start = line.indexOf(marker);
    if (start < 0) continue;
    try {
      const parsed = JSON.parse(line.slice(start + marker.length).trim()) as OperatorState;
      if (parsed.schema_version !== "operator-state-v1" || !Array.isArray(parsed.profiles)) {
        continue;
      }
      return parsed.profiles.flatMap((value) => {
        const profile = authoritativeProfile(value);
        return profile ? [profile] : [];
      });
    } catch {
      continue;
    }
  }
  return [];
}

function parseEvaluationProfiles(logs: string): Map<string, FunnelSnapshot> {
  const ansi = /\u001b\[[0-9;]*m/g;
  const lines = logs.replace(ansi, "").split("\n").reverse();
  for (const line of lines) {
    const marker = '{"profiles":';
    const start = line.indexOf(marker);
    if (start < 0) continue;
    const candidate = line.slice(start).trim();
    const end = candidate.lastIndexOf("}");
    if (end < 0) continue;
    try {
      const parsed = JSON.parse(candidate.slice(0, end + 1)) as { profiles?: unknown[] };
      if (!Array.isArray(parsed.profiles)) continue;
      const funnels = new Map<string, FunnelSnapshot>();
      for (const value of parsed.profiles) {
        if (!value || typeof value !== "object") continue;
        const source = value as Record<string, unknown>;
        if (!validProfileId(source.profile_id)) continue;
        const funnel = sanitizeFunnel(source.client_funnel);
        if (funnel) funnels.set(source.profile_id, funnel);
      }
      if (funnels.size) return funnels;
    } catch {
      continue;
    }
  }
  return new Map();
}

type SnapshotCandidate = {
  kind: "inventory" | "delivery" | "configure";
  run: WorkflowRun;
  jobName: "operator-snapshot" | "control" | "configure";
};

async function jobLogsForRun(candidate: SnapshotCandidate, token: string) {
  if (candidate.run.status !== "completed") return null;
  try {
    const jobs = await github<{ jobs: GithubJob[] }>(
      `/repos/${owner}/${repo}/actions/runs/${candidate.run.id}/jobs?per_page=100`,
      token,
    );
    const job = jobs.jobs.find((value) => value.name === candidate.jobName);
    if (!job || job.status !== "completed") return null;
    return await githubText(
      `/repos/${owner}/${repo}/actions/jobs/${job.id}/logs`,
      token,
    );
  } catch {
    return null;
  }
}

async function latestEvaluationFunnels(inventoryRuns: WorkflowRun[], token: string) {
  for (const run of inventoryRuns) {
    if (run.status !== "completed") continue;
    try {
      const jobs = await github<{ jobs: GithubJob[] }>(
        `/repos/${owner}/${repo}/actions/runs/${run.id}/jobs?per_page=100`,
        token,
      );
      const persist = jobs.jobs.find((value) => value.name === "persist-and-deliver");
      if (!persist || persist.conclusion !== "success") continue;
      const logs = await githubText(
        `/repos/${owner}/${repo}/actions/jobs/${persist.id}/logs`,
        token,
      );
      const funnels = parseEvaluationProfiles(logs);
      if (funnels.size) return funnels;
    } catch {
      continue;
    }
  }
  return new Map<string, FunnelSnapshot>();
}

async function latestOperatorSnapshot(
  inventoryRuns: WorkflowRun[],
  deliveryRuns: WorkflowRun[],
  configureRuns: WorkflowRun[],
  token: string,
) {
  const candidates: SnapshotCandidate[] = [
    ...inventoryRuns.map((run) => ({
      kind: "inventory" as const,
      run,
      jobName: "operator-snapshot" as const,
    })),
    ...deliveryRuns.map((run) => ({
      kind: "delivery" as const,
      run,
      jobName: "control" as const,
    })),
    ...configureRuns.map((run) => ({
      kind: "configure" as const,
      run,
      jobName: "configure" as const,
    })),
  ].sort(
    (left, right) => Date.parse(right.run.updated_at) - Date.parse(left.run.updated_at),
  );

  let chosen: SnapshotCandidate | null = null;
  let profiles: ProfileSnapshot[] = [];
  for (const candidate of candidates) {
    const logs = await jobLogsForRun(candidate, token);
    if (!logs) continue;
    const parsed = parseAuthoritativeState(logs);
    if (!parsed.length && !logs.includes("JOBSIFT_OPERATOR_STATE=")) continue;
    chosen = candidate;
    profiles = parsed;
    break;
  }

  const funnels = await latestEvaluationFunnels(inventoryRuns, token);
  profiles = profiles.map((profile) => ({
    ...profile,
    client_funnel: funnels.get(profile.profile_id) ?? null,
  }));

  const run = chosen
    ? {
        id: chosen.run.id,
        run_number: chosen.run.run_number,
        status: chosen.run.status,
        conclusion: chosen.run.conclusion,
        created_at: chosen.run.created_at,
        updated_at: chosen.run.updated_at,
        url: chosen.run.url,
        kind: chosen.kind,
      }
    : null;

  return { run, profiles };
}

export async function GET() {
  const token = process.env.JOBSIFT_GITHUB_TOKEN?.trim() ?? "";
  if (!token) {
    const unavailable = (name: string, workflow: string) => ({
      name,
      state: "unavailable",
      url: `https://github.com/${owner}/${repo}/actions/workflows/${workflow}`,
      runs: [],
    });
    return NextResponse.json(
      {
        data: {
          control_ready: false,
          inventory: unavailable("Refresh Live Job Inventory", workflows.inventory),
          delivery: unavailable("Client Delivery Control", workflows.delivery),
          operator_snapshot: { run: null, profiles: [] },
        },
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  }

  try {
    const [inventory, delivery, configure] = await Promise.all([
      workflowStatus(workflows.inventory, token),
      workflowStatus(workflows.delivery, token),
      workflowStatus(workflows.configure, token),
    ]);
    const operatorSnapshot = await latestOperatorSnapshot(
      inventory.runs,
      delivery.runs,
      configure.runs,
      token,
    );
    return NextResponse.json(
      {
        data: {
          control_ready: true,
          inventory,
          delivery,
          operator_snapshot: operatorSnapshot,
        },
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json(
      {
        error: {
          code: "CONTROL_STATUS_UNAVAILABLE",
          message: "Could not load JobSift workflow status.",
        },
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
