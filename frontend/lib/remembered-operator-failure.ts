/**
 * Local UI reminder for an explicitly correlated failed JobSift operation.
 * Only a request UUID and its already-public workflow link are stored.
 * It is an alert, never proof that an operation succeeded or permission to retry.
 */
export type RememberedFailedOperation = {
  requestId: string;
  runNumber: number;
  url: string;
};

const KEY = "jobsift:unresolved-failed-operation:v1";
const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function readRememberedFailure(): RememberedFailedOperation | null {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(KEY) ?? "null");
    if (!value || typeof value !== "object") return null;
    const record = value as Partial<RememberedFailedOperation>;
    if (typeof record.requestId !== "string" || !REQUEST_ID.test(record.requestId)) return null;
    if (!Number.isSafeInteger(record.runNumber) || (record.runNumber ?? 0) < 1) return null;
    if (typeof record.url !== "string") return null;
    const url = new URL(record.url);
    if (url.protocol !== "https:" || url.hostname !== "github.com") return null;
    return {
      requestId: record.requestId.toLowerCase(),
      runNumber: record.runNumber,
      url: url.toString(),
    };
  } catch {
    return null;
  }
}

export function rememberFailedOperation(value: RememberedFailedOperation) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    // Storage might be blocked; in-memory alert still applies this visit.
  }
}

export function clearRememberedFailure() {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // Never block a verified success because browser storage is restricted.
  }
}
