import { randomUUID } from "node:crypto";
import { ownerApiGuard } from "../../../lib/owner-auth";
import {
  decryptProvisioningPayload,
  encryptProvisioningPayload,
  validProvisioningKey,
} from "../../../lib/provisioning-crypto";
import { NextRequest, NextResponse } from "next/server";
import {
  encryptGithubActionSecret,
  InvalidGithubActionsPublicKeyError,
} from "../../../lib/github-secret";
import {
  describeProvisioningSyncFailure,
  ProvisioningSyncError,
} from "../../../lib/provisioning-sync-error";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const workflow = "operator-client-provision.yml";
const healthWorkflow = "operator-health-check.yml";
const titlePrefix = "Provision operator client · ";
const healthTitlePrefix = "JobSift backend health · ";
const allowedOperations = new Set(["health_check", "inspect_sheet", "create_client"]);

type GithubRun = {
  id: number;
  status: string;
  conclusion: string | null;
  display_title?: string | null;
  html_url?: string | null;
  run_number?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
};

function token() {
  return process.env.JOBSIFT_GITHUB_TOKEN?.trim() ?? "";
}

function provisioningKey() {
  return process.env.JOBSIFT_PROVISIONING_KEY?.trim() ?? "";
}

function sameOrigin(request: NextRequest) {
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  return (!origin || origin === request.nextUrl.origin) && (!site || site === "same-origin");
}

function response(data: unknown, status = 200, traceId?: string) {
  const body = traceId && data && typeof data === "object" && !Array.isArray(data)
    ? { ...(data as Record<string, unknown>), meta: { trace_id: traceId } }
    : data;
  return NextResponse.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      ...(traceId ? { "X-JobSift-Trace-Id": traceId } : {}),
    },
  });
}

function invalid(message: string, traceId?: string) {
  return response({ error: { code: "VALIDATION_ERROR", message } }, 400, traceId);
}

function clean(value: unknown, max: number) {
  return typeof value === "string" ? value.trim().slice(0, max) : "";
}

function validTimeZone(value: string) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value }).format(new Date(0));
    return true;
  } catch {
    return false;
  }
}

function validRequestId(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    value,
  );
}

function boundedStrings(value: unknown, maxItems: number, maxLength: number) {
  if (!Array.isArray(value) || value.length > maxItems) return null;
  const strings = value.map((item) => clean(item, maxLength)).filter(Boolean);
  return strings.length === value.length ? strings : null;
}

