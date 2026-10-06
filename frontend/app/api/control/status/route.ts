import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const workflows = {
  inventory: "refresh-live-inventory.yml",
  delivery: "client-delivery-control.yml",
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

type ProfileSnapshot = {
  action: string;
  profile_id: string;
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
  client_funnel: {
    overall: Record<string, number>;
    age_buckets: Record<string, number>;
    delivery: Record<string, number>;
  } | null;
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

function sanitizeFunnel(value: unknown): ProfileSnapshot["client_funnel"] {
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

function sanitizeProfile(value: unknown): ProfileSnapshot | null {
  if (!value || typeof value !== "object") return null;
  const source = value as Record<string, unknown>;
  const profileId = typeof source.profile_id === "string" ? source.profile_id : "";
  if (!/^[0-9a-f]{16}$/.test(profileId)) return null;
  const batchId =
    typeof source.batch_id === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      source.batch_id,
    )
      ? source.batch_id
      : null;
  return {
    action: typeof source.action === "string" ? source.action.slice(0, 64) : "unknown",
    profile_id: profileId,
    batch_id: batchId,
    batch_status: typeof source.batch_status === "string" ? source.batch_status.slice(0, 32) : null,
    requested_quota: integer(source.requested_quota),
    selected_count: integer(source.selected_count),
    shortfall: integer(source.shortfall),
    fresh_eligible_employers: integer(source.fresh_eligible_employers),
    match_eligible_postings: integer(source.match_eligible_postings),
    needs_review_postings: integer(source.needs_review_postings),
    selection_eligible_postings: integer(source.selection_eligible_postings),
    stale_posting_suppressed_groups: integer(source.stale_posting_suppressed_groups),
    company_cap_suppressed_groups: integer(source.company_cap_suppressed_groups),
    client_funnel: sanitizeFunnel(source.client_funnel),
  };
}

function parseProfileSnapshots(logs: string): ProfileSnapshot[] {
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
      return parsed.profiles.flatMap((value) => {
        const profile = sanitizeProfile(value);
        return profile ? [profile] : [];
      });
    } catch {
      continue;
    }
  }
  return [];
}

async function latestOperatorSnapshot(runs: WorkflowRun[], token: string) {
  const latest = runs[0];
  if (!latest) return { run: null, profiles: [] as ProfileSnapshot[] };
  const run = {
    id: latest.id,
    run_number: latest.run_number,
    status: latest.status,
    conclusion: latest.conclusion,
    created_at: latest.created_at,
    updated_at: latest.updated_at,
    url: latest.url,
  };
  if (latest.status !== "completed") return { run, profiles: [] as ProfileSnapshot[] };

  try {
    const jobs = await github<{ jobs: GithubJob[] }>(
      `/repos/${owner}/${repo}/actions/runs/${latest.id}/jobs?per_page=100`,
      token,
    );
    const persist = jobs.jobs.find((job) => job.name === "persist-and-deliver");
    if (!persist || persist.conclusion !== "success") {
      return { run, profiles: [] as ProfileSnapshot[] };
    }
    const logs = await githubText(
      `/repos/${owner}/${repo}/actions/jobs/${persist.id}/logs`,
      token,
    );
    return { run, profiles: parseProfileSnapshots(logs) };
  } catch {
    return { run, profiles: [] as ProfileSnapshot[] };
  }
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
    const [inventory, delivery] = await Promise.all([
      workflowStatus(workflows.inventory, token),
      workflowStatus(workflows.delivery, token),
    ]);
    const operatorSnapshot = await latestOperatorSnapshot(inventory.runs, token);
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
