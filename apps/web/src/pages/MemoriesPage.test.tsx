import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  apiAgentInstance,
  authenticatedHandler,
  renderRoute,
  setupStatusHandler,
} from "../test/agentFixtures";
import { server } from "../test/server";

function signedIn(extra?: {
  memories?: unknown[];
  agentInstances?: unknown[];
}) {
  return [
    setupStatusHandler(true),
    authenticatedHandler(),
    http.get("/api/v1/agent-instances", () =>
      HttpResponse.json({
        items: extra?.agentInstances ?? [apiAgentInstance()],
        next_before_id: null,
      })
    ),
    http.get("/api/v1/memories", () =>
      HttpResponse.json({
        items: extra?.memories ?? [],
        next_before_id: null,
      })
    ),
  ];
}

describe("memories page", () => {
  it("shows empty state when no memories exist", async () => {
    server.use(...signedIn());

    await renderRoute("/memories");

    expect(await screen.findByText(/no active memory items found/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /add memory/i })).toBeVisible();
  });

  it("lists existing memories with edit and delete actions", async () => {
    const mem = {
      id: 1,
      owner_user_id: 1,
      agent_instance_id: null,
      scope: "user",
      status: "active",
      current_version: 1,
      content: "User prefers Python and SQLite.",
      source_kind: "direct_user",
      source_id: null,
      provenance_type: "user_authored",
      created_at: "2026-09-22T12:00:00Z",
      updated_at: "2026-09-22T12:00:00Z",
    };
    server.use(...signedIn({ memories: [mem] }));

    await renderRoute("/memories");

    expect(await screen.findByText(/User prefers Python and SQLite\./i)).toBeVisible();
    expect(screen.getByRole("cell", { name: "USER" })).toBeVisible();
    expect(screen.getByText(/v1/)).toBeVisible();
    expect(screen.getByRole("button", { name: /edit/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /delete/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /history/i })).toBeVisible();
  });

  it("opens edit modal and allows editing", async () => {
    const mem = {
      id: 1,
      owner_user_id: 1,
      agent_instance_id: null,
      scope: "user",
      status: "active",
      current_version: 1,
      content: "User prefers Python.",
      source_kind: "direct_user",
      source_id: null,
      provenance_type: "user_authored",
      created_at: "2026-09-22T12:00:00Z",
      updated_at: "2026-09-22T12:00:00Z",
    };
    server.use(
      ...signedIn({ memories: [mem] }),
      http.patch("/api/v1/memories/1", () =>
        HttpResponse.json({
          ...mem,
          current_version: 2,
          content: "User prefers Rust.",
          updated_at: "2026-09-23T12:00:00Z",
        })
      )
    );

    const user = userEvent.setup();
    await renderRoute("/memories");

    const editBtn = await screen.findByRole("button", { name: /edit/i });
    await user.click(editBtn);

    expect(screen.getByText(/Edit memory fact \(v1\)/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /save update/i })).toBeVisible();
  });
});
