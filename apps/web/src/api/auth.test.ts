import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiProtocolError } from "./client";
import { getCurrentUser, getSetupStatus, login, logout, setup } from "./auth";
import { server } from "../test/server";

const user = { id: 1, username: "admin", role: "admin", is_active: true };

describe("authentication API", () => {
  it("maps the verified setup, login, current-user, and logout contracts", async () => {
    server.use(
      http.get("/api/v1/setup/status", () => HttpResponse.json({ setup_complete: false })),
      http.post("/api/v1/setup", () => HttpResponse.json(user, { status: 201 })),
      http.post("/api/v1/auth/login", () => HttpResponse.json(user)),
      http.get("/api/v1/auth/me", () => HttpResponse.json(user)),
      http.post("/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
    );

    await expect(getSetupStatus()).resolves.toEqual({ setup_complete: false });
    await expect(setup({ username: "admin", password: "password1234" })).resolves.toEqual(user);
    await expect(login({ username: "admin", password: "password1234" })).resolves.toEqual(user);
    await expect(getCurrentUser()).resolves.toEqual(user);
    await expect(logout()).resolves.toBeUndefined();
  });

  it.each([
    ["setup status", "/api/v1/setup/status", { setup_complete: "no" }],
    ["user", "/api/v1/auth/me", { id: "1", username: "admin" }],
  ])("rejects a malformed %s response", async (_name, path, body) => {
    server.use(http.get(path, () => HttpResponse.json(body)));

    const request = path.endsWith("status") ? getSetupStatus() : getCurrentUser();
    await expect(request).rejects.toBeInstanceOf(ApiProtocolError);
  });
});
