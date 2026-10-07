import { randomUUID } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { validBatchId } from "../../../../lib/control-validation";
import { isOperatorProfileAllowed } from "../../../../lib/operator-profiles";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const workflow = "client-review-batch.yml";
const reviewTitlePrefix = "Review client batch · ";

type GithubRun = {
  id: number;
  status: string;
  conclusion: string | null;
  display_title?: string | null;
};

type ReviewItem = {
  ordinal: number;
  title: string;
  company: string;
  application_link: string | null;
  source: string;
  posted_at: string | null;
  age_hours: number | null;
  location: string | null;
  remote_status: string;
  decision: string;
  matched_reasons: string[];
  review_reasons: string[];
  evidence_verified: boolean;
  release_ready: boolean;
  warnings: string[];
};

type ReviewSnapshot = {
  observed_at: string;
  profile_id: string;
  batch_id: string;
  batch_status: string;
  selected_count: number;
  requested_quota: number;
  freshness_limit_hours: number | null;
  safe_to_release: boolean;
  recovery_required: boolean;
  error: string | null;
  items: ReviewItem[];
};

function githubToken() {
  return process.env.JOBSIFT_GITHUB_TOKEN?.trim() ?? "";
}

function sameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  return (!origin || origin === request.nextUrl.origin) && (!site || site === "same-origin");
}

function noStore(data: unknown, status = 200) {
  return NextResponse.json(data, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function invalid(message: string) {
  return noStore({ error: { code: "VALIDATION_ERROR", message } }, 400);
}

function unavailable() {
  return noStore(
    {
      error: {
        code: "CONTROL_NOT_CONFIGURED",
        message: "Production review controls are not configured on this deployment.",
      },
    },
    503,
  );
}

function validControlRequestId(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    value,
  );
}

function cleanString(value: unknown, max = 500): string {
  return typeof value === "string" ? value.trim().slice(0, max) : "";
}

function stringArray(value: unknown, maxItems = 40): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === "string")
    .slice(0, maxItems)
    .map((item) => item.trim().slice(0, 500))
    .filter(Boolean);
}

function sanitizeReview(raw: unknown): ReviewSnapshot | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const profileId = cleanString(value.profile_id, 64).toLowerCase();
  const batchId = cleanString(value.batch_id, 160);
  if (
    !/^[0-9a-f]{16}$/.test(profileId) ||
    !validBatchId(batchId) ||
    !isOperatorProfileAllowed(process.env.JOBSIFT_OPERATOR_PROFILES, profileId)
  ) {
    return null;
  }

  const rawItems = Array.isArray(value.items) ? value.items.slice(0, 5000) : [];
  const items: ReviewItem[] = [];
  for (const rawItem of rawItems) {
    if (!rawItem || typeof rawItem !== "object") return null;
    const item = rawItem as Record<string, unknown>;
    const ordinal = Number(item.ordinal);
    if (!Number.isInteger(ordinal) || ordinal < 1) return null;
    const age =
      typeof item.age_hours === "number" && Number.isFinite(item.age_hours)
        ? item.age_hours
        : null;
    items.push({
      ordinal,
      title: cleanString(item.title, 300),
      company: cleanString(item.company, 300),
      application_link: cleanString(item.application_link, 2000) || null,
      source: cleanString(item.source, 120),
      posted_at: cleanString(item.posted_at, 80) || null,
      age_hours: age,
      location: cleanString(item.location, 300) || null,
      remote_status: cleanString(item.remote_status, 40) || "unknown",
      decision: cleanString(item.decision, 40),
      matched_reasons: stringArray(item.matched_reasons),
      review_reasons: stringArray(item.review_reasons),
      evidence_verified: item.evidence_verified === true,
      release_ready: item.release_ready === true,
      warnings: stringArray(item.warnings),
    });
  }

  const selectedCount = Number(value.selected_count);
  const requestedQuota = Number(value.requested_quota);
  if (
    !Number.isInteger(selectedCount) ||
    selectedCount < 0 ||
    !Number.isInteger(requestedQuota) ||
    requestedQuota < selectedCount ||
    selectedCount !== items.length
  ) {
    return null;
  }

  return {
    observed_at: cleanString(value.observed_at, 80),
    profile_id: profileId,
    batch_id: batchId,
    batch_status: cleanString(value.batch_status, 40),
    selected_count: selectedCount,
    requested_quota: requestedQuota,
    freshness_limit_hours:
      typeof value.freshness_limit_hours === "number"
        ? value.freshness_limit_hours
        : null,
    safe_to_release: value.safe_to_release === true,
    recovery_required: value.recovery_required === true,
    error: cleanString(value.error, 1000) || null,
    items,
  };
}

