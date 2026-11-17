import { test, expect } from "@playwright/test";
import {
  applicationLabel,
  decisionLabel,
  factText,
  metricText,
  safeExternalUrl,
} from "../lib/display";
import { FixtureJobSiftApi } from "../lib/api/fixture-api";
import { LiveJobSiftApi } from "../lib/api/live-api";
import { GET as proxyRead } from "../app/api/operator/[...path]/route";
import { GET as onboardingHealth } from "../app/api/onboarding/health/route";
import { POST as onboardingLaunch } from "../app/api/onboarding/route";
import { NextRequest } from "next/server";
import sodium from "libsodium-wrappers";
import type { ApiError } from "../lib/contracts/service";
import { canonicalTimeZone, validBatchId } from "../lib/control-validation";
const query = {
  client_id: "example-client",
  destination_id: "example-destination",
};

test("onboarding health preflight checks GitHub access without exposing secrets", async () => {
  const originalFetch = globalThis.fetch;
  const previousToken = process.env.JOBSIFT_GITHUB_TOKEN;
  const previousKey = process.env.JOBSIFT_PROVISIONING_KEY;
  process.env.JOBSIFT_GITHUB_TOKEN = "test-github-token";
  process.env.JOBSIFT_PROVISIONING_KEY = Buffer.alloc(32, 7).toString("base64url");

  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/actions/secrets/public-key")) {
      return Response.json({
        key: Buffer.alloc(32, 1).toString("base64"),
        key_id: "test-key",
      });
    }
    if (
      url.endsWith("/actions/workflows/operator-client-provision.yml") ||
      url.endsWith("/actions/workflows/operator-health-check.yml")
    ) {
      return Response.json({ state: "active" });
    }
    return Response.json({ name: url.split("/").pop(), updated_at: "2026-11-17T00:00:00Z" });
  };

  try {
    const request = new NextRequest("http://localhost:3000/api/onboarding/health");
    const response = await onboardingHealth(request);
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.data.ready).toBe(true);
    expect(body.data.checks).toMatchObject({
      github_token: "configured",
      provisioning_key: "configured",
      actions_public_key: { status: "available", http_status: 200 },
      workflow: { status: "active", http_status: 200 },
      secret_write_permission: "not_verified",
      secret_value_match: "not_verifiable",
      database_connectivity: "not_checked",
      google_sheets_authentication: "not_checked",
    });
    expect(body.meta.trace_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(JSON.stringify(body)).not.toContain("test-github-token");
    expect(JSON.stringify(body)).not.toContain(process.env.JOBSIFT_PROVISIONING_KEY);
  } finally {
    globalThis.fetch = originalFetch;
    if (previousToken === undefined) delete process.env.JOBSIFT_GITHUB_TOKEN;
    else process.env.JOBSIFT_GITHUB_TOKEN = previousToken;
    if (previousKey === undefined) delete process.env.JOBSIFT_PROVISIONING_KEY;
    else process.env.JOBSIFT_PROVISIONING_KEY = previousKey;
  }
});

test("control validation canonicalizes timezone aliases and rejects malformed IDs", () => {
  expect(canonicalTimeZone("america/new_york")).toBe("America/New_York");
  expect(canonicalTimeZone(" Africa/Lagos ")).toBe("Africa/Lagos");
  expect(canonicalTimeZone("\"; echo pwned; #")).toBeNull();
  expect(validBatchId("01234567-89ab-5cde-8fab-0123456789ab")).toBe(true);
  expect(validBatchId("\"; echo pwned; #")).toBe(false);
});

