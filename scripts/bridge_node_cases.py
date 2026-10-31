"""Node SDK smoke cases for scripts/bridge_sdk_smoke.py (ADR 0044/0045).

Each constant is a self-contained ESM script written into the throwaway Node
directory and executed with `node <script>.mjs`. The children read the same
environment variables the production host sets and use each SDK with default
settings; every request stays on 127.0.0.1 against the shim.
"""

_NODE_COMMON = r"""
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

function _pkgVersion(name) {
  const here = dirname(fileURLToPath(import.meta.url));
  const pkg = JSON.parse(readFileSync(join(here, "node_modules", name, "package.json"), "utf8"));
  return pkg.version;
}

function fail(sdk, name, error) {
  const status = error?.status ?? error?.response?.status ?? "";
  const body =
    error?.error?.message ??
    error?.response?.data?.error?.message ??
    error?.message ??
    "";
  const line = `RESULT ${sdk}|${name}|FAIL|${error?.constructor?.name}|${status}`;
  console.log(`${line}|${String(body).slice(0, 200)}`);
}

function report(sdk, name, fn) {
  try {
    const outcome = fn();
    if (outcome && typeof outcome.then === "function") {
      return outcome.then(
        () => console.log(`RESULT ${sdk}|${name}|PASS`),
        (error) => fail(sdk, name, error),
      );
    }
    console.log(`RESULT ${sdk}|${name}|PASS`);
    return undefined;
  } catch (error) {
    return fail(sdk, name, error);
  }
}
"""

NODE_OPENAI_CASE = (
    r"""
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
console.log(`VERSION openai|${_pkgVersion("openai")}`);
import OpenAI from "openai";

const client = new OpenAI(); // OPENAI_BASE_URL / OPENAI_API_KEY from the environment

"""
    + _NODE_COMMON
    + r"""

const tools = [
  {
    type: "function",
    function: {
      name: "echo",
      description: "Echo tool",
      parameters: { type: "object", properties: { value: { type: "integer" } } },
    },
  },
];

async function chatPlain() {
  const completion = await client.chat.completions.create({
    model: "smoke-model",
    messages: [{ role: "user", content: "hi" }],
  });
  if (completion.choices[0].message.content !== "SMOKE_OK") {
    throw new Error(`unexpected content: ${completion.choices[0].message.content}`);
  }
}

async function chatStream() {
  const chunks = [];
  const stream = await client.chat.completions.create({
    model: "smoke-model",
    messages: [{ role: "user", content: "hi" }],
    stream: true,
  });
  for await (const chunk of stream) {
    const text = chunk.choices?.[0]?.delta?.content;
    if (text) chunks.push(text);
  }
  if (chunks.join("") !== "SMOKE_OK") throw new Error(`stream gave ${chunks.join("")}`);
}

async function chatTool() {
  const first = await client.chat.completions.create({
    model: "smoke-model",
    messages: [{ role: "user", content: "run the echo tool" }],
    tools,
  });
  const call = first.choices[0].message.tool_calls?.[0];
  if (!call || call.function.name !== "echo") throw new Error("no echo tool call");
  if (JSON.parse(call.function.arguments).value !== 7) throw new Error("bad arguments");
  const final = await client.chat.completions.create({
    model: "smoke-model",
    messages: [
      { role: "user", content: "run the echo tool" },
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: call.id,
            type: "function",
            function: { name: call.function.name, arguments: call.function.arguments },
          },
        ],
      },
      { role: "tool", tool_call_id: call.id, content: "TOOL_RESULT_OK" },
    ],
    tools,
  });
  if (final.choices[0].message.content !== "TOOL_FINAL_OK") {
    throw new Error(`final was ${final.choices[0].message.content}`);
  }
}

async function responsesPlain() {
  const response = await client.responses.create({ model: "smoke-model", input: "hi" });
  if (response.output_text !== "SMOKE_OK") throw new Error(`output_text ${response.output_text}`);
}

async function responsesStream() {
  const chunks = [];
  const stream = await client.responses.create({
    model: "smoke-model",
    input: "hi",
    stream: true,
  });
  for await (const event of stream) {
    if (event.type === "response.output_text.delta") chunks.push(event.delta);
  }
  if (chunks.join("") !== "SMOKE_OK") throw new Error(`stream gave ${chunks.join("")}`);
}

await report("openai", "chat_plain", chatPlain);
await report("openai", "chat_stream", chatStream);
await report("openai", "chat_tool", chatTool);
await report("openai", "responses_plain", responsesPlain);
await report("openai", "responses_stream", responsesStream);
"""
)

