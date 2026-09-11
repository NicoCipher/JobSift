import type { JobSiftApi } from "./interface";
import { FixtureJobSiftApi } from "./fixture-api";
/** Only transport selection point. No BFF/auth/network implementation in JOB-31. */
export const api: JobSiftApi = new FixtureJobSiftApi();
