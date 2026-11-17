export type ProvisioningSyncFailureCode =
  | "PROVISIONING_KEY_INVALID"
  | "GITHUB_PUBLIC_KEY_REQUEST_FAILED"
  | "GITHUB_PUBLIC_KEY_RESPONSE_INVALID"
  | "GITHUB_SECRET_ENCRYPTION_FAILED"
  | "GITHUB_SECRET_WRITE_FAILED"
  | "GITHUB_SYNC_UNEXPECTED";

export class ProvisioningSyncError extends Error {
  constructor(
    readonly code: ProvisioningSyncFailureCode,
    readonly httpStatus?: number,
  ) {
    super(code);
    this.name = "ProvisioningSyncError";
  }
}

export type ProvisioningSyncFailure = {
  code: ProvisioningSyncFailureCode;
  stage:
    | "provisioning_key_validation"
    | "public_key_fetch"
    | "public_key_validation"
    | "secret_encryption"
    | "secret_write"
    | "unknown";
  httpStatus?: number;
  message: string;
};

export function describeProvisioningSyncFailure(
  error: unknown,
): ProvisioningSyncFailure {
  const known = error instanceof ProvisioningSyncError
    ? error
    : new ProvisioningSyncError("GITHUB_SYNC_UNEXPECTED");

  const status = known.httpStatus;
  let stage: ProvisioningSyncFailure["stage"];
  let message: string;

  switch (known.code) {
    case "PROVISIONING_KEY_INVALID":
      stage = "provisioning_key_validation";
      message =
        "The server-side provisioning key is invalid. Check JOBSIFT_PROVISIONING_KEY in Vercel Production.";
      break;
    case "GITHUB_PUBLIC_KEY_REQUEST_FAILED":
      stage = "public_key_fetch";
      message = status === 401
        ? "GitHub rejected the configured token while retrieving the Actions public key (HTTP 401). Replace the token in Vercel Production."
        : status === 403
          ? "GitHub denied access to this repository's Actions public key (HTTP 403). Check the token's repository selection and any organization access policies."
          : status === 404
            ? "GitHub could not find the repository or Actions secrets endpoint (HTTP 404). Check the token's repository selection and repository Actions availability."
            : status
              ? `GitHub could not return this repository's Actions public key (HTTP ${status}).`
              : "JobSift could not reach GitHub to retrieve this repository's Actions public key. Retry, then check GitHub API or network access if it continues.";
      break;
    case "GITHUB_PUBLIC_KEY_RESPONSE_INVALID":
      stage = "public_key_validation";
      message =
        "GitHub returned an invalid Actions public-key response. Retry; if it continues, check the GitHub API response and deployment logs.";
      break;
    case "GITHUB_SECRET_ENCRYPTION_FAILED":
      stage = "secret_encryption";
      message =
        "JobSift retrieved the GitHub public key but could not encrypt the provisioning secret. Check the deployed encryption dependency and deployment logs.";
      break;
    case "GITHUB_SECRET_WRITE_FAILED":
      stage = "secret_write";
      message = status === 401
        ? "GitHub rejected the configured token during the Actions secret write (HTTP 401). Replace the token in Vercel Production."
        : status === 403
          ? "GitHub refused the Actions secret write (HTTP 403). The token is configured for Actions read/write, so check that it is scoped to NicoCipher/JobSift and that organization policies permit secret writes."
          : status === 404
            ? "GitHub could not find the repository or Actions secret endpoint (HTTP 404). Check the token's repository selection and repository Actions availability."
            : status === 422
              ? "GitHub rejected the encrypted Actions secret payload (HTTP 422). The public key or encrypted payload was not accepted; retry and inspect the deployment logs if it repeats."
              : status
                ? `GitHub rejected the Actions secret write (HTTP ${status}).`
                : "JobSift could not complete the GitHub Actions secret write because the GitHub API request failed. Retry, then check GitHub API or network access if it continues.";
      break;
    default:
      stage = "unknown";
      message =
        "An unexpected server error interrupted secure onboarding before the GitHub Actions secret could be synced. Retry once; if it repeats, inspect the deployment logs.";
  }

  return {
    code: known.code,
    stage,
    ...(status ? { httpStatus: status } : {}),
    message,
  };
}
