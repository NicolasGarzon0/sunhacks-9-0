export type SendErrorCode = "NOT_ALLOWED" | "RATE_LIMITED" | "SEND_FAILED";

export interface ClassifiedError {
  code: SendErrorCode;
  detail: string;
  retryAfter?: number;
  /** Raw IMessageError.code string, if present (for logs). */
  errCode?: string;
  /** Raw gRPC status number, if present (for logs). */
  grpcCode?: number;
}

// gRPC RESOURCE_EXHAUSTED
const GRPC_RESOURCE_EXHAUSTED = 8;

// IMessageError.code values from @photon-ai/advanced-imessage's ErrorCode that are quota / throttle related.
const RATE_LIMIT_CODES = new Set([
  "dailyLimitExceeded",
  "recipientLimitExceeded",
  "uploadRateExceeded",
  "contentDuplicateExceeded",
  "recipientCoolingDown",
  "burstRateExceeded",
  "newContactThrottled",
  "sendReceiveRatioExceeded",
]);

/**
 * Classifies a thrown value by duck typing. @photon-ai/advanced-imessage is not a declared
 * dependency, so we never use instanceof. "Target not allowed for this project" has no dedicated
 * class and is detected by message substring.
 */
export function classifyError(err: unknown): ClassifiedError {
  const e = (typeof err === "object" && err !== null ? err : {}) as Record<string, unknown>;
  const message = typeof e.message === "string" ? e.message : typeof err === "string" ? err : "unknown error";
  const name = typeof e.name === "string" ? e.name : undefined;
  const errCode = typeof e.code === "string" ? e.code : undefined;
  const grpcCode = typeof e.grpcCode === "number" ? e.grpcCode : undefined;
  const retryAfter = typeof e.retryAfter === "number" ? e.retryAfter : undefined;
  const meta = {
    ...(errCode !== undefined ? { errCode } : {}),
    ...(grpcCode !== undefined ? { grpcCode } : {}),
  };

  if (message.includes("Target not allowed")) {
    return { code: "NOT_ALLOWED", detail: message, ...meta };
  }
  if (
    name === "RateLimitError" ||
    grpcCode === GRPC_RESOURCE_EXHAUSTED ||
    (errCode !== undefined && RATE_LIMIT_CODES.has(errCode))
  ) {
    return { code: "RATE_LIMITED", detail: message, ...(retryAfter !== undefined ? { retryAfter } : {}), ...meta };
  }
  return { code: "SEND_FAILED", detail: message, ...meta };
}