NODE_ANTHROPIC_CASE = (
    r"""
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
console.log(`VERSION anthropic|${_pkgVersion("@anthropic-ai/sdk")}`);
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic(); // ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY from the environment

"""
    + _NODE_COMMON
    + r"""

const tools = [
  {
    name: "echo",
    description: "Echo tool",
    input_schema: { type: "object", properties: { value: { type: "integer" } } },
  },
];

async function messagesPlain() {
  const message = await client.messages.create({
    model: "claude-smoke",
    max_tokens: 64,
    messages: [{ role: "user", content: "hi" }],
  });
  if (message.content[0].text !== "SMOKE_OK") throw new Error(`text ${message.content[0].text}`);
}

async function messagesStream() {
  const chunks = [];
  const stream = client.messages.stream({
    model: "claude-smoke",
    max_tokens: 64,
    messages: [{ role: "user", content: "hi" }],
  });
  stream.on("text", (delta) => chunks.push(delta));
  await stream.finalMessage();
  if (chunks.join("") !== "SMOKE_OK") throw new Error(`stream gave ${chunks.join("")}`);
}

async function messagesTool() {
  const first = await client.messages.create({
    model: "claude-smoke",
    max_tokens: 64,
    messages: [{ role: "user", content: "run the echo tool" }],
    tools,
  });
  const block = first.content.find((item) => item.type === "tool_use");
  if (!block || block.name !== "echo") throw new Error("no echo tool_use");
  const final = await client.messages.create({
    model: "claude-smoke",
    max_tokens: 64,
    messages: [
      { role: "user", content: "run the echo tool" },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: block.id, name: "echo", input: block.input }],
      },
      {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: block.id, content: "TOOL_RESULT_OK" }],
      },
    ],
    tools,
  });
  if (final.content[0].text !== "TOOL_FINAL_OK") {
    throw new Error(`final was ${final.content[0].text}`);
  }
}

await report("anthropic", "messages_plain", messagesPlain);
await report("anthropic", "messages_stream", messagesStream);
await report("anthropic", "messages_tool", messagesTool);
"""
)

NODE_GEMINI_CASE = (
    r"""
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
console.log(`VERSION google-genai|${_pkgVersion("@google/genai")}`);
import { GoogleGenAI } from "@google/genai";

const client = new GoogleGenAI({
  apiKey: process.env.GEMINI_API_KEY,
  httpOptions: { baseUrl: process.env.GOOGLE_GEMINI_BASE_URL },
});

"""
    + _NODE_COMMON
    + r"""

const tool = {
  functionDeclarations: [
    {
      name: "echo",
      description: "Echo tool",
      parameters: { type: "object", properties: { value: { type: "integer" } } },
    },
  ],
};

async function generatePlain() {
  const response = await client.models.generateContent({
    model: "gemini-smoke",
    contents: "hi",
  });
  if (response.text !== "SMOKE_OK") throw new Error(`text ${response.text}`);
}

async function generateStream() {
  const chunks = [];
  const stream = await client.models.generateContentStream({
    model: "gemini-smoke",
    contents: "hi",
  });
  for await (const chunk of stream) {
    if (chunk.text) chunks.push(chunk.text);
  }
  if (chunks.join("") !== "SMOKE_OK") throw new Error(`stream gave ${chunks.join("")}`);
}

async function generateTool() {
  const first = await client.models.generateContent({
    model: "gemini-smoke",
    contents: "run the echo tool",
    config: { tools: [tool] },
  });
  const call = first.candidates[0].content.parts.find((part) => part.functionCall);
  if (!call || call.functionCall.name !== "echo") throw new Error("no echo functionCall");
  const final = await client.models.generateContent({
    model: "gemini-smoke",
    contents: [
      { role: "user", parts: [{ text: "run the echo tool" }] },
      { role: "model", parts: [{ functionCall: call.functionCall }] },
      {
        role: "user",
        parts: [{ functionResponse: { name: "echo", response: { output: "TOOL_RESULT_OK" } } }],
      },
    ],
    config: { tools: [tool] },
  });
  if (final.text !== "TOOL_FINAL_OK") throw new Error(`final was ${final.text}`);
}

await report("google-genai", "generate_plain", generatePlain);
await report("google-genai", "generate_stream", generateStream);
await report("google-genai", "generate_tool", generateTool);
"""
)

NODE_VERCEL_CASE = (
    r"""
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
console.log(`VERSION vercel-ai|${_pkgVersion("ai")}`);
import { generateText, streamText, tool, stepCountIs } from "ai";
import { createOpenAI } from "@ai-sdk/openai";
import { z } from "zod";

const provider = createOpenAI(); // OPENAI_BASE_URL / OPENAI_API_KEY from the environment
const model = provider("smoke-model");

"""
    + _NODE_COMMON
    + r"""

async function generatePlain() {
  const result = await generateText({ model, prompt: "hi" });
  if (result.text !== "SMOKE_OK") throw new Error(`text ${result.text}`);
}

async function generateStream() {
  const chunks = [];
  const result = streamText({ model, prompt: "hi" });
  for await (const delta of result.textStream) chunks.push(delta);
  if (chunks.join("") !== "SMOKE_OK") throw new Error(`stream gave ${chunks.join("")}`);
}

async function generateTool() {
  const echo = tool({
    description: "Echo tool",
    inputSchema: z.object({ value: z.number() }),
    execute: async ({ value }) => ({ echoed: value }),
  });
  const result = await generateText({
    model,
    prompt: "run the echo tool",
    tools: { echo },
    stopWhen: stepCountIs(3),
  });
  const call = result.toolCalls.find((item) => item.toolName === "echo");
  if (!call) throw new Error("no echo tool call");
  await result.finishReason;
}

async function generateStructured() {
  // Structured output via a forced tool choice.
  const result = await generateText({
    model,
    prompt: "reply as json",
    tools: {
      reply: tool({
        description: "Reply",
        inputSchema: z.object({ answer: z.string() }),
      }),
    },
    toolChoice: { type: "tool", toolName: "reply" },
  });
  const call = result.toolCalls.find((item) => item.toolName === "reply");
  if (!call) throw new Error("no forced reply call");
}

await report("vercel-ai", "generate_plain", generatePlain);
await report("vercel-ai", "generate_stream", generateStream);
await report("vercel-ai", "generate_tool", generateTool);
await report("vercel-ai", "generate_structured", generateStructured);
"""
)
