import type { JobSiftApi } from "./interface";
import { FixtureJobSiftApi } from "./fixture-api";
import { LiveJobSiftApi } from "./live-api";
export const liveMode = process.env.NEXT_PUBLIC_JOBSIFT_LIVE === "1";
export const api: JobSiftApi = liveMode ? new LiveJobSiftApi() : new FixtureJobSiftApi();
