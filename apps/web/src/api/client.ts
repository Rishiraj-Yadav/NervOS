import { parseApiError } from "./types";

export class ApiHttpError extends Error {
  readonly kind = "http";

  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiHttpError";
  }
}

export class ApiNetworkError extends Error {
  readonly kind = "network";

  constructor(cause: unknown) {
    super("Unable to reach NervOS. Check that the API is running and try again.", {
      cause,
    });
    this.name = "ApiNetworkError";
  }
}

export class ApiProtocolError extends Error {
  readonly kind = "protocol";

  constructor(message = "NervOS returned an unexpected response. Please try again.") {
    super(message);
    this.name = "ApiProtocolError";
  }
}

type Validator<T> = (value: unknown) => value is T;

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
}

const API_PREFIX = "/api/v1";

export async function apiRequest<T>(
  path: string,
  validator: Validator<T>,
  options: RequestOptions = {},
): Promise<T> {
  const response = await request(path, options);
  const payload = await readJson(response);

  if (!response.ok) {
    throwHttpError(response.status, payload);
  }
  if (!validator(payload)) {
    throw new ApiProtocolError();
  }
  return payload;
}

export async function apiRequestNoContent(
  path: string,
  options: RequestOptions,
): Promise<void> {
  const response = await request(path, options);
  if (!response.ok) {
    const payload = await readJson(response);
    throwHttpError(response.status, payload);
  }
  if (response.status !== 204) {
    throw new ApiProtocolError();
  }
}

async function request(path: string, options: RequestOptions): Promise<Response> {
  try {
    return await fetch(`${API_PREFIX}${path}`, {
      method: options.method ?? "GET",
      credentials: "include",
      headers: options.body === undefined ? undefined : { "Content-Type": "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ApiNetworkError(error);
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ApiProtocolError(
      response.ok
        ? undefined
        : `NervOS returned an unreadable error response (HTTP ${response.status}).`,
    );
  }
}

function throwHttpError(status: number, payload: unknown): never {
  const detail = parseApiError(payload);
  if (detail === null) {
    throw new ApiProtocolError(`NervOS returned an invalid error response (HTTP ${status}).`);
  }
  throw new ApiHttpError(status, detail.code, detail.message);
}
