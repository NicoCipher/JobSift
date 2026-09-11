import type {
  ApiError,
  ApiErrorCode,
  ApiResponse,
  HistoryEntry,
  JobGroupDetail,
  JobGroupSummary,
  ListResponse,
} from "../contracts/service";
import type { JobSiftApi, JobsQuery } from "./interface";
import * as fixture from "../fixtures/evidence";
export class ServiceError extends Error {
  constructor(
    public status: number,
    public body: ApiError,
  ) {
    super(body.error.message);
  }
}
function fail(code: ApiErrorCode, message: string, status = 409): never {
  throw new ServiceError(status, {
    error: {
      code,
      message,
      request_id: "example-request",
      retryable: status === 503,
      details: {},
    },
  });
}
type Snapshot = {
  id: string;
  fingerprint: string;
  query: JobsQuery;
  rows: JobGroupDetail[];
  expires: number;
};
/** Query behavior emulates a server only over fixed authored examples. Never usable with real source data. */
export class FixtureJobSiftApi implements JobSiftApi {
  private snapshots = new Map<string, Snapshot>();
  private cursors = new Map<string, { snapshot: string; offset: number }>();
  private scope(clientId: string) {
    if (clientId !== fixture.CLIENT_ID)
      fail("NOT_FOUND", "Client not found.", 404);
  }
  private response<T>(data: T): ApiResponse<T> {
    return structuredClone({ data, meta: fixture.meta });
  }
  async getSession() {
    return this.response(fixture.session);
  }
  async getClient(id: string) {
    this.scope(id);
    return this.response(fixture.client);
  }
  async getBrief(id: string) {
    this.scope(id);
    return this.response(fixture.brief);
  }
  async getRun(id: string) {
    this.scope(id);
    return this.response(fixture.run);
  }
  async getDiagnostics(id: string) {
    this.scope(id);
    const result = this.response(fixture.diagnostics);
    result.meta.scope.cohort_id = fixture.diagnostics.cohort_id;
    result.meta.scope.run_id = null;
    result.meta.completeness = "complete";
    result.meta.source_failures = [];
    return result;
  }
  async getHistory(id: string): Promise<ListResponse<HistoryEntry>> {
    this.scope(id);
    const snapshotId = `fixture-history-${crypto.randomUUID()}`;
    const response = this.response(fixture.history);
    return {
      ...response,
      meta: {
        ...response.meta,
        scope: { client_id: id, brief_revision_id: null, run_id: null,
          destination_id: null, cohort_id: null },
        snapshot_id: snapshotId,
        served_at: new Date().toISOString(),
        completeness: "complete",
        source_failures: [],
        limitations: ["fictional_development_evidence"],
        supported_filters: [],
        supported_sorts: [],
      },
      page: {
        limit: 40,
        next_cursor: null,
        previous_cursor: null,
        known_total: fixture.metric(response.data.length, "history_entries", "entries_in_fictional_client_history"),
        snapshot_id: snapshotId,
        expires_at: new Date(Date.now() + 30 * 60 * 1000).toISOString(),
      },
    };
  }
  async listJobs(query: JobsQuery): Promise<ListResponse<JobGroupSummary>> {
    this.scope(query.client_id);
    if (query.destination_id !== fixture.DESTINATION_ID)
      fail("NOT_FOUND", "Destination not found.", 404);
    if ((query.q?.trim().length ?? 0) > 200)
      fail("VALIDATION_ERROR", "Search must be 200 characters or fewer.", 422);
    const decisions = query.decision ?? ["strong_match", "possible_match"];
    if (
      decisions.some(
        (d) =>
          ![
            "strong_match",
            "possible_match",
            "needs_review",
            "reject",
          ].includes(d),
      )
    )
      fail("VALIDATION_ERROR", "Unrecognized match decision.", 422);
    const normalized = {
      client_id: query.client_id,
      destination_id: query.destination_id,
      q: query.q?.trim().toLowerCase() ?? "",
      decision: [...decisions].sort(),
      source: [...(query.source ?? [])].sort(),
      limit: query.limit ?? 40,
    };
    const fingerprint = JSON.stringify(normalized);
    let snapshot: Snapshot;
    let offset = 0;
    if (query.cursor) {
      const cursor = this.cursors.get(query.cursor);
      if (!cursor)
        fail(
          "INVALID_CURSOR",
          "This fixture cursor is no longer available. Clear filters to refresh.",
          400,
        );
      const found = this.snapshots.get(cursor.snapshot);
      if (!found || found.expires < Date.now())
        fail(
          "SNAPSHOT_EXPIRED",
          "This result snapshot has expired. Clear filters to refresh.",
        );
      if (found.fingerprint !== fingerprint || (query.snapshot_id && query.snapshot_id !== found.id))
        fail("INVALID_CURSOR", "The cursor does not match these filters or snapshot.", 400);
      snapshot = found;
      offset = cursor.offset;
    } else if (query.snapshot_id) {
      const found = this.snapshots.get(query.snapshot_id);
      if (!found || found.expires < Date.now())
        fail(
          "SNAPSHOT_EXPIRED",
          "This result snapshot has expired. Clear filters to refresh.",
        );
      if (found.fingerprint !== fingerprint)
        fail(
          "INVALID_CURSOR",
          "The snapshot does not match these filters.",
          400,
        );
      snapshot = found;
    } else {
      // Fixture filtering/sorting is adapter-only: decisions/group membership were already authored.
      const rows = fixture.groups.filter((group) => {
        const p = group.representative_posting;
        return (
          p &&
          decisions.includes(group.match!.decision) &&
          (!normalized.source.length || normalized.source.includes(p.source)) &&
          `${p.title} ${p.company} ${p.location_text ?? ""}`
            .toLowerCase()
            .includes(normalized.q)
        );
      });
      snapshot = {
        id: `fixture-${crypto.randomUUID()}`,
        fingerprint,
        query: normalized,
        rows: structuredClone(rows),
        expires: Date.now() + 30 * 60 * 1000,
      };
      this.snapshots.set(snapshot.id, snapshot);
    }
    const cursorFor = (position: number) => {
      const token = `cursor-${crypto.randomUUID()}`;
      this.cursors.set(token, { snapshot: snapshot.id, offset: position });
      return token;
    };
    const limit = normalized.limit;
    const data = snapshot.rows.slice(offset, offset + limit).map((detail) => {
      // Lists intentionally carry no description/member payloads.
      const {
        resource_type,
        delivery_group_id,
        representative_posting,
        representative_basis,
        member_count,
        member_completeness,
        match,
        delivery_state,
        outcome_summary,
        detail_url,
      } = detail;
      return {
        resource_type,
        delivery_group_id,
        representative_posting,
        representative_basis,
        member_count,
        member_completeness,
        match,
        delivery_state,
        outcome_summary,
        detail_url: `${detail_url}?${new URLSearchParams({ snapshot_id: snapshot.id })}`,
      };
    });
    return {
      ...this.response(data),
      meta: { ...structuredClone(fixture.meta), snapshot_id: snapshot.id },
      page: {
        limit,
        next_cursor:
          offset + limit < snapshot.rows.length
            ? cursorFor(offset + limit)
            : null,
        previous_cursor:
          offset > 0 ? cursorFor(Math.max(0, offset - limit)) : null,
        known_total: fixture.metric(
          snapshot.rows.length,
          "groups",
          "groups_in_fictional_filtered_scope",
        ),
        snapshot_id: snapshot.id,
        expires_at: new Date(snapshot.expires).toISOString(),
      },
    };
  }
  async getJob(
    clientId: string,
    groupId: string,
    snapshotId: string,
  ): Promise<ApiResponse<JobGroupDetail>> {
    this.scope(clientId);
    const snapshot = this.snapshots.get(snapshotId);
    if (!snapshot || snapshot.expires < Date.now())
      fail(
        "SNAPSHOT_EXPIRED",
        "This result snapshot has expired. Refresh the Jobs list.",
      );
    const group = snapshot.rows.find(
      (row) => row.delivery_group_id === groupId,
    );
    if (!group) fail("NOT_FOUND", "Group not found in this snapshot.", 404);
    return {
      ...this.response({
        ...group,
        detail_url: `${group.detail_url}?${new URLSearchParams({ snapshot_id: snapshotId })}`,
      }),
      meta: { ...structuredClone(fixture.meta), snapshot_id: snapshotId },
    };
  }
}
