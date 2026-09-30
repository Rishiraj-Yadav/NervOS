"""Operator-invoked, bounded Gemini Developer API proof; never part of automated checks."""

from __future__ import annotations

import argparse
import asyncio
import os

from nervos_core.application.model_completion import ModelProviderError, ModelRequest
from nervos_models.gemini import GeminiModelCompletion, create_gemini_client


async def prove(model_name: str, api_key: str) -> int:
    client = create_gemini_client(api_key, 15.0)
    try:
        request = ModelRequest(
            system_instruction="Answer briefly.",
            user_text="Reply with one short greeting.",
            model_name=model_name,
            max_output_tokens=64,
            timeout_ms=15_000,
        )
        result = await asyncio.wait_for(GeminiModelCompletion(client).complete(request), 16.0)
        print("GEMINI LIVE PROOF — PASS")
        finish = result.finish_reason.value if result.finish_reason is not None else "unknown"
        print(f"provider=gemini model={model_name} finish={finish}")
        if result.usage is not None:
            print(f"usage={result.usage.values()}")
        return 0
    except ModelProviderError as error:
        print(f"GEMINI LIVE PROOF — FAILED — {error.code}")
        return 1
    except TimeoutError:
        print("GEMINI LIVE PROOF — FAILED — model_timed_out")
        return 1
    except Exception:
        print("GEMINI LIVE PROOF — FAILED — model_unavailable")
        return 1
    finally:
        await client.aio.aclose()
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual Gemini Developer API proof")
    parser.add_argument("--model", help="Exact Gemini Developer API model ID")
    args = parser.parse_args()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI LIVE PROOF — NOT EXECUTED")
        return 0
    if not args.model:
        parser.error("--model is required when GEMINI_API_KEY is configured")
    return asyncio.run(prove(args.model, api_key))


if __name__ == "__main__":
    raise SystemExit(main())
