"""OpenRouter adapter (OpenAI-compatible chat completions).

The same adapter works for any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM) by
changing base_url; v1 only configures OpenRouter.
"""

from __future__ import annotations

import json
import time

import httpx
import openai
from pydantic import BaseModel, ValidationError

from appa.llm.base import Completion, ProviderError, Refused, SchemaFailed
from appa.llm.schema import strict_schema

BASE_URL = "https://openrouter.ai/api/v1"
APP_HEADERS = {"X-Title": "appa"}  # attribution on OpenRouter; no personal data
SCHEMA_RETRIES = 1  # one corrective retry when output doesn't match the schema


class OpenRouterModel:
    def __init__(
        self,
        name: str,
        *,
        api_key: str,
        provider: dict,
        max_output_tokens: int,
        timeout_s: float,
        max_retries: int,
        http_client: httpx.Client | None = None,
    ):
        """An OpenRouter chat client for one model, with its provider routing and limits."""
        self.name = name
        self.provider = provider
        self.max_output_tokens = max_output_tokens
        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            default_headers=APP_HEADERS,
            timeout=timeout_s,
            max_retries=max_retries,
            http_client=http_client,
        )

    def complete(
        self, messages: list[dict], *, schema: type[BaseModel] | None = None, max_output_tokens: int | None = None
    ) -> Completion:
        """Call the model; with a schema, retry on invalid JSON, raising Refused or SchemaFailed if it never fits."""
        out = Completion(model=self.name, attempts=0)
        convo = list(messages)
        started = time.monotonic()
        for attempt in range(SCHEMA_RETRIES + 1):
            out.attempts = attempt + 1
            resp = self._call(convo, schema, max_output_tokens, out)
            choice = resp["choices"][0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            out.text = text
            if msg.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise Refused(f"{self.name} declined: {msg.get('refusal') or 'content filter'}", cost_usd=out.cost_usd)
            if schema is None:
                break
            if choice.get("finish_reason") == "length":
                raise SchemaFailed(f"{self.name} hit the output limit before finishing", cost_usd=out.cost_usd)
            try:
                out.parsed = schema.model_validate_json(_strip_fences(text))
                break
            except (ValidationError, ValueError) as e:
                if attempt == SCHEMA_RETRIES:
                    raise SchemaFailed(
                        f"{self.name} output didn't match the schema: {_short(e)}", cost_usd=out.cost_usd
                    ) from e
                convo += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": "That reply did not match the required JSON schema "
                        f"({_short(e)}). Reply again with only the JSON object.",
                    },
                ]
        out.latency_s = round(time.monotonic() - started, 3)
        return out

    def _call(self, messages, schema, max_output_tokens, out: Completion) -> dict:
        """One chat completion request; maps API errors to ProviderError and adds tokens and cost to `out`."""
        kwargs: dict = {
            "model": self.name,
            "messages": messages,
            "max_tokens": max_output_tokens or self.max_output_tokens,
            "extra_body": {"provider": self.provider},
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": True, "schema": strict_schema(schema)},
            }
        try:
            resp = self.client.chat.completions.create(**kwargs).model_dump()
        except openai.RateLimitError as e:
            raise ProviderError(f"rate limited by OpenRouter ({self.name})", cost_usd=out.cost_usd) from e
        except openai.AuthenticationError as e:
            raise ProviderError("OpenRouter rejected the API key (check OPENROUTER_API_KEY)") from e
        except openai.APIStatusError as e:
            raise ProviderError(
                f"OpenRouter error {e.status_code} for {self.name}: {_api_message(e)}", cost_usd=out.cost_usd
            ) from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise ProviderError(f"could not reach OpenRouter: {e.__class__.__name__}", cost_usd=out.cost_usd) from e
        usage = resp.get("usage") or {}
        out.input_tokens += int(usage.get("prompt_tokens") or 0)
        out.output_tokens += int(usage.get("completion_tokens") or 0)
        out.cost_usd += float(usage.get("cost") or 0.0)
        out.served_by = "/".join(x for x in (resp.get("model"), resp.get("provider")) if x)
        out.raw = resp
        return resp


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in ```json fences even in JSON mode."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _short(e: Exception) -> str:
    """A short error summary: the first three validation errors, or the message cut to 200 chars."""
    if isinstance(e, ValidationError):
        errs = e.errors()[:3]
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in errs)
    return str(e)[:200]


def _api_message(e: openai.APIStatusError) -> str:
    """The API error body (or message) as a string of at most 200 chars."""
    try:
        return json.dumps(e.body)[:200] if e.body else e.message[:200]
    except (TypeError, ValueError):
        return "unknown error"