test("exact decision display labels", () => {
  expect(decisionLabel).toEqual({
    strong_match: "Strong match",
    possible_match: "Possible",
    needs_review: "Needs review",
    reject: "Rejected",
  });
});
test("unknown, absent, false and measured zero remain distinct", () => {
  expect(factText({ value: null, availability: "unknown" })).toBe("Unknown");
  expect(factText({ value: null, availability: "not_reported" })).toBe(
    "Not reported",
  );
  expect(factText({ value: false, availability: "reported" })).toBe("false");
  expect(
    metricText({
      value: 0,
      availability: "reported",
      unit: "exports",
      definition: "csv_rows_appended",
    }),
  ).toBe("0");
});
test("application labels and unsafe URL defense do not synthesize URLs", () => {
  expect(
    applicationLabel({
      application_url: "https://jobs.example.com/a",
      canonical_url: null,
      application_url_kind: "direct_apply",
    }),
  ).toBe("Open application");
  expect(
    applicationLabel({
      application_url: "https://jobs.example.com/a",
      canonical_url: null,
      application_url_kind: "vacancy_page",
    }),
  ).toBe("Open listing");
  expect(
    applicationLabel({
      application_url: null,
      canonical_url: null,
      application_url_kind: "unavailable",
    }),
  ).toBe("Application URL unavailable");
  expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
  expect(safeExternalUrl("https://user:pass@example.com")).toBeNull();
});
test("fixture cursors retain values and reject mismatched scope", async () => {
  const api = new FixtureJobSiftApi();
  const first = await api.listJobs(query);
  expect(first.data).toHaveLength(40);
  const next = await api.listJobs({
    ...query,
    cursor: first.page.next_cursor!,
  });
  expect(next.data).toHaveLength(4);
  expect(next.page.snapshot_id).toBe(first.page.snapshot_id);
  const back = await api.listJobs({
    ...query,
    cursor: next.page.previous_cursor!,
  });
  expect(back.data).toEqual(first.data);
  await expect(
    api.listJobs({ ...query, q: "different", cursor: first.page.next_cursor! }),
  ).rejects.toMatchObject({ body: { error: { code: "INVALID_CURSOR" } } });
  await expect(
    api.getJob(
      "other-client",
      first.data[0].delivery_group_id,
      first.page.snapshot_id,
    ),
  ).rejects.toMatchObject({ status: 404 });
});
test("filtered query searches fixture transport; details preserve evidence", async () => {
  const api = new FixtureJobSiftApi();
  const result = await api.listJobs({
    ...query,
    q: " Alder ",
    source: ["greenhouse"],
  });
  expect(result.data.length).toBeGreaterThan(0);
  expect(
    result.data.every((row) =>
      row.representative_posting?.company.includes("Alder"),
    ),
  ).toBe(true);
  const detail = await api.getJob(
    query.client_id,
    result.data[0].delivery_group_id,
    result.page.snapshot_id,
  );
  expect(detail.data.members).toHaveLength(2);
  expect(detail.data.capabilities.can_edit_outcome).toEqual({
    allowed: false,
    reason: "not_implemented",
  });
});
test("correct evidence scope and dependency codes stay distinct", () => {
  const errors: ApiError["error"]["code"][] = [
    "EVIDENCE_SCOPE_UNAVAILABLE",
    "EVIDENCE_UNAVAILABLE",
  ];
  expect(new Set(errors).size).toBe(2);
});

test("history responses include the JOB-4 list envelope", async () => {
  const api = new FixtureJobSiftApi();
  const history = await api.getHistory(query.client_id);
  expect(history).toHaveProperty("page");
  expect(history).toMatchObject({
    page: { next_cursor: null, previous_cursor: null,
      known_total: { availability: "reported", value: 2, unit: "history_entries" } },
  });
});

test("job detail links retain the list evidence snapshot", async () => {
  const api = new FixtureJobSiftApi();
  const list = await api.listJobs(query);
  for (const job of list.data) {
    const url = new URL(job.detail_url, "https://example.invalid");
    expect(url.searchParams.get("snapshot_id")).toBe(list.page.snapshot_id);
  }
});

test("live adapter preserves the service snapshot and does not fabricate group evidence", async () => {
  const fixture = new FixtureJobSiftApi();
  const list = await fixture.listJobs(query);
  const group = await fixture.getJob(query.client_id, list.data[0].delivery_group_id, list.page.snapshot_id);
  const original = globalThis.fetch;
  const previousBase = process.env.JOBSIFT_SERVICE_URL;
  process.env.JOBSIFT_SERVICE_URL = "http://127.0.0.1:8000";
  const requested: string[] = [];
  globalThis.fetch = async (input) => {
    requested.push(String(input));
    return Response.json(requested.length === 1 ? list : {
      ...group,
      data: { ...group.data, provenance: { ...group.data.provenance, source_target: { source: "greenhouse" } } },
    });
  };
  try {
    const api = new LiveJobSiftApi();
    const result = await api.listJobs({ ...query, decision: ["strong_match"], source: ["greenhouse"] });
    expect(result.page.snapshot_id).toBe(list.page.snapshot_id);
    expect(result.data[0].delivery_group_id).toBe(list.data[0].delivery_group_id);
    const detail = await api.getJob(query.client_id, result.data[0].delivery_group_id, result.page.snapshot_id);
    expect(detail.data.provenance.source_target).toBe("source: greenhouse");
    expect(requested[0]).toContain("representation=groups");
    expect(requested[0]).toContain("decision=strong_match");
    expect(requested[1]).toContain(`snapshot_id=${list.page.snapshot_id}`);
  } finally {
    globalThis.fetch = original;
    if (previousBase === undefined) delete process.env.JOBSIFT_SERVICE_URL;
    else process.env.JOBSIFT_SERVICE_URL = previousBase;
  }
});