function validatePayload(operation: string, raw: unknown): Record<string, unknown> | null {
  if (operation === "health_check") {
    return raw && typeof raw === "object" && !Array.isArray(raw) && Object.keys(raw).length === 0
      ? {}
      : null;
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const source = raw as Record<string, unknown>;

  if (operation === "inspect_sheet") {
    const sheetUrl = clean(source.sheet_url, 2048);
    const tab = clean(source.tab, 120);
    if (!sheetUrl || !tab) return null;
    return { sheet_url: sheetUrl, tab };
  }

  if (operation !== "create_client") return null;
  const clientName = clean(source.client_name, 120);
  const criteria =
    source.criteria && typeof source.criteria === "object" && !Array.isArray(source.criteria)
      ? (source.criteria as Record<string, unknown>)
      : null;
  const sheet =
    source.sheet && typeof source.sheet === "object" && !Array.isArray(source.sheet)
      ? (source.sheet as Record<string, unknown>)
      : null;
  if (!clientName || !criteria || !sheet) return null;

  const roleTitles = boundedStrings(criteria.role_titles, 20, 120);
  const workModes = boundedStrings(criteria.work_modes, 3, 20);
  const exclusions = boundedStrings(criteria.exclusions ?? [], 50, 120);
  const preferredTerms = boundedStrings(criteria.preferred_terms ?? [], 50, 120);
  const country = clean(criteria.country, 2).toUpperCase();
  const freshnessHours = Number(criteria.freshness_hours);
  const dailyLimit = Number(source.daily_limit);
  const deliveryMode = clean(source.delivery_mode, 12).toLowerCase();
  const timezone = clean(source.timezone, 80);
  const sheetUrl = clean(sheet.url, 2048);
  const tab = clean(sheet.tab, 120);
  const mapping =
    sheet.column_mapping && typeof sheet.column_mapping === "object" && !Array.isArray(sheet.column_mapping)
      ? (sheet.column_mapping as Record<string, unknown>)
      : {};
  const columnMapping: Record<string, string> = {};
  for (const [field, header] of Object.entries(mapping)) {
    const cleanField = clean(field, 80);
    const cleanHeader = clean(header, 200);
    if (!cleanField || !cleanHeader || Object.keys(columnMapping).length >= 20) return null;
    columnMapping[cleanField] = cleanHeader;
  }

  if (
    !roleTitles?.length ||
    !workModes?.length ||
    !/^[A-Z]{2}$/.test(country) ||
    workModes.some((mode) => !["remote", "hybrid", "onsite"].includes(mode.toLowerCase())) ||
    !Number.isInteger(freshnessHours) ||
    freshnessHours < 1 ||
    freshnessHours > 48 ||
    !Number.isInteger(dailyLimit) ||
    dailyLimit < 1 ||
    dailyLimit > 2000 ||
    !["review", "auto"].includes(deliveryMode) ||
    !timezone ||
    !validTimeZone(timezone) ||
    !sheetUrl ||
    !tab ||
    exclusions === null ||
    preferredTerms === null
  ) {
    return null;
  }

  return {
    client_name: clientName,
    criteria: {
      role_titles: roleTitles,
      country,
      work_modes: workModes.map((mode) => mode.toLowerCase()),
      exclusions,
      preferred_terms: preferredTerms,
      freshness_hours: freshnessHours,
    },
    daily_limit: dailyLimit,
    delivery_mode: deliveryMode,
    timezone,
    sheet: {
      url: sheetUrl,
      tab,
      column_mapping: columnMapping,
    },
  };
}

async function github(path: string, init?: RequestInit) {
  const secret = token();
  if (!secret) throw new Error("NOT_CONFIGURED");
  return fetch("https://api.github.com/repos/" + owner + "/" + repo + path, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: "Bearer " + secret,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "JobSift-operator-onboarding",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
    signal: AbortSignal.timeout(10000),
  });
}


async function syncProvisioningKeyToGitHub() {
  const rawKey = provisioningKey();
  if (!validProvisioningKey(rawKey)) {
    throw new ProvisioningSyncError("PROVISIONING_KEY_INVALID");
  }

  let publicKeyResponse: Response;
  try {
    publicKeyResponse = await github("/actions/secrets/public-key");
  } catch {
    throw new ProvisioningSyncError("GITHUB_PUBLIC_KEY_REQUEST_FAILED");
  }
  if (!publicKeyResponse.ok) {
    throw new ProvisioningSyncError(
      "GITHUB_PUBLIC_KEY_REQUEST_FAILED",
      publicKeyResponse.status,
    );
  }

  let publicKey: { key?: string; key_id?: string };
  try {
    publicKey = (await publicKeyResponse.json()) as {
      key?: string;
      key_id?: string;
    };
  } catch {
    throw new ProvisioningSyncError("GITHUB_PUBLIC_KEY_RESPONSE_INVALID");
  }
  if (
    !publicKey ||
    typeof publicKey !== "object" ||
    typeof publicKey.key !== "string" ||
    !publicKey.key ||
    typeof publicKey.key_id !== "string" ||
    !publicKey.key_id
  ) {
    throw new ProvisioningSyncError("GITHUB_PUBLIC_KEY_RESPONSE_INVALID");
  }

  let encryptedValue: string;
  try {
    encryptedValue = await encryptGithubActionSecret(rawKey, publicKey.key);
  } catch (error) {
    if (error instanceof InvalidGithubActionsPublicKeyError) {
      throw new ProvisioningSyncError("GITHUB_PUBLIC_KEY_RESPONSE_INVALID");
    }
    throw new ProvisioningSyncError("GITHUB_SECRET_ENCRYPTION_FAILED");
  }

  let updateResponse: Response;
  try {
    updateResponse = await github("/actions/secrets/JOBSIFT_PROVISIONING_KEY", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        encrypted_value: encryptedValue,
        key_id: publicKey.key_id,
      }),
    });
  } catch {
    throw new ProvisioningSyncError("GITHUB_SECRET_WRITE_FAILED");
  }
  if (!updateResponse.ok) {
    throw new ProvisioningSyncError(
      "GITHUB_SECRET_WRITE_FAILED",
      updateResponse.status,
    );
  }
}

