import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import {
  ApiNetworkError,
  ApiProtocolError,
  apiRequest,
  apiRequestNoContent,
} from "./client";
import { server } from "../test/server";

const hasValue = (value: unknown): value is { value: string } =>
  typeof value === "object" && value !== null && "value" in value && typeof value.value === "string";

describe("api client", () => {
  it("uses a relative API path, JSON body, and browser credentials", async () => {
    server.use(
      http.post("/api/v1/example", async ({ request }) => {
        expect(request.credentials).toBe("include");
        expect(request.headers.get("content-type")).toBe("application/json");
        await expect(request.json()).resolves.toEqual({ value: "input" });
        return HttpResponse.json({ value: "output" });
      }),
    );

    await expect(
      apiRequest("/example", hasValue, { method: "POST", body: { value: "input" } }),
    ).resolves.toEqual({ value: "output" });
  });

  it("accepts a server-confirmed 204 without parsing JSON", async () => {
    server.use(
      http.post("/api/v1/empty", () => new HttpResponse(null, { status: 204 })),
    );

    await expect(
      apiRequestNoContent("/empty", { method: "POST" }),
    ).resolves.toBeUndefined();
  });

  it("preserves validated safe HTTP error details", async () => {
    server.use(
      http.get("/api/v1/example", () =>
        HttpResponse.json(
          { error: { code: "service_unavailable", message: "Please try again." } },
          { status: 503 },
        ),
      ),
    );

    await expect(apiRequest("/example", hasValue)).rejects.toMatchObject({
      name: "ApiHttpError",
      status: 503,
      code: "service_unavailable",
      message: "Please try again.",
    });
  });

  it.each([
    ["a non-JSON success", new HttpResponse("not json", { status: 200 })],
    ["a malformed JSON success", HttpResponse.json({ unexpected: true })],
  ])("rejects %s as a protocol error", async (_name, response) => {
    server.use(http.get("/api/v1/example", () => response));

    await expect(apiRequest("/example", hasValue)).rejects.toBeInstanceOf(ApiProtocolError);
  });

  it("normalizes network failure without treating it as an API response", async () => {
    server.use(http.get("/api/v1/example", () => HttpResponse.error()));

    await expect(apiRequest("/example", hasValue)).rejects.toBeInstanceOf(ApiNetworkError);
  });

  it("preserves request cancellation instead of reporting a network failure", async () => {
    const abortError = new DOMException("The operation was aborted.", "AbortError");
    vi.spyOn(globalThis, "fetch").mockRejectedValueOnce(abortError);

    await expect(apiRequest("/example", hasValue)).rejects.toBe(abortError);
  });
});
