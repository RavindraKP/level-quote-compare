"""The one wrapper around the Anthropic SDK. Every model call in Level goes through here.

- Key: LEVEL_ANTHROPIC_API_KEY only. base_url is set explicitly, so the coding session's
  ANTHROPIC_* variables are never used. If the key is missing we stop; no fake output.
- Model: LEVEL_MODEL (default claude-haiku-5-5); images use LEVEL_VISION_MODEL (default: same).
- Every request streams (no HTTP timeouts on long documents) and is checked for
  stop_reason "refusal" and "max_tokens" before its content is used.
- `structured()` takes a Pydantic model, sends it as the output schema
  (output_config.format) and returns a validated instance.
- Tokens and latency are logged and returned, so callers can store them (extraction_run).
- `untrusted()` wraps vendor text so prompts can tell the model it is data, not instructions.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

log = logging.getLogger("level.llm")

API_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-haiku-5-5"
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LLMError(RuntimeError):
    """Base class for model call problems the caller should show, not hide."""


class BadAPIKey(LLMError):
    """The key is set but the API refused it (wrong, revoked or without access)."""


class MissingAPIKey(LLMError):
    pass


class RefusalError(LLMError):
    pass


class TruncatedError(LLMError):
    """The model hit max_tokens; the output is incomplete and must not be used."""


class InvalidOutputError(LLMError):
    """Structured output did not validate against the schema."""


@dataclass
class LLMResult:
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    request_id: str | None
    parsed: Any = None  # the Pydantic instance, for structured()
    message: Any = field(default=None, repr=False)  # the SDK Message, for tool use


def model_name(vision: bool = False) -> str:
    """The configured model id. Vision calls may use a different (stronger) model."""
    text_model = os.environ.get("LEVEL_MODEL") or DEFAULT_MODEL
    if vision:
        return os.environ.get("LEVEL_VISION_MODEL") or text_model
    return text_model


def get_client() -> anthropic.Anthropic:
    """Client for the app's own key. An explicit api_key means env vars are not consulted."""
    key = os.environ.get("LEVEL_ANTHROPIC_API_KEY")
    if not key:
        raise MissingAPIKey(
            "LEVEL_ANTHROPIC_API_KEY is not set. Add it to .env (local) or the environment's "
            "settings (cloud). Level does not fall back to any other key."
        )
    return anthropic.Anthropic(api_key=key, base_url=API_BASE_URL, max_retries=3)


def untrusted(label: str, text: str) -> str:
    """Wrap vendor-supplied text so the prompt can treat it as data (T12)."""
    safe = text.replace("</untrusted_document>", "</untrusted_document_>")  # cannot close early
    return f'<untrusted_document label="{label}">\n{safe}\n</untrusted_document>'


def _system_blocks(system: str | None, cache: bool) -> list[dict] | None:
    """System prompt as a block list; the cache mark lets repeated calls reuse the prefix."""
    if not system:
        return None
    block: dict = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def complete(
    messages: list[dict],
    *,
    system: str | None = None,
    vision: bool = False,
    model: str | None = None,
    max_tokens: int = 16000,
    effort: Effort = "medium",
    tools: list[dict] | None = None,
    output_config: dict | None = None,
    cache_system: bool = True,
    on_text: Callable[[str], None] | None = None,
) -> LLMResult:
    """One streamed request. Raises RefusalError / TruncatedError instead of returning junk.

    on_text, if given, receives text deltas as they arrive (for chat UIs).
    """
    client = get_client()
    model = model or model_name(vision)
    config = {"effort": effort, **(output_config or {})}
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "output_config": config,
    }
    system_blocks = _system_blocks(system, cache_system)
    if system_blocks:
        kwargs["system"] = system_blocks
    if tools:
        kwargs["tools"] = tools

    started = time.monotonic()
    try:
        with client.messages.stream(**kwargs) as stream:
            if on_text:
                for delta in stream.text_stream:
                    on_text(delta)
            msg = stream.get_final_message()
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        # A wrong or revoked key: one clear message, never a traceback on screen (H7).
        raise BadAPIKey("LEVEL_ANTHROPIC_API_KEY was refused by the API. Check the key.") from exc
    except anthropic.APIError as exc:  # outage, overload after retries, bad request
        raise LLMError(f"The model service could not complete the request: {exc}") from exc
    latency_ms = int((time.monotonic() - started) * 1000)

    usage = msg.usage
    result = LLMResult(
        text="".join(b.text for b in msg.content if b.type == "text"),
        model=msg.model,
        stop_reason=msg.stop_reason,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
        latency_ms=latency_ms,
        request_id=getattr(msg, "_request_id", None),
        message=msg,
    )
    log.info(
        "llm model=%s stop=%s in=%d out=%d cache_read=%d latency_ms=%d",
        result.model, result.stop_reason, result.input_tokens, result.output_tokens,
        result.cache_read_tokens, result.latency_ms,
    )

    if msg.stop_reason == "refusal":
        details = getattr(msg, "stop_details", None)
        raise RefusalError(f"The model declined this request ({details}).")
    if msg.stop_reason == "max_tokens":
        raise TruncatedError(
            f"Output hit max_tokens={max_tokens}; the result is incomplete and was not used."
        )
    return result


def structured[T: BaseModel](
    output_model: type[T],
    messages: list[dict],
    *,
    system: str | None = None,
    vision: bool = False,
    model: str | None = None,
    max_tokens: int = 16000,
    effort: Effort = "medium",
    cache_system: bool = True,
) -> LLMResult:
    """Ask for JSON matching `output_model`; return LLMResult with `.parsed` set.

    The schema is converted with the SDK's transform_schema (the form structured outputs
    accept). We validate the text ourselves so a failure is a clear InvalidOutputError.
    """
    schema = anthropic.transform_schema(output_model.model_json_schema())
    result = complete(
        messages,
        system=system,
        vision=vision,
        model=model,
        max_tokens=max_tokens,
        effort=effort,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        cache_system=cache_system,
    )
    try:
        result.parsed = output_model.model_validate(json.loads(result.text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise InvalidOutputError(
            f"Model output did not match {output_model.__name__}: {exc}"
        ) from exc
    return result