test("live adapter reads postings and snapshot detail independently of group representation", async () => {
  const original = globalThis.fetch;
  const previousBase = process.env.JOBSIFT_SERVICE_URL;
  process.env.JOBSIFT_SERVICE_URL = "http://127.0.0.1:8000";
  const posting = { posting_id: "job-1", title: "Support Engineer" };
  const envelope = { data: [posting], meta: { snapshot_id: "snapshot-1" }, page: { snapshot_id: "snapshot-1" } };
  const urls: string[] = [];
  globalThis.fetch = async input => {
    urls.push(String(input));
    return Response.json(urls.length === 1 ? envelope : { data: posting, meta: { snapshot_id: "snapshot-1" } });
  };
  try {
    const api = new LiveJobSiftApi();
    const list = await api.listPostings({ ...query, destination_id: "", q: "support" });
    expect(list.data[0].posting_id).toBe("job-1");
    await api.getPosting(query.client_id, "job-1", list.page.snapshot_id);
    expect(urls[0]).toContain("representation=postings");
    expect(urls[0]).not.toContain("destination_id=");
    expect(urls[1]).toContain("/jobs/postings/job-1?snapshot_id=snapshot-1");
  } finally {
    globalThis.fetch = original;
    if (previousBase === undefined) delete process.env.JOBSIFT_SERVICE_URL;
    else process.env.JOBSIFT_SERVICE_URL = previousBase;
  }
});

test("same-origin route forwards encoded opaque identifiers and blocks traversal", async () => {
  const original = globalThis.fetch;
  const previousBase = process.env.JOBSIFT_SERVICE_URL;
  process.env.JOBSIFT_SERVICE_URL = "http://127.0.0.1:8000";
  let forwarded = "";
  globalThis.fetch = async input => {
    forwarded = String(input);
    return Response.json({ data: { posting_id: "legacy.job 1" } });
  };
  const request = new NextRequest("http://127.0.0.1:3000/api/operator/clients/client.one/jobs/postings/legacy.job%201");
  try {
    const response = await proxyRead(request, { params: Promise.resolve({ path: ["clients", "client.one", "jobs", "postings", "legacy.job 1"] }) });
    expect(response.status).toBe(200);
    expect(forwarded).toContain("/clients/client.one/jobs/postings/legacy.job%201");
    const bad = await proxyRead(request, { params: Promise.resolve({ path: ["clients", "..", "jobs"] }) });
    expect(bad.status).toBe(400);
  } finally {
    globalThis.fetch = original;
    if (previousBase === undefined) delete process.env.JOBSIFT_SERVICE_URL;
    else process.env.JOBSIFT_SERVICE_URL = previousBase;
  }
});

test("history pagination and metadata describe the same client snapshot", async () => {
  const history = await new FixtureJobSiftApi().getHistory(query.client_id);
  expect(history.page.limit).toBe(40);
  expect(history.page.snapshot_id).toBe(history.meta.snapshot_id);
  expect(history.page.snapshot_id).toBeTruthy();
  expect(Date.parse(history.page.expires_at)).toBeGreaterThan(Date.now());
  expect(history.meta.scope).toEqual({ client_id: query.client_id,
    brief_revision_id: null, run_id: null, destination_id: null, cohort_id: null });
  expect(history.meta.source_failures).toEqual([]);
});


test("backend preflight allows the launch path to create a missing provisioning secret", async () => {
  const originalFetch = globalThis.fetch;
  const previousToken = process.env.JOBSIFT_GITHUB_TOKEN;
  const previousKey = process.env.JOBSIFT_PROVISIONING_KEY;
  process.env.JOBSIFT_GITHUB_TOKEN = "test-github-token";
  process.env.JOBSIFT_PROVISIONING_KEY = Buffer.alloc(32, 8).toString("base64url");

  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/actions/secrets/public-key")) {
      return Response.json({
        key: Buffer.alloc(32, 2).toString("base64"),
        key_id: "test-key",
      });
    }
    if (
      url.endsWith("/actions/workflows/operator-client-provision.yml") ||
      url.endsWith("/actions/workflows/operator-health-check.yml")
    ) {
      return Response.json({ state: "active" });
    }
    if (url.endsWith("/actions/secrets/JOBSIFT_PROVISIONING_KEY")) {
      return new Response(null, { status: 404 });
    }
    return Response.json({ name: url.split("/").pop() });
  };

  try {
    const request = new NextRequest("http://localhost:3000/api/onboarding/health");
    const response = await onboardingHealth(request);
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.data.ready).toBe(true);
    expect(body.data.checks.repository_secrets.JOBSIFT_PROVISIONING_KEY.status).toBe("missing");
  } finally {
    globalThis.fetch = originalFetch;
    if (previousToken === undefined) delete process.env.JOBSIFT_GITHUB_TOKEN;
    else process.env.JOBSIFT_GITHUB_TOKEN = previousToken;
    if (previousKey === undefined) delete process.env.JOBSIFT_PROVISIONING_KEY;
    else process.env.JOBSIFT_PROVISIONING_KEY = previousKey;
  }
});


