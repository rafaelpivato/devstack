/** HTTP is presentation transport only. Flask and controller remain authoritative. */
export type ApiErrorKind = "unavailable" | "unauthorized" | "conflict" | "invalid" | "not-found" | "unexpected";
export class ApiError extends Error {
  constructor(public readonly kind: ApiErrorKind, message: string, public readonly status?: number) {
    super(message);
    this.name = "ApiError";
  }
}

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
export interface RequestOptions {
  method?: Method;
  body?: unknown;
  signal?: AbortSignal;
  /** Tests can supply a token without a DOM. Never persist tokens in local storage. */
  csrfToken?: string;
  idempotencyKey?: string;
}

function kindFor(status: number): ApiErrorKind {
  if (status === 403) return "unauthorized";
  if (status === 404) return "not-found";
  if (status === 409) return "conflict";
  if (status === 400 || status === 413 || status === 415 || status === 422) return "invalid";
  if (status === 503 || status === 502 || status === 504) return "unavailable";
  return "unexpected";
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
  transport: typeof fetch = fetch,
): Promise<T> {
  // Prevent redirecting cookies/CSRF tokens to a remote host, even on caller error.
  if (!path.startsWith("/api/v2/") || path.startsWith("//") || path.split("/").includes("..")) {
    throw new ApiError("invalid", "Invalid DevStack API path.");
  }
  const method = options.method ?? "GET";
  const headers = new Headers({ Accept: "application/json" });
  if (method !== "GET") {
    const csrf = options.csrfToken ??
      (typeof document === "undefined" ? "" :
        document.querySelector<HTMLMetaElement>('meta[name="devstack-csrf"]')?.content ?? "");
    if (!csrf) throw new ApiError("unauthorized", "Missing CSRF token. Reload DevStack.");
    headers.set("X-CSRF-Token", csrf);
    if (options.idempotencyKey !== undefined) {
      if (!/^[0-9a-f]{32}$/.test(options.idempotencyKey))
        throw new ApiError("invalid", "Invalid idempotency key.");
      headers.set("Idempotency-Key", options.idempotencyKey);
    }
  }
  if (options.body !== undefined) headers.set("Content-Type", "application/json");

  let response: Response;
  try {
    response = await transport(path, {
      method, credentials: "same-origin", cache: "no-store", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (cause) {
    if (options.signal?.aborted) throw cause;
    throw new ApiError("unavailable", "Cannot reach DevStack. Check the connection.");
  }

  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new ApiError("unexpected", "DevStack returned an invalid response.", response.status);
  }
  if (!response.ok) {
    const safeMessage = typeof data === "object" && data !== null && "message" in data &&
      typeof data.message === "string" && data.message.length <= 300
      ? data.message : "DevStack could not complete the request.";
    throw new ApiError(kindFor(response.status), safeMessage, response.status);
  }
  return data as T;
}
