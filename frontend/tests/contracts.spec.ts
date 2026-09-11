import { test, expect } from "@playwright/test";
import {
  applicationLabel,
  decisionLabel,
  factText,
  metricText,
  safeExternalUrl,
} from "../lib/display";
import { FixtureJobSiftApi } from "../lib/api/fixture-api";
import type { ApiError } from "../lib/contracts/service";
const query = {
  client_id: "example-client",
  destination_id: "example-destination",
};
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