async function github(path: string, init?: RequestInit) {
  const token = githubToken();
  if (!token) throw new Error("CONTROL_NOT_CONFIGURED");
  return fetch(`https://api.github.com/repos/${owner}/${repo}${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "JobSift-operator-review",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
    signal: AbortSignal.timeout(10000),
  });
}

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) {
    return noStore(
      { error: { code: "FORBIDDEN", message: "Cross-origin review requests are not allowed." } },
      403,
    );
  }
  if (!githubToken()) return unavailable();
  if (!request.headers.get("content-type")?.startsWith("application/json")) {
    return invalid("Expected a JSON review request.");
  }

  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return invalid("Invalid JSON review request.");
  }

  const profileId = cleanString(body.profile_id, 64).toLowerCase();
  const batchId = cleanString(body.batch_id, 160);
  if (!/^[0-9a-f]{16}$/.test(profileId)) {
    return invalid("A valid delivery profile is required.");
  }
  if (!isOperatorProfileAllowed(process.env.JOBSIFT_OPERATOR_PROFILES, profileId)) {
    return noStore(
      { error: { code: "FORBIDDEN", message: "This client is not in the operator catalogue." } },
      403,
    );
  }
  if (!validBatchId(batchId)) return invalid("A valid prepared review batch is required.");

  const controlRequestId = randomUUID();
  const response = await github(`/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ref: "main",
      inputs: {
        profile_id: profileId,
        batch_id: batchId,
        control_request_id: controlRequestId,
      },
    }),
  });
  if (response.status !== 204) {
    return noStore(
      {
        error: {
          code: "REVIEW_DISPATCH_FAILED",
          message: "JobSift could not start the authoritative review read.",
        },
      },
      502,
    );
  }
  return noStore(
    { data: { state: "queued", control_request_id: controlRequestId } },
    202,
  );
}

export async function GET(request: NextRequest) {
  if (!githubToken()) return unavailable();
  const controlRequestId =
    request.nextUrl.searchParams.get("control_request_id")?.trim().toLowerCase() ?? "";
  if (!validControlRequestId(controlRequestId)) {
    return invalid("A valid review request ID is required.");
  }

  try {
    const runsResponse = await github(
      `/actions/workflows/${workflow}/runs?event=workflow_dispatch&per_page=100`,
    );
    if (!runsResponse.ok) throw new Error("runs");
    const runsBody = (await runsResponse.json()) as { workflow_runs?: GithubRun[] };
    const title = reviewTitlePrefix + controlRequestId;
    const run = (runsBody.workflow_runs ?? []).find(
      (item) => item.display_title === title,
    );
    if (!run) return noStore({ data: { state: "queued" } });
    if (run.status !== "completed") {
      return noStore({ data: { state: run.status || "running" } });
    }
    if (run.conclusion !== "success") {
      return noStore({
        data: {
          state: "failed",
          message: "JobSift could not verify this review batch. Refresh client state before retrying.",
        },
      });
    }

    const jobsResponse = await github(`/actions/runs/${run.id}/jobs?per_page=100`);
    if (!jobsResponse.ok) throw new Error("jobs");
    const jobsBody = (await jobsResponse.json()) as {
      jobs?: Array<{ id: number; name: string }>;
    };
    const reviewJob = (jobsBody.jobs ?? []).find((job) => job.name === "review");
    if (!reviewJob) throw new Error("review job");

    const logsResponse = await github(`/actions/jobs/${reviewJob.id}/logs`);
    if (!logsResponse.ok) throw new Error("logs");
    const logs = await logsResponse.text();
    const marker = "JOBSIFT_REVIEW_STATE=";
    const lines = logs.split("\n");
    const line = [...lines].reverse().find((candidate) => candidate.includes(marker));
    if (!line) throw new Error("review evidence");
    const payloadText = line.slice(line.indexOf(marker) + marker.length).trim();
    const review = sanitizeReview(JSON.parse(payloadText));
    if (!review) throw new Error("invalid review evidence");

    return noStore({ data: { state: "ready", review } });
  } catch {
    return noStore(
      {
        error: {
          code: "REVIEW_STATUS_FAILED",
          message: "JobSift could not read the authoritative review evidence.",
        },
      },
      502,
    );
  }
}
