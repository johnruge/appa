from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel


@dataclass
class Completion:
    model: str  # what we asked for
    served_by: str = ""  # model/provider that actually answered (OpenRouter reports it)
    text: str = ""
    parsed: BaseModel | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # summed over schema retries
    latency_s: float = 0.0
    attempts: int = 1
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class Model(Protocol):
    name: str

    def complete(
        self, messages: list[dict], *, schema: type[BaseModel] | None = None, max_output_tokens: int | None = None
    ) -> Completion:
        """Send the chat messages and return the completion, parsed into the schema if one is given."""
        ...


class LLMError(RuntimeError):
    """Base for everything the model layer raises. Callers catch this, not SDK errors."""

    def __init__(self, message: str, *, cost_usd: float = 0.0):
        """An LLM error carrying the money already spent before it failed."""
        super().__init__(message)
        self.cost_usd = cost_usd  # money already spent before failing


class ModelNotAllowed(LLMError):
    pass


class Refused(LLMError):
    pass


class SchemaFailed(LLMError):
    pass


class ProviderError(LLMError):
    """Network, rate limit, or provider failure after transport retries."""
