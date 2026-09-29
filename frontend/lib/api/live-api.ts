import type { ApiResponse, BriefRevision, Client, Diagnostics, HistoryEntry, JobGroupDetail, JobGroupSummary, ListResponse, RunDetail, Session } from "../contracts/service";
import type { JobSiftApi, JobsQuery } from "./interface";

const root = "/api/operator";
async function read<T>(path: string, params?: URLSearchParams): Promise<T> {
  const base = typeof window === "undefined" ? process.env.JOBSIFT_SERVICE_URL : undefined;
  if (typeof window === "undefined" && (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base))) {
    throw new Error("Local operator service is not configured.");
  }
  const url = `${base ? `${base}/api/v1` : root}/${path}${params?.size ? `?${params}` : ""}`;
  const response = await fetch(url, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body?.error?.message ?? "Could not load operator evidence.");
  return body as T;
}
const clientPath = (id: string) => `clients/${encodeURIComponent(id)}`;

export class LiveJobSiftApi implements JobSiftApi {
  getSession() { return read<ApiResponse<Session>>("session"); }
  getClient(id: string) { return read<ApiResponse<Client>>(clientPath(id)); }
  listJobs(query: JobsQuery) {
    const params = new URLSearchParams({ representation: "groups", limit: String(query.limit ?? 40) });
    if (query.destination_id) params.set("destination_id", query.destination_id);
    if (query.q) params.set("q", query.q);
    if (query.cursor) params.set("cursor", query.cursor);
    if (query.snapshot_id) params.set("snapshot_id", query.snapshot_id);
    query.decision?.forEach((value) => params.append("decision", value));
    query.source?.forEach((value) => params.append("source", value));
    return read<ListResponse<JobGroupSummary>>(`${clientPath(query.client_id)}/jobs`, params);
  }
  async getJob(id: string, groupId: string, snapshotId: string) {
    const params = new URLSearchParams({ snapshot_id: snapshotId });
    const response = await read<ApiResponse<JobGroupDetail>>(`${clientPath(id)}/jobs/groups/${encodeURIComponent(groupId)}`, params);
    const raw = response.data;
    return { ...response, data: {
      ...raw,
      description_text: (raw.representative_posting as (typeof raw.representative_posting & { description_text?: string }) | null)?.description_text ?? "Description not reported.",
      provenance: {
        ...raw.provenance,
        source_target: typeof raw.provenance.source_target === "object"
          ? Object.entries(raw.provenance.source_target).map(([key, value]) => `${key}: ${value}`).join(", ") || "Not reported"
          : raw.provenance.source_target,
      },
    } };
  }
  async getBrief(id: string) {
    const list = await read<ListResponse<{ brief_id: string }>>(`${clientPath(id)}/briefs`);
    const brief = list.data[0];
    if (!brief) throw new Error("No registered brief is available for this client.");
    const revisions = await read<ListResponse<{ brief_revision_id: string }>>(`${clientPath(id)}/briefs/${encodeURIComponent(brief.brief_id)}/revisions`);
    if (!revisions.data[0]) throw new Error("No registered brief revision is available.");
    return read<ApiResponse<BriefRevision>>(`${clientPath(id)}/briefs/${encodeURIComponent(revisions.data[0].brief_revision_id)}`);
  }
  getHistory(id: string) { return read<ListResponse<HistoryEntry>>(`${clientPath(id)}/history`); }
  getRun(): Promise<ApiResponse<RunDetail>> { return Promise.reject(new Error("Run reads are not available in the operator service.")); }
  getDiagnostics(): Promise<ApiResponse<Diagnostics>> { return Promise.reject(new Error("Diagnostics reads are not available in the operator service.")); }
}
