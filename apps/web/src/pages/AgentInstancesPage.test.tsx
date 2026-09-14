import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  apiAgentInstance,
  apiError,
  authenticatedHandler,
  renderRoute,
  setupStatusHandler,
} from "../test/agentFixtures";
import { server } from "../test/server";

function signedIn(extra?: { items?: unknown[] }) {
  return [
    setupStatusHandler(true),
    authenticatedHandler(),
    http.get("/api/v1/agent-instances", () =>
      HttpResponse.json({ items: extra?.items ?? [], next_before_id: null }),
    ),
  ];
}

describe("agent list page", () => {
  it("shows an explicit empty state and never creates an agent implicitly", async () => {
    server.use(...signedIn());

    await renderRoute("/agents");

    expect(await screen.findByRole("heading", { name: /no agents yet/i })).toBeVisible();
    expect(screen.getByRole("button", { name: /create a chat agent/i })).toBeVisible();
    expect(screen.getByText(/nothing is created for you automatically/i)).toBeVisible();
  });

  it("lists the owned agent instances with their trusted definition", async () => {
    server.use(...signedIn({ items: [apiAgentInstance({ display_name: "Research" })] }));

    await renderRoute("/agents");

    const link = await screen.findByRole("link", { name: /research/i });
    expect(link).toHaveAttribute("href", "/agents/1");
    expect(link).toHaveTextContent("nervos.chat v1");
    expect(link).toHaveTextContent("Enabled");
  });

  it("creates an agent with the exact definition and no owner field", async () => {
    let body: unknown;
    server.use(
      ...signedIn(),
      http.post("/api/v1/agent-instances", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(apiAgentInstance(), { status: 201 });
      }),
      http.get("/api/v1/agent-instances/1", () => HttpResponse.json(apiAgentInstance())),
      http.get("/api/v1/agent-instances/1/runs", () =>
        HttpResponse.json({ items: [], next_before_id: null }),
      ),
    );
    const { user } = await renderRoute("/agents");
    await user.click(await screen.findByRole("button", { name: /create a chat agent/i }));

    await user.type(screen.getByLabelText("Display name"), "Chat");
    await user.type(screen.getByLabelText("Model"), "opaque/model");
    await user.click(screen.getByRole("button", { name: /^create agent$/i }));

    expect(await screen.findByRole("heading", { name: "Chat" })).toBeVisible();
    expect(body).toEqual({
      agent_key: "nervos.chat",
      agent_definition_version: "1",
      display_name: "Chat",
      model_provider: "anthropic",
      model_name: "opaque/model",
    });
    expect(JSON.stringify(body)).not.toContain("owner_user_id");
  });

  it("shows the safe server error when creation is rejected", async () => {
    server.use(
      ...signedIn(),
      http.post("/api/v1/agent-instances", () =>
        apiError(422, "invalid_agent_instance", "The agent instance configuration is invalid."),
      ),
    );
    const { user } = await renderRoute("/agents");
    await user.click(await screen.findByRole("button", { name: /create a chat agent/i }));

    await user.type(screen.getByLabelText("Display name"), "Chat");
    await user.type(screen.getByLabelText("Model"), "opaque/model");
    await user.click(screen.getByRole("button", { name: /^create agent$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The agent instance configuration is invalid.",
    );
  });

  it("tells the user that the model is not verified until it runs", async () => {
    server.use(...signedIn());
    const { user } = await renderRoute("/agents");
    await user.click(await screen.findByRole("button", { name: /create a chat agent/i }));

    expect(screen.getByText(/does not check whether it exists until you run the agent/i)).toBeVisible();
    expect(screen.getByLabelText("Model provider")).toHaveValue("anthropic");
    expect(screen.getByLabelText("Agent definition")).toHaveValue("nervos.chat v1");
  });

  it("offers exactly the two supported providers and no discovery request", async () => {
    let providerListCalls = 0;
    server.use(
      ...signedIn(),
      http.get("/api/v1/model-providers", () => {
        providerListCalls += 1;
        return HttpResponse.json({ items: [] });
      }),
    );
    const { user } = await renderRoute("/agents");
    await user.click(await screen.findByRole("button", { name: /create a chat agent/i }));

    const options = Array.from(
      screen.getByLabelText("Model provider").querySelectorAll("option"),
      (option) => ({ value: option.value, label: option.textContent }),
    );

    expect(options).toEqual([
      { value: "anthropic", label: "Anthropic" },
      { value: "openai", label: "OpenAI" },
    ]);
    expect(providerListCalls).toBe(0);
  });

  it("creates an instance with the explicitly selected second provider", async () => {
    let body: unknown = null;
    server.use(
      ...signedIn(),
      http.post("/api/v1/agent-instances", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(
          apiAgentInstance({ display_name: "Chat", model_provider: "openai" }),
          { status: 201 },
        );
      }),
      http.get("/api/v1/agent-instances/1", () =>
        HttpResponse.json(apiAgentInstance({ display_name: "Chat", model_provider: "openai" })),
      ),
      http.get("/api/v1/agent-instances/1/runs", () =>
        HttpResponse.json({ items: [], next_before_id: null }),
      ),
    );
    const { user } = await renderRoute("/agents");
    await user.click(await screen.findByRole("button", { name: /create a chat agent/i }));

    await user.type(screen.getByLabelText("Display name"), "Chat");
    await user.selectOptions(screen.getByLabelText("Model provider"), "openai");
    await user.type(screen.getByLabelText("Model"), "opaque/second-model");
    await user.click(screen.getByRole("button", { name: /^create agent$/i }));

    await screen.findByRole("heading", { name: "Chat" });
    expect(body).toMatchObject({
      display_name: "Chat",
      model_provider: "openai",
      model_name: "opaque/second-model",
    });
  });

  it("surfaces a load failure with a retry affordance", async () => {
    server.use(
      setupStatusHandler(true),
      authenticatedHandler(),
      http.get("/api/v1/agent-instances", () =>
        apiError(503, "service_unavailable", "NervOS storage is temporarily unavailable."),
      ),
    );

    await renderRoute("/agents");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "NervOS storage is temporarily unavailable.",
    );
    expect(screen.getByRole("button", { name: /try again/i })).toBeVisible();
  });

  it("never renders a provider credential or a stored answer on the list page", async () => {
    server.use(...signedIn({ items: [apiAgentInstance()] }));

    const { container } = await renderRoute("/agents");
    await screen.findByRole("link", { name: /chat/i });

    expect(container.textContent ?? "").not.toMatch(/credential/i);
  });
});
