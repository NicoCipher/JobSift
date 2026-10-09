export type OperatorResponseBody = {
  data?: unknown;
  error?: {
    message?: unknown;
    code?: unknown;
  };
};

function statusExplanation(status: number, fallback: string) {
  if (status === 401) return "Your owner session has expired. Sign in again before retrying.";
  if (status === 403) return "This action was denied. Check your owner access and connection.";
  if (status === 408 || status === 504) return "JobSift timed out. Check the latest state before trying again.";
  if (status === 429) return "JobSift has reached a request limit. Wait before checking again.";
  if (status >= 500) return "JobSift is temporarily unavailable. Your latest delivery state is not confirmed.";
  return fallback;
}

/**
 * API responses cannot be assumed to be JSON: auth proxies, unavailable
 * backends and upstream errors sometimes return HTML. Never imply a
 * command succeeded when a reply is unreadable.
 */
export async function readOperatorJson(
  response: Response,
  fallback = "JobSift couldn't complete the request.",
): Promise<OperatorResponseBody> {
  let body: OperatorResponseBody;
  try {
    body = (await response.json()) as OperatorResponseBody;
  } catch {
    throw new Error(
      response.ok
        ? "JobSift's reply could not be verified. Check current status before repeating an action."
        : statusExplanation(response.status, fallback),
    );
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("JobSift returned an unexpected reply. Check current status before retrying.");
  }
  if (!response.ok) {
    const message = body.error?.message;
    throw new Error(
      typeof message === "string" && message.trim()
        ? message
        : statusExplanation(response.status, fallback),
    );
  }
  return body;
}
