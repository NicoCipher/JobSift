import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const inventoryWorkflow = "refresh-live-inventory.yml";
const deliveryWorkflow = "client-delivery-control.yml";

const workdayTargets = new Set(["1", "5", "10", "20", "25"]);
const workdayConcurrency = new Set(["4", "6", "8"]);
const deliveryOperations = new Set([
  "list",
  "status",
  "pause",
  "resume",
  "set-quota",
  "set-mode",
  "set-timezone",
  "run-now",
  "release-batch",
  "discard-batch",
]);

function clean(value: unknown, max = 160): string {
  return typeof value === "string" ? value.trim().slice(0, max) : "";
}

function sameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  return (!origin || origin === request.nextUrl.origin) && (!site || site === "same-origin");
}

function invalid(message: string) {
  return NextResponse.json(
    { error: { code: "VALIDATION_ERROR", message } },
    { status: 400, headers: { "Cache-Control": "no-store" } },
  );
}

async function dispatch(workflow: string, inputs: Record<string, string>) {
  const token = process.env.JOBSIFT_GITHUB_TOKEN?.trim();
  if (!token) {
    return NextResponse.json(
      {
        error: {
          code: "CONTROL_NOT_CONFIGURED",
          message: "Production controls are not configured on this deployment.",
        },
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "JobSift-operator-control",
      },
      body: JSON.stringify({ ref: "main", inputs }),
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    },
  );
  if (response.status !== 204) {
    return NextResponse.json(
      {
        error: {
          code: "CONTROL_DISPATCH_FAILED",
          message: "GitHub did not accept the JobSift command.",
        },
      },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
  return NextResponse.json(
    { data: { accepted: true, workflow, inputs } },
    { status: 202, headers: { "Cache-Control": "no-store" } },
  );
}

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) {
    return NextResponse.json(
      { error: { code: "FORBIDDEN", message: "Cross-origin control requests are not allowed." } },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (!request.headers.get("content-type")?.startsWith("application/json")) {
    return invalid("Expected a JSON control request.");
  }

  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return invalid("Invalid JSON control request.");
  }

  const command = clean(body.command, 40);
  if (command === "inventory-refresh") {
    const targets = clean(body.workday_targets, 4);
    const concurrency = clean(body.workday_detail_concurrency, 2);
    if (!workdayTargets.has(targets) || !workdayConcurrency.has(concurrency)) {
      return invalid("Unsupported guarded Workday settings.");
    }
    return dispatch(inventoryWorkflow, {
      workday_targets: targets,
      workday_detail_concurrency: concurrency,
    });
  }

  if (command === "client-control") {
    const operation = clean(body.operation, 24);
    if (!deliveryOperations.has(operation)) return invalid("Unsupported client control operation.");

    const profileId = clean(body.profile_id, 64);
    const batchId = clean(body.batch_id, 160);
    const quota = clean(body.daily_quota, 8);
    const mode = clean(body.delivery_mode, 16);
    const timezone = clean(body.timezone, 80);

    if (!["list"].includes(operation) && !profileId) {
      return invalid("A delivery profile ID is required.");
    }
    if (operation === "set-quota") {
      const parsed = Number(quota);
      if (!Number.isInteger(parsed) || parsed < 1 || parsed > 5000) {
        return invalid("Daily quota must be an integer from 1 to 5000.");
      }
    }
    if (operation === "set-mode" && !["review", "auto"].includes(mode)) {
      return invalid("Delivery mode must be review or auto.");
    }
    if (operation === "set-timezone" && !timezone) {
      return invalid("Timezone is required.");
    }
    if (["release-batch", "discard-batch"].includes(operation) && !batchId) {
      return invalid("Batch ID is required.");
    }

    return dispatch(deliveryWorkflow, {
      operation,
      profile_id: profileId,
      daily_quota: quota || "100",
      delivery_mode: mode || "review",
      timezone: timezone || "Africa/Lagos",
      batch_id: batchId,
    });
  }

  return invalid("Unsupported JobSift control command.");
}
