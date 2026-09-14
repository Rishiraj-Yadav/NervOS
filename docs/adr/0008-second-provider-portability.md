# ADR 0008: Second-provider portability

## Status

Accepted for Stage B4.

## Context

Anthropic proved the one-shot model boundary, but one adapter could not prove portability.

## Decision

Add OpenAI (`openai`) through the official async Python SDK and Responses API behind the unchanged `ModelCompletion` port. Production knows exactly `anthropic` and `openai`. Credentials are optional process environment values (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). SDK retries and NervOS fallback are disabled.

OpenAI requests are stateless, non-streaming, non-background, and use `store=False`. The adapter traverses typed output directly, excludes reasoning, treats explicit refusal as refusal, and rejects tools and unknown output. Only provider-reported usage is retained; Anthropic totals remain unknown while OpenAI authoritative totals may be retained.

## Consequences

The same trusted Chat handler, coordinator, API, persistence, and dashboard support both providers. No schema, route, provider-status endpoint, model discovery, conversation, tool execution, retry, or fallback is added. Real cloud execution sends the current instruction and prompt outside the local machine and remains operator-triggered.

## Rejected alternatives

Gemini had a less precise error boundary and more transport/tool complexity for this milestone. Dynamic discovery, provider fallback, a plugin framework, Chat Completions, convenience output aggregation, derived totals, and schema changes were rejected as unnecessary or unsafe.
