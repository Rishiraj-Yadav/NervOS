import { screen } from "@testing-library/react";
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
  conversations?: unknown[];
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
    http.get("/api/v1/conversations", () =>
      HttpResponse.json({
        items: extra?.conversations ?? [],
        next_before_id: null,
      })
    ),
  ];
}

describe("conversations page", () => {
  it("shows an explicit empty state when no conversations exist", async () => {
    server.use(...signedIn());

    await renderRoute("/conversations");

    expect(await screen.findByText(/no conversations yet/i)).toBeVisible();
    expect(
      screen.getByRole("button", { name: /new conversation/i })
    ).toBeVisible();
  });

  it("lists existing conversations", async () => {
    const conv = {
      id: 1,
      owner_user_id: 1,
      agent_instance_id: 1,
      title: "Project Discussion",
      created_at: "2026-09-21T12:00:00Z",
      updated_at: "2026-09-21T12:00:00Z",
    };
    server.use(...signedIn({ conversations: [conv] }));

    await renderRoute("/conversations");

    const link = await screen.findByRole("link", {
      name: /project discussion/i,
    });
    expect(link).toHaveAttribute("href", "/conversations/1");
  });
});
