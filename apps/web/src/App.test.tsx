import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { appRoutes } from "./router";
import { renderWithRouter } from "./test/render";
import { server } from "./test/server";

const apiUser = { id: 1, username: "admin", role: "admin", is_active: true };
const error = (status: number, code: string, message: string) =>
  HttpResponse.json({ error: { code, message } }, { status });

function setupStatus(complete: boolean) {
  return http.get("/api/v1/setup/status", () =>
    HttpResponse.json({ setup_complete: complete }),
  );
}

function authenticated() {
  return http.get("/api/v1/auth/me", () => HttpResponse.json(apiUser));
}

function unauthenticated() {
  return http.get("/api/v1/auth/me", () =>
    error(401, "authentication_required", "Authentication is required."),
  );
}

async function renderRoute(path: string) {
  const result = renderWithRouter(appRoutes, { initialEntries: [path] });
  return { user: userEvent.setup(), ...result };
}

async function fillCredentials(password = "correct horse battery") {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/^username$/i), "admin");
  await user.type(screen.getByLabelText(/^password$/i), password);
  return user;
}

async function fillSetup(password = "correct horse battery") {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/^username$/i), "admin");
  await user.type(screen.getByLabelText(/^password$/i), password);
  await user.type(screen.getByLabelText(/confirm password/i), password);
  return user;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("route guards", () => {
  it.each(["/", "/setup", "/login", "/dashboard"])(
    "routes an incomplete installation at %s to setup",
    async (path) => {
      server.use(setupStatus(false));
      await renderRoute(path);
      expect(await screen.findByRole("heading", { name: /create your administrator/i })).toBeVisible();
    },
  );

  it.each(["/", "/setup", "/login", "/dashboard"])(
    "routes an unauthenticated initialized installation at %s to login",
    async (path) => {
      server.use(setupStatus(true), unauthenticated());
      await renderRoute(path);
      expect(await screen.findByRole("heading", { name: /welcome back/i })).toBeVisible();
    },
  );

  it.each(["/", "/setup", "/login", "/dashboard"])(
    "routes an authenticated installation at %s to the dashboard",
    async (path) => {
      server.use(setupStatus(true), authenticated());
      await renderRoute(path);
      expect(await screen.findByRole("heading", { name: /nervos is ready/i })).toBeVisible();
      expect(screen.getByText("admin")).toBeVisible();
    },
  );

  it("shows an accessible loading state while bootstrap is pending", async () => {
    server.use(
      http.get("/api/v1/setup/status", async () => {
        await delay("infinite");
        return HttpResponse.json({ setup_complete: false });
      }),
    );
    await renderRoute("/");

    expect(screen.getByRole("status")).toHaveAccessibleName(/loading/i);
  });

  it("recovers from a bootstrap error using retry", async () => {
    let attempts = 0;
    server.use(
      http.get("/api/v1/setup/status", () => {
        attempts += 1;
        return attempts === 1
          ? error(503, "service_unavailable", "Authentication is temporarily unavailable.")
          : HttpResponse.json({ setup_complete: false });
      }),
    );
    const { user } = await renderRoute("/");

    expect(await screen.findByRole("alert")).toHaveTextContent(/temporarily unavailable/i);
    await user.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByRole("heading", { name: /create your administrator/i })).toBeVisible();
    expect(attempts).toBe(2);
  });

  it("renders not found without resolving authentication state", async () => {
    await renderRoute("/missing");

    expect(await screen.findByRole("heading", { name: /not found/i })).toBeVisible();
    expect(screen.getByRole("link", { name: /return to nervos/i })).toHaveAttribute(
      "href",
      "/",
    );
  });
});