function publicResult(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  if (source.database_status === "connected" || source.google_auth_status === "authenticated") {
    return {
      database_status: clean(source.database_status, 32),
      google_auth_status: clean(source.google_auth_status, 32),
      checked_at: clean(source.checked_at, 64),
    };
  }
  if (Array.isArray(source.tabs)) {
    const tabs = source.tabs.filter((item): item is string => typeof item === "string").slice(0, 200);
    const headers = Array.isArray(source.headers)
      ? source.headers.filter((item): item is string => typeof item === "string").slice(0, 200)
      : [];
    const proposed =
      source.proposed_mapping && typeof source.proposed_mapping === "object"
        ? source.proposed_mapping
        : {};
    const missing = Array.isArray(source.missing_required_fields)
      ? source.missing_required_fields.filter((item): item is string => typeof item === "string")
      : [];
    return {
      tabs,
      selected_tab: clean(source.selected_tab, 120),
      headers,
      proposed_mapping: proposed,
      missing_required_fields: missing,
    };
  }
  return {
    client_name: clean(source.client_name, 120),
    destination_name: clean(source.destination_name, 120),
    delivery_mode: clean(source.delivery_mode, 20),
    daily_limit: Number.isInteger(source.daily_limit) ? source.daily_limit : null,
    sheet_status: clean(source.sheet_status, 32),
    brief_revision: Number.isInteger(source.brief_revision) ? source.brief_revision : null,
  };
}