test("onboarding launch failures include a trace ID and exact GitHub stage", async () => {
  const originalFetch = globalThis.fetch;
  const previousToken = process.env.JOBSIFT_GITHUB_TOKEN;
  const previousKey = process.env.JOBSIFT_PROVISIONING_KEY;
  const calls: string[] = [];
  process.env.JOBSIFT_GITHUB_TOKEN = "test-github-token";
  process.env.JOBSIFT_PROVISIONING_KEY = Buffer.alloc(32, 9).toString("base64url");
  globalThis.fetch = async (input) => {
    const url = String(input);
    calls.push(url);
    return new Response(JSON.stringify({ message: "forbidden" }), { status: 403 });
  };

  try {
    const request = new NextRequest("http://localhost:3000/api/onboarding", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: "http://localhost:3000",
        "sec-fetch-site": "same-origin",
      },
      body: JSON.stringify({ operation: "health_check", payload: {} }),
    });
    const response = await onboardingLaunch(request);
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(body.error).toMatchObject({
      code: "GITHUB_PUBLIC_KEY_REQUEST_FAILED",
      stage: "public_key_fetch",
      http_status: 403,
    });
    expect(body.data.state).toBe("failed");
    expect(body.data.operation).toBe("health_check");
    expect(body.data.control_request_id).toBe(body.meta.trace_id);
    expect(response.headers.get("X-JobSift-Trace-Id")).toBe(body.meta.trace_id);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("/actions/secrets/public-key");
  } finally {
    globalThis.fetch = originalFetch;
    if (previousToken === undefined) delete process.env.JOBSIFT_GITHUB_TOKEN;
    else process.env.JOBSIFT_GITHUB_TOKEN = previousToken;
    if (previousKey === undefined) delete process.env.JOBSIFT_PROVISIONING_KEY;
    else process.env.JOBSIFT_PROVISIONING_KEY = previousKey;
  }
});


test("health operation dispatches to the isolated non-mutating workflow", async () => {
  await sodium.ready;
  const originalFetch = globalThis.fetch;
  const previousToken = process.env.JOBSIFT_GITHUB_TOKEN;
  const previousKey = process.env.JOBSIFT_PROVISIONING_KEY;
  const calls: Array<{ url: string; method: string; body?: string }> = [];
  const keyPair = sodium.crypto_box_keypair();
  const publicKey = sodium.to_base64(
    keyPair.publicKey,
    sodium.base64_variants.ORIGINAL,
  );
  process.env.JOBSIFT_GITHUB_TOKEN = "test-github-token";
  process.env.JOBSIFT_PROVISIONING_KEY = Buffer.alloc(32, 10).toString("base64url");

  globalThis.fetch = async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({
      url,
      method,
      body: typeof init?.body === "string" ? init.body : undefined,
    });
    if (url.endsWith("/actions/secrets/public-key")) {
      return Response.json({ key: publicKey, key_id: "test-key" });
    }
    if (url.endsWith("/actions/secrets/JOBSIFT_PROVISIONING_KEY") && method === "PUT") {
      return new Response(null, { status: 201 });
    }
    if (url.endsWith("/actions/workflows/operator-health-check.yml/dispatches")) {
      return new Response(null, { status: 204 });
    }
    return new Response(null, { status: 404 });
  };

  try {
    const request = new NextRequest("http://localhost:3000/api/onboarding", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Origin: "http://localhost:3000",
        "sec-fetch-site": "same-origin",
      },
      body: JSON.stringify({ operation: "health_check", payload: {} }),
    });
    const response = await onboardingLaunch(request);
    const body = await response.json();
    const dispatch = calls.find((call) => call.url.endsWith("/dispatches"));
    const dispatchBody = dispatch?.body ? JSON.parse(dispatch.body) : null;

    expect(response.status).toBe(202);
    expect(body.data).toMatchObject({
      state: "queued",
      operation: "health_check",
    });
    expect(body.data.control_request_id).toBe(body.meta.trace_id);
    expect(body.data.request_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(dispatch?.url).toContain("/operator-health-check.yml/dispatches");
    expect(dispatchBody.inputs).toMatchObject({
      operation: "health_check",
      request_id: body.data.request_id,
      control_request_id: body.data.control_request_id,
    });
    expect(dispatchBody.inputs.payload_ciphertext).not.toBe("{}");
  } finally {
    globalThis.fetch = originalFetch;
    if (previousToken === undefined) delete process.env.JOBSIFT_GITHUB_TOKEN;
    else process.env.JOBSIFT_GITHUB_TOKEN = previousToken;
    if (previousKey === undefined) delete process.env.JOBSIFT_PROVISIONING_KEY;
    else process.env.JOBSIFT_PROVISIONING_KEY = previousKey;
  }
});