describe("initial setup", () => {
  it("submits the password exactly and enters the dashboard", async () => {
    const password = "  exact password value  ";
    let body: unknown;
    server.use(
      setupStatus(false),
      http.post("/api/v1/setup", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(apiUser, { status: 201 });
      }),
      authenticated(),
    );
    await renderRoute("/setup");
    const user = await fillSetup(password);

    await user.click(screen.getByRole("button", { name: /create.*admin/i }));
    expect(await screen.findByRole("heading", { name: /nervos is ready/i })).toBeVisible();
    expect(body).toEqual({ username: "admin", password });
  });

  it("requires matching password confirmation without making a request", async () => {
    let requests = 0;
    server.use(
      setupStatus(false),
      http.post("/api/v1/setup", () => {
        requests += 1;
        return HttpResponse.json(apiUser, { status: 201 });
      }),
    );
    await renderRoute("/setup");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^username$/i), "admin");
    await user.type(screen.getByLabelText(/^password$/i), "correct horse battery");
    await user.type(screen.getByLabelText(/confirm password/i), "different password");
    await user.click(screen.getByRole("button", { name: /create.*admin/i }));

    expect(screen.getByRole("alert")).toHaveTextContent(/passwords.*match/i);
    expect(requests).toBe(0);
  });

  it("moves to login when another client completed setup first", async () => {
    let setupComplete = false;
    server.use(
      http.get("/api/v1/setup/status", () =>
        HttpResponse.json({ setup_complete: setupComplete }),
      ),
      http.post("/api/v1/setup", () => {
        setupComplete = true;
        return error(409, "setup_complete", "Initial setup is already complete.");
      }),
      unauthenticated(),
    );
    await renderRoute("/setup");
    const user = await fillSetup();
    await user.click(screen.getByRole("button", { name: /create.*admin/i }));

    expect(await screen.findByRole("heading", { name: /welcome back/i })).toBeVisible();
  });

  it.each([
    [503, "service_unavailable", "Authentication is temporarily unavailable."],
    [422, "invalid_password", "Password does not meet the required length."],
  ])("shows a safe setup error for HTTP %i", async (status, code, message) => {
    server.use(
      setupStatus(false),
      http.post("/api/v1/setup", () => error(status, code, message)),
    );
    await renderRoute("/setup");
    const user = await fillSetup();
    await user.click(screen.getByRole("button", { name: /create.*admin/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText(/^password$/i)).toHaveFocus();
  });
});

describe("login and session restoration", () => {
  it("uses a generic credentials error and preserves the username", async () => {
    server.use(
      setupStatus(true),
      unauthenticated(),
      http.post("/api/v1/auth/login", () =>
        error(401, "invalid_credentials", "Invalid username or password."),
      ),
    );
    await renderRoute("/login");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^username$/i), "admin");
    await user.type(screen.getByLabelText(/^password$/i), "wrong password");
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Invalid username or password.",
    );
    expect(screen.getByLabelText(/^username$/i)).toHaveValue("admin");
    expect(screen.getByLabelText(/^password$/i)).toHaveValue("");
  });

  it.each([
    [429, "too_many_attempts", "Too many authentication attempts."],
    [503, "service_unavailable", "Authentication is temporarily unavailable."],
  ])("renders the recoverable HTTP %i login error", async (status, code, message) => {
    server.use(
      setupStatus(true),
      unauthenticated(),
      http.post("/api/v1/auth/login", () => error(status, code, message)),
    );
    await renderRoute("/login");
    await screen.findByLabelText(/^username$/i);
    const user = await fillCredentials();
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("renders a safe retryable message for a network failure", async () => {
    server.use(
      setupStatus(true),
      unauthenticated(),
      http.post("/api/v1/auth/login", () => HttpResponse.error()),
    );
    await renderRoute("/login");
    await screen.findByLabelText(/^username$/i);
    const user = await fillCredentials();
    await user.click(screen.getByRole("button", { name: /sign in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/connect|network|try again/i);
  });

  it("restores a cookie session without reading authentication from browser storage", async () => {
    const storage = vi.spyOn(Storage.prototype, "getItem");
    server.use(setupStatus(true), authenticated());
    await renderRoute("/dashboard");

    expect(await screen.findByRole("heading", { name: /nervos is ready/i })).toBeVisible();
    expect(storage).not.toHaveBeenCalledWith(expect.stringMatching(/auth|session|user|token/i));
  });
});

describe("logout", () => {
  it("does not leave the dashboard until the server confirms logout", async () => {
    let release: (() => void) | undefined;
    const response = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      setupStatus(true),
      authenticated(),
      http.post("/api/v1/auth/logout", async () => {
        await response;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = await renderRoute("/dashboard");
    await screen.findByRole("heading", { name: /nervos is ready/i });
    await user.click(screen.getByRole("button", { name: /log out/i }));

    expect(screen.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /signing out/i })).toBeDisabled();
    release?.();
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: /welcome back/i })).toBeVisible();
    });
  });

  it("keeps the session UI on logout failure and permits retry", async () => {
    let attempts = 0;
    server.use(
      setupStatus(true),
      authenticated(),
      http.post("/api/v1/auth/logout", () => {
        attempts += 1;
        return attempts === 1
          ? error(503, "service_unavailable", "Authentication is temporarily unavailable.")
          : new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = await renderRoute("/dashboard");
    await screen.findByRole("heading", { name: /nervos is ready/i });
    await user.click(screen.getByRole("button", { name: /log out/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/temporarily unavailable/i);
    expect(screen.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();
    await user.click(screen.getByRole("button", { name: /log out/i }));
    expect(await screen.findByRole("heading", { name: /welcome back/i })).toBeVisible();
    await waitFor(() => expect(attempts).toBe(2));
  });
});