function parseMarker(logs: string, marker: string) {
  const ansi = /\u001b\[[0-9;]*m/g;
  const line = logs
    .replace(ansi, "")
    .split("\n")
    .reverse()
    .find((candidate) => candidate.includes(marker));
  if (!line) return null;
  try {
    return JSON.parse(line.slice(line.indexOf(marker) + marker.length).trim()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function parseTextMarker(logs: string, marker: string) {
  const ansi = /\u001b\[[0-9;]*m/g;
  const line = logs
    .replace(ansi, "")
    .split("\n")
    .reverse()
    .find((candidate) => candidate.includes(marker));
  if (!line) return "";
  return line.slice(line.indexOf(marker) + marker.length).trim();
}

export async function POST(request: NextRequest) {
  const controlRequestId = randomUUID();
  const requestId = randomUUID();
  let operation = "";

  const trace = (stage: string, details: Record<string, unknown> = {}) => {
    console.info("[JobSift onboarding] operation trace", {
      trace_id: controlRequestId,
      request_id: requestId,
      operation: operation || null,
      stage,
      ...details,
    });
  };
  const fail = (
    code: string,
    message: string,
    status: number,
    stage: string,
    httpStatus?: number,
  ) => {
    trace("failed", { code, stage, http_status: httpStatus ?? null });
    return response(
      {
        error: {
          code,
          message,
          stage,
          ...(httpStatus ? { http_status: httpStatus } : {}),
        },
        data: {
          state: "failed",
          operation: operation || null,
          request_id: requestId,
          control_request_id: controlRequestId,
        },
      },
      status,
      controlRequestId,
    );
  };

  const ownerDenied = ownerApiGuard(request);
  if (ownerDenied) {
    ownerDenied.headers.set("X-JobSift-Trace-Id", controlRequestId);
    return ownerDenied;
  }
  if (!sameOrigin(request)) {
    return fail(
      "FORBIDDEN",
      "Cross-origin onboarding requests are not allowed.",
      403,
      "request_validation",
    );
  }
  if (!token()) {
    return fail(
      "CONTROL_NOT_CONFIGURED",
      "Production onboarding is not configured.",
      503,
      "configuration",
    );
  }
  if (!validProvisioningKey(provisioningKey())) {
    return fail(
      "PROVISIONING_NOT_CONFIGURED",
      "Secure client onboarding is not configured on this deployment.",
      503,
      "configuration",
    );
  }
  if (!request.headers.get("content-type")?.startsWith("application/json")) {
    return fail(
      "VALIDATION_ERROR",
      "Expected a JSON onboarding request.",
      400,
      "request_validation",
    );
  }

  let body: Record<string, unknown>;
  try {
    const parsed = await request.json();
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return fail(
        "VALIDATION_ERROR",
        "Invalid onboarding request.",
        400,
        "request_validation",
      );
    }
    body = parsed as Record<string, unknown>;
  } catch {
    return fail(
      "VALIDATION_ERROR",
      "Invalid onboarding request.",
      400,
      "request_validation",
    );
  }

  operation = clean(body.operation, 32);
  if (!allowedOperations.has(operation)) {
    return fail(
      "VALIDATION_ERROR",
      "Unsupported onboarding operation.",
      400,
      "request_validation",
    );
  }
  const payload = validatePayload(operation, body.payload);
  if (!payload) {
    return fail(
      "VALIDATION_ERROR",
      "Some onboarding fields are missing or invalid.",
      400,
      "request_validation",
    );
  }

  const serialized = JSON.stringify(payload);
  if (Buffer.byteLength(serialized, "utf8") > 20_000) {
    return fail(
      "VALIDATION_ERROR",
      "Onboarding request is too large.",
      400,
      "request_validation",
    );
  }
  const ciphertext = encryptProvisioningPayload(provisioningKey(), serialized);
  if (!ciphertext) {
    return fail(
      "PROVISIONING_NOT_CONFIGURED",
      "Secure client onboarding is not configured on this deployment.",
      503,
      "payload_encryption",
    );
  }

  trace("secret_sync_started");
  try {
    await syncProvisioningKeyToGitHub();
  } catch (error) {
    const failure = describeProvisioningSyncFailure(error);
    trace("secret_sync_failed", {
      code: failure.code,
      stage: failure.stage,
      http_status: failure.httpStatus ?? null,
    });
    return response(
      {
        error: {
          code: failure.code,
          message: failure.message,
          stage: failure.stage,
          ...(failure.httpStatus ? { http_status: failure.httpStatus } : {}),
        },
        data: {
          state: "failed",
          operation,
          request_id: requestId,
          control_request_id: controlRequestId,
        },
      },
      503,
      controlRequestId,
    );
  }

  const selectedWorkflow = operation === "health_check" ? healthWorkflow : workflow;
  let dispatched: Response;
  trace("workflow_dispatch_started", { workflow: selectedWorkflow });
  try {
    dispatched = await github("/actions/workflows/" + selectedWorkflow + "/dispatches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ref: "main",
        inputs: {
          request_id: requestId,
          operation,
          payload_ciphertext: ciphertext,
          control_request_id: controlRequestId,
        },
      }),
    });
  } catch {
    return fail(
      "ONBOARDING_DISPATCH_REQUEST_FAILED",
      "JobSift could not reach GitHub to start this action.",
      502,
      "workflow_dispatch",
    );
  }

  if (dispatched.status !== 204) {
    return fail(
      "ONBOARDING_DISPATCH_FAILED",
      "GitHub rejected the request to start this action.",
      502,
      "workflow_dispatch",
      dispatched.status,
    );
  }

  trace("workflow_dispatch_accepted");
  return response(
    {
      data: {
        state: "queued",
        operation,
        request_id: requestId,
        control_request_id: controlRequestId,
      },
    },
    202,
    controlRequestId,
  );
}

export async function GET(request: NextRequest) {
  const controlRequestId =
    request.nextUrl.searchParams.get("control_request_id")?.trim().toLowerCase() ?? "";
  const traceId = validRequestId(controlRequestId) ? controlRequestId : randomUUID();
  const ownerDenied = ownerApiGuard(request);
  if (ownerDenied) {
    ownerDenied.headers.set("X-JobSift-Trace-Id", traceId);
    return ownerDenied;
  }
  if (!validRequestId(controlRequestId)) {
    return invalid("A valid onboarding request is required.", traceId);
  }
  if (!token()) {
    return response(
      { error: { code: "CONTROL_NOT_CONFIGURED", message: "Production onboarding is not configured." } },
      503,
      traceId,
    );
  }
  if (!validProvisioningKey(provisioningKey())) {
    return response(
      {
        error: {
          code: "PROVISIONING_NOT_CONFIGURED",
          message: "Secure client onboarding is not configured on this deployment.",
        },
      },
      503,
      traceId,
    );
  }

  let stage = "workflow_lookup";
  let runId: number | null = null;
  try {
    const runsResponse = await github(
      "/actions/runs?event=workflow_dispatch&per_page=100",
    );
    if (!runsResponse.ok) {
      throw new Error("GitHub workflow lookup failed with HTTP " + runsResponse.status);
    }
    const runsBody = (await runsResponse.json()) as { workflow_runs?: GithubRun[] };
    const run = (runsBody.workflow_runs ?? []).find(
      (item) =>
        item.display_title === titlePrefix + controlRequestId ||
        item.display_title === healthTitlePrefix + controlRequestId,
    );
    if (!run) {
      return response(
        {
          data: {
            state: "queued",
            control_request_id: controlRequestId,
            workflow_run_id: null,
          },
        },
        200,
        controlRequestId,
      );
    }
    runId = run.id;

    const runMeta = {
      workflow_run_id: run.id,
      workflow_run_url: clean(run.html_url, 500),
      run_number: Number.isInteger(run.run_number) ? run.run_number : null,
      created_at: clean(run.created_at, 64),
      updated_at: clean(run.updated_at, 64),
      completed_at: clean(run.completed_at, 64),
    };

    if (run.status !== "completed") {
      return response(
        {
          data: {
            state: run.status || "in_progress",
            control_request_id: controlRequestId,
            ...runMeta,
          },
        },
        200,
        controlRequestId,
      );
    }

    stage = "job_lookup";
    const jobsResponse = await github("/actions/runs/" + run.id + "/jobs?per_page=100");
    if (!jobsResponse.ok) {
      throw new Error("GitHub workflow job lookup failed with HTTP " + jobsResponse.status);
    }
    const jobsBody = (await jobsResponse.json()) as {
      jobs?: Array<{ id: number; name: string }>;
    };
    const job = (jobsBody.jobs ?? []).find((item) => item.name === "provision");
    if (!job) throw new Error("Provisioning job was not found.");

    stage = "workflow_logs";
    const logsResponse = await github("/actions/jobs/" + job.id + "/logs");
    if (!logsResponse.ok) {
      throw new Error("GitHub workflow logs could not be read (HTTP " + logsResponse.status + ").");
    }
    const logs = await logsResponse.text();

    if (run.conclusion === "success") {
      stage = "result_decryption";
      const ciphertext = parseTextMarker(logs, "JOBSIFT_PROVISION_RESULT_CIPHERTEXT=");
      const plaintext = decryptProvisioningPayload(provisioningKey(), ciphertext);
      if (!plaintext) throw new Error("The encrypted operation result could not be verified.");
      const marker = JSON.parse(plaintext) as Record<string, unknown>;
      const resultRequestId = clean(marker.request_id, 36);
      const resultControlId = clean(marker.control_request_id, 36);
      const operation = clean(marker.operation, 32);
      if (
        !validRequestId(resultRequestId) ||
        resultControlId !== controlRequestId ||
        !allowedOperations.has(operation)
      ) {
        throw new Error("The operation result did not match its request reference.");
      }
      const result = publicResult(marker.result);
      if (!result) throw new Error("The operation result was invalid.");
      console.info("[JobSift onboarding] operation completed", {
        trace_id: controlRequestId,
        request_id: resultRequestId,
        operation,
        workflow_run_id: run.id,
        conclusion: run.conclusion,
      });
      return response(
        {
          data: {
            state: "ready",
            operation,
            request_id: resultRequestId,
            control_request_id: controlRequestId,
            result,
            ...runMeta,
          },
        },
        200,
        controlRequestId,
      );
    }

    stage = "worker_failure";
    const error = parseMarker(logs, "JOBSIFT_PROVISION_ERROR=");
    const workflowTrace = parseMarker(logs, "JOBSIFT_OPERATION_TRACE=");
    const errorControlId = clean(error?.control_request_id, 36);
    const traceControlId = clean(workflowTrace?.control_request_id, 36);
    if (
      (errorControlId && errorControlId !== controlRequestId) ||
      (traceControlId && traceControlId !== controlRequestId)
    ) {
      throw new Error("The worker failure did not match its request reference.");
    }
    const errorRequestId = clean(error?.request_id, 36);
    const traceRequestId = clean(workflowTrace?.request_id, 36);
    const operation = clean(error?.operation, 32) || clean(workflowTrace?.operation, 32);
    const failedStage = clean(workflowTrace?.failed_stage, 64);
    const failureStage = failedStage ? "workflow_setup:" + failedStage : "worker_execution";
    const code =
      clean(error?.code, 80) ||
      (failedStage ? "WORKFLOW_SETUP_FAILED" : "PROVISIONING_FAILED");
    const message =
      clean(error?.message, 500) ||
      (failedStage
        ? "GitHub Actions failed during " + failedStage.replace(/_/g, " ") + " before the worker returned a result."
        : "JobSift could not complete this action. Check the connection and try again.");
    const failedRequestId = validRequestId(errorRequestId)
      ? errorRequestId
      : traceRequestId;
    console.error("[JobSift onboarding] operation failed", {
      trace_id: controlRequestId,
      request_id: validRequestId(failedRequestId) ? failedRequestId : null,
      operation: allowedOperations.has(operation) ? operation : null,
      workflow_run_id: run.id,
      code,
      stage: failureStage,
    });
    return response(
      {
        data: {
          state: "failed",
          code,
          stage: failureStage,
          message,
          operation: allowedOperations.has(operation) ? operation : null,
          request_id: validRequestId(failedRequestId) ? failedRequestId : null,
          control_request_id: controlRequestId,
          conclusion: run.conclusion,
          ...runMeta,
        },
      },
      200,
      controlRequestId,
    );
  } catch {
    console.error("[JobSift onboarding] status lookup failed", {
      trace_id: controlRequestId,
      workflow_run_id: runId,
      stage,
    });
    return response(
      {
        error: {
          code: "ONBOARDING_STATUS_FAILED",
          message: "JobSift could not verify this action's status. Use the request reference when checking logs.",
          stage,
        },
        data: {
          state: "unknown",
          control_request_id: controlRequestId,
          workflow_run_id: runId,
        },
      },
      502,
      controlRequestId,
    );
  }
}
