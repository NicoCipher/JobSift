import { randomUUID } from "node:crypto";
import {
  decryptProvisioningPayload,
  encryptProvisioningPayload,
  validProvisioningKey,
} from "../../../lib/provisioning-crypto";
import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const owner = "NicoCipher";
const repo = "JobSift";
const workflow = "operator-client-provision.yml";
const titlePrefix = "Provision operator client · ";
const allowedOperations = new Set(["inspect_sheet", "create_client"]);

type GithubRun = {
  id: number;
  status: string;
  conclusion: string | null;
  display_title?: string | null;
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

function response(data: unknown, status = 200) {
  return NextResponse.json(data, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function invalid(message: string) {
  return response({ error: { code: "VALIDATION_ERROR", message } }, 400);
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
    freshnessHours > 24 ||
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

function publicResult(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
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
  if (!sameOrigin(request)) {
    return response(
      { error: { code: "FORBIDDEN", message: "Cross-origin onboarding requests are not allowed." } },
      403,
    );
  }
  if (!token()) {
    return response(
      { error: { code: "CONTROL_NOT_CONFIGURED", message: "Production onboarding is not configured." } },
      503,
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
    );
  }
  if (!request.headers.get("content-type")?.startsWith("application/json")) {
    return invalid("Expected a JSON onboarding request.");
  }

  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return invalid("Invalid onboarding request.");
  }
  const operation = clean(body.operation, 32);
  if (!allowedOperations.has(operation)) return invalid("Unsupported onboarding operation.");
  const payload = validatePayload(operation, body.payload);
  if (!payload) return invalid("Some onboarding fields are missing or invalid.");

  const serialized = JSON.stringify(payload);
  if (Buffer.byteLength(serialized, "utf8") > 20_000) {
    return invalid("Onboarding request is too large.");
  }
  const ciphertext = encryptProvisioningPayload(provisioningKey(), serialized);
  if (!ciphertext) {
    return response(
      {
        error: {
          code: "PROVISIONING_NOT_CONFIGURED",
          message: "Secure client onboarding is not configured on this deployment.",
        },
      },
      503,
    );
  }
  const requestId = randomUUID();
  const controlRequestId = randomUUID();
  const dispatched = await github("/actions/workflows/" + workflow + "/dispatches", {
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
  if (dispatched.status !== 204) {
    return response(
      { error: { code: "ONBOARDING_DISPATCH_FAILED", message: "JobSift could not start this onboarding action." } },
      502,
    );
  }
  return response({ data: { state: "queued", control_request_id: controlRequestId } }, 202);
}

export async function GET(request: NextRequest) {
  if (!token()) {
    return response(
      { error: { code: "CONTROL_NOT_CONFIGURED", message: "Production onboarding is not configured." } },
      503,
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
    );
  }
  const controlRequestId =
    request.nextUrl.searchParams.get("control_request_id")?.trim().toLowerCase() ?? "";
  if (!validRequestId(controlRequestId)) return invalid("A valid onboarding request is required.");

  try {
    const runsResponse = await github(
      "/actions/workflows/" + workflow + "/runs?event=workflow_dispatch&per_page=100",
    );
    if (!runsResponse.ok) throw new Error("runs");
    const runsBody = (await runsResponse.json()) as { workflow_runs?: GithubRun[] };
    const run = (runsBody.workflow_runs ?? []).find(
      (item) => item.display_title === titlePrefix + controlRequestId,
    );
    if (!run) return response({ data: { state: "queued" } });
    if (run.status !== "completed") {
      return response({ data: { state: run.status || "in_progress" } });
    }

    const jobsResponse = await github("/actions/runs/" + run.id + "/jobs?per_page=100");
    if (!jobsResponse.ok) throw new Error("jobs");
    const jobsBody = (await jobsResponse.json()) as {
      jobs?: Array<{ id: number; name: string }>;
    };
    const job = (jobsBody.jobs ?? []).find((item) => item.name === "provision");
    if (!job) throw new Error("provision job");
    const logsResponse = await github("/actions/jobs/" + job.id + "/logs");
    if (!logsResponse.ok) throw new Error("logs");
    const logs = await logsResponse.text();

    if (run.conclusion === "success") {
      const ciphertext = parseTextMarker(
        logs,
        "JOBSIFT_PROVISION_RESULT_CIPHERTEXT=",
      );
      const plaintext = decryptProvisioningPayload(
        provisioningKey(),
        ciphertext,
      );
      if (!plaintext) throw new Error("result");
      const marker = JSON.parse(plaintext) as Record<string, unknown>;
      const result = publicResult(marker.result);
      if (!result) throw new Error("result");
      return response({ data: { state: "ready", result } });
    }

    const error = parseMarker(logs, "JOBSIFT_PROVISION_ERROR=");
    return response({
      data: {
        state: "failed",
        message:
          clean(error?.message, 500) ||
          "JobSift could not complete this onboarding action. Check the Sheet and try again.",
      },
    });
  } catch {
    return response(
      { error: { code: "ONBOARDING_STATUS_FAILED", message: "JobSift could not read onboarding status." } },
      502,
    );
  }
}
