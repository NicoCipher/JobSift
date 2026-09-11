import type {
  ApiResponse,
  BriefRevision,
  Client,
  Decision,
  Diagnostics,
  HistoryEntry,
  JobGroupDetail,
  JobGroupSummary,
  ListResponse,
  RunDetail,
  Session,
} from "../contracts/service";
export type JobsQuery = {
  client_id: string;
  destination_id: string;
  q?: string;
  decision?: Decision[];
  source?: string[];
  cursor?: string;
  snapshot_id?: string;
  limit?: 20 | 40 | 80;
};
export interface JobSiftApi {
  getSession(): Promise<ApiResponse<Session>>;
  getClient(clientId: string): Promise<ApiResponse<Client>>;
  listJobs(query: JobsQuery): Promise<ListResponse<JobGroupSummary>>;
  getJob(
    clientId: string,
    groupId: string,
    snapshotId: string,
  ): Promise<ApiResponse<JobGroupDetail>>;
  getBrief(clientId: string): Promise<ApiResponse<BriefRevision>>;
  getRun(clientId: string): Promise<ApiResponse<RunDetail>>;
  getDiagnostics(clientId: string): Promise<ApiResponse<Diagnostics>>;
  getHistory(clientId: string): Promise<ListResponse<HistoryEntry>>;
}
