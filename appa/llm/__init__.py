"""Model layer: one interface for every model appa can use.

    from appa import llm
    result = llm.complete("screener", messages, schema=Extraction)        # role default
    result = llm.complete("screener", messages, schema=Extraction, model="qwen/qwen3.6-35b-a3b")

Every call returns a Completion with the validated output, token counts, and real cost.
"""

from appa.llm.base import (
    Completion,
    LLMError,
    ModelNotAllowed,
    ProviderError,
    Refused,
    SchemaFailed,
)
from appa.llm.registry import Registry, complete, get_registry

__all__ = [
    "Completion",
    "LLMError",
    "ModelNotAllowed",
    "ProviderError",
    "Refused",
    "SchemaFailed",
    "Registry",
    "complete",
    "get_registry",
]
