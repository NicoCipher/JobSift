import { createHash, randomUUID } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { canonicalTimeZone, validBatchId } from "../../../../lib/control-validation";
import {
  encryptClientRegistration,
  googleServiceAccountCertificates,
} from "../../../../lib/onboarding-crypto";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const inventoryWorkflow = "refresh-live-inventory.yml";
const deliveryWorkflow = "client-delivery-control.yml";
const configureWorkflow = "configure-client-delivery-profile.yml";
const operatorProfilesVariable = "JOBSIFT_OPERATOR_PROFILES";
const defaultSheetsServiceAccount =
  "jobsift-sheets-publisher@jobsift-510120.iam.gserviceaccount.com";

const onboardingPlans = {
  "taiwo-software-remote-us-v2": {
    path: "config/sourcing_plans/taiwo_software_remote_us_v2.json",
    clientId: "taiwo_operator_sourcing_v1",
    label: "US remote software roles",
  },
} as const;

const workdayTargets = new Set(["1", "5", "10", "20", "25"]);
const workdayConcurrency = new Set(["4", "6", "8"]);
const yieldExtraBudgets = new Set(["0", "25", "50", "75", "100"]);
const deliveryOperations = new Set([
  "list",
  "status",
  "pause",
  "resume",
  "sheet-check",
  "sheet-disable",
  "sheet-enable",
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

function githubToken() {
  return process.env.JOBSIFT_GITHUB_TOKEN?.trim() ?? "";
}

function controlUnavailable() {
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

async function setWorkflowState(enabled: boolean) {
  const token = githubToken();
  if (!token) return controlUnavailable();
  const action = enabled ? "enable" : "disable";
  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${encodeURIComponent(inventoryWorkflow)}/${action}`,
    {
      method: "PUT",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "JobSift-operator-control",
      },
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    },
  );
  if (response.status !== 204) {
    return NextResponse.json(
      {
        error: {
          code: "CONTROL_DISPATCH_FAILED",
          message: `GitHub did not ${action} the inventory workflow.`,
        },
      },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
  return NextResponse.json(
    { data: { accepted: true, action } },
    { status: 202, headers: { "Cache-Control": "no-store" } },
  );
}

type DispatchResult =
  | { ok: true; controlRequestId: string }
  | { ok: false; response: NextResponse };

async function dispatchWorkflow(
  workflow: string,
  inputs: Record<string, string>,
): Promise<DispatchResult> {
  const token = githubToken();
  if (!token) return { ok: false, response: controlUnavailable() };
  const controlRequestId = randomUUID();
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
      body: JSON.stringify({
        ref: "main",
        inputs: { ...inputs, control_request_id: controlRequestId },
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    },
  );
  if (response.status !== 204) {
    return {
      ok: false,
      response: NextResponse.json(
        {
          error: {
            code: "CONTROL_DISPATCH_FAILED",
            message: "GitHub did not accept the JobSift command.",
          },
        },
        { status: 502, headers: { "Cache-Control": "no-store" } },
      ),
    };
  }
  return { ok: true, controlRequestId };
}

async function dispatch(workflow: string, inputs: Record<string, string>) {
  const result = await dispatchWorkflow(workflow, inputs);
  if (!result.ok) return result.response;
  return NextResponse.json(
    {
      data: {
        accepted: true,
        workflow,
        control_request_id: result.controlRequestId,
      },
    },
    { status: 202, headers: { "Cache-Control": "no-store" } },
  );
}

function profileControlId(clientId: string, destinationId: string) {
  return createHash("sha256")
    .update(`jobsift-delivery-profile-v1\0${clientId}\0${destinationId}`)
    .digest("hex")
    .slice(0, 16);
}

type OperatorProfile = {
  profile_id: string;
  client_name: string;
  destination_name: string;
};

function sanitizeOperatorProfiles(value: unknown): OperatorProfile[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.flatMap((entry): OperatorProfile[] => {
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    const profileId = clean(item.profile_id, 16).toLowerCase();
    const clientName = clean(item.client_name, 120);
    const destinationName = clean(item.destination_name, 120);
    if (
      !/^[0-9a-f]{16}$/.test(profileId) ||
      !clientName ||
      !destinationName ||
      seen.has(profileId)
    ) {
      return [];
    }
    seen.add(profileId);
    return [{
      profile_id: profileId,
      client_name: clientName,
      destination_name: destinationName,
    }];
  });
}

async function persistOperatorProfile(profile: OperatorProfile) {
  const token = githubToken();
  if (!token) return controlUnavailable();
  const base = `https://api.github.com/repos/${owner}/${repo}/actions/variables`;
  const headers = {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "JobSift-operator-control",
  };

  const current = await fetch(
    `${base}/${encodeURIComponent(operatorProfilesVariable)}`,
    {
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    },
  );

  let profiles: OperatorProfile[] = [];
  let exists = false;
  if (current.status === 200) {
    exists = true;
    const body = (await current.json()) as { value?: unknown };
    if (typeof body.value === "string") {
      try {
        profiles = sanitizeOperatorProfiles(JSON.parse(body.value));
      } catch {
        profiles = [];
      }
    }
  } else if (current.status !== 404) {
    return NextResponse.json(
      {
        error: {
          code: "CONTROL_CATALOGUE_UNAVAILABLE",
          message: "Could not read the private JobSift operator catalogue.",
        },
      },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }

  const next = [
    ...profiles.filter((value) => value.profile_id !== profile.profile_id),
    profile,
  ].sort((left, right) => left.client_name.localeCompare(right.client_name));

  const response = await fetch(
    exists ? `${base}/${encodeURIComponent(operatorProfilesVariable)}` : base,
    {
      method: exists ? "PATCH" : "POST",
      headers,
      body: JSON.stringify(
        exists
          ? { name: operatorProfilesVariable, value: JSON.stringify(next) }
          : { name: operatorProfilesVariable, value: JSON.stringify(next) },
      ),
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    },
  );
  if (![201, 204].includes(response.status)) {
    return NextResponse.json(
      {
        error: {
          code: "CONTROL_CATALOGUE_UNAVAILABLE",
          message:
            "Could not save the client label safely. Client setup was not started.",
        },
      },
      { status: 502, headers: { "Cache-Control": "no-store" } },
    );
  }
  return null;
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
  if (command === "inventory-schedule-pause") return setWorkflowState(false);
  if (command === "inventory-schedule-resume") return setWorkflowState(true);

  if (command === "inventory-refresh") {
    const targets = clean(body.workday_targets, 4);
    const concurrency = clean(body.workday_detail_concurrency, 2);
    const yieldExtraBudget = clean(body.yield_extra_budget, 3) || "100";
    if (!workdayTargets.has(targets) || !workdayConcurrency.has(concurrency)) {
      return invalid("Unsupported guarded Workday settings.");
    }
    if (!yieldExtraBudgets.has(yieldExtraBudget)) {
      return invalid("Yield-aware bonus budget must be 0, 25, 50, 75, or 100.");
    }
    return dispatch(inventoryWorkflow, {
      workday_targets: targets,
      workday_detail_concurrency: concurrency,
      yield_extra_budget: yieldExtraBudget,
    });
  }

  if (command === "client-sheet-onboard") {
    const planKey = clean(body.plan, 80) as keyof typeof onboardingPlans;
    const plan = onboardingPlans[planKey];
    if (!plan) return invalid("Choose a supported JobSift search setup.");

    const clientName = clean(body.client_name, 120);
    const destinationName = clean(body.destination_name, 120);
    const spreadsheet = clean(body.spreadsheet, 700);
    const tab = clean(body.tab, 120);
    const linkHeader = clean(body.link_header, 160);
    const titleHeader = clean(body.title_header, 160);
    const companyHeader = clean(body.company_header, 160);
    const descriptionHeader = clean(body.description_header, 160);
    const platformHeader = clean(body.platform_header, 160);
    const statusHeader = clean(body.status_header, 160);
    const quota = Number(clean(body.daily_quota, 8));
    const mode = clean(body.delivery_mode, 16) || "review";
    const timezone = canonicalTimeZone(clean(body.timezone, 80) || "Africa/Lagos");

    if (!clientName || !destinationName) {
      return invalid("Client name and Sheet name are required.");
    }
    let sheetUrl: URL;
    try {
      sheetUrl = new URL(spreadsheet);
    } catch {
      return invalid("Paste a valid Google Sheets URL.");
    }
    if (
      sheetUrl.protocol !== "https:" ||
      !["docs.google.com", "drive.google.com"].includes(sheetUrl.hostname)
    ) {
      return invalid("Paste a Google Sheets URL from docs.google.com or drive.google.com.");
    }
    if (!tab || /[\r\n!]/.test(tab)) {
      return invalid("Sheet tab name is invalid.");
    }
    if (!linkHeader || !titleHeader || !companyHeader) {
      return invalid("Link, job title, and company header names are required.");
    }
    if (!Number.isInteger(quota) || quota < 1 || quota > 5000) {
      return invalid("Daily quota must be an integer from 1 to 5000.");
    }
    if (!["review", "auto"].includes(mode)) {
      return invalid("Delivery mode must be review or auto.");
    }
    if (!timezone) return invalid("Timezone must be a valid IANA timezone.");

    const destinationId = `ui-${randomUUID().replaceAll("-", "").slice(0, 24)}`;
    const profileId = profileControlId(plan.clientId, destinationId);
    const columnMapping: Record<string, string> = {
      "Job Link": linkHeader,
      "Job Title": titleHeader,
      "Company Name": companyHeader,
    };
    if (descriptionHeader) columnMapping["Job Description"] = descriptionHeader;
    if (platformHeader) columnMapping["Job Platform"] = platformHeader;
    if (statusHeader) columnMapping.Status = statusHeader;

    try {
      const certificates = await googleServiceAccountCertificates(
        process.env.JOBSIFT_SHEETS_SERVICE_ACCOUNT_EMAIL?.trim() ||
          defaultSheetsServiceAccount,
      );
      const encryptedRegistration = encryptClientRegistration(
        {
          client_id: plan.clientId,
          destination_id: destinationId,
          display_name: destinationName,
          spreadsheet,
          tab,
          column_mapping: columnMapping,
        },
        certificates,
      );

      const catalogueError = await persistOperatorProfile({
        profile_id: profileId,
        client_name: clientName,
        destination_name: destinationName,
      });
      if (catalogueError) return catalogueError;

      const result = await dispatchWorkflow(configureWorkflow, {
        plan: plan.path,
        daily_quota: String(quota),
        status: "active",
        delivery_mode: mode,
        timezone,
        encrypted_registration: encryptedRegistration,
      });
      if (!result.ok) return result.response;
      return NextResponse.json(
        {
          data: {
            accepted: true,
            workflow: configureWorkflow,
            control_request_id: result.controlRequestId,
            profile_id: profileId,
          },
        },
        { status: 202, headers: { "Cache-Control": "no-store" } },
      );
    } catch {
      return NextResponse.json(
        {
          error: {
            code: "CLIENT_SETUP_UNAVAILABLE",
            message:
              "Secure client setup is temporarily unavailable. No Sheet details were sent to GitHub.",
          },
        },
        { status: 503, headers: { "Cache-Control": "no-store" } },
      );
    }
  }

  if (command === "client-control") {
    const operation = clean(body.operation, 24);
    if (!deliveryOperations.has(operation)) return invalid("Unsupported client control operation.");

    const profileId = clean(body.profile_id, 64);
    const batchId = clean(body.batch_id, 160);
    const quota = clean(body.daily_quota, 8);
    const mode = clean(body.delivery_mode, 16);
    const timezone = clean(body.timezone, 80);

    if (!["list"].includes(operation) && !/^[0-9a-f]{16}$/.test(profileId)) {
      return invalid("A valid opaque delivery profile ID is required.");
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
    const canonicalTimezone =
      operation === "set-timezone" ? canonicalTimeZone(timezone) : "Africa/Lagos";
    if (operation === "set-timezone" && !canonicalTimezone) {
      return invalid("Timezone must be a valid IANA timezone.");
    }
    if (["release-batch", "discard-batch"].includes(operation) && !validBatchId(batchId)) {
      return invalid("Batch ID must be a valid JobSift batch UUID.");
    }

    return dispatch(deliveryWorkflow, {
      operation,
      profile_id: profileId,
      daily_quota: quota || "100",
      delivery_mode: mode || "review",
      timezone: canonicalTimezone ?? "Africa/Lagos",
      batch_id: batchId,
    });
  }

  return invalid("Unsupported JobSift control command.");
}
