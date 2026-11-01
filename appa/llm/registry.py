"""Model allowlist and per-role defaults, from config/models.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel

from appa.config import ROOT, load_env
from appa.llm.base import Completion, LLMError, ModelNotAllowed
from appa.llm.openrouter import OpenRouterModel

MODELS_PATH = ROOT / "config" / "models.yaml"


@dataclass(frozen=True)
class ModelInfo:
    name: str
    vision: bool
    price_in: float  # USD per million tokens (as listed when added)
    price_out: float


class Registry:
    def __init__(self, path: Path = MODELS_PATH, *, http_client: httpx.Client | None = None):
        """Load allowed models, defaults, provider and limits; every default must be in the allowed list."""
        cfg = yaml.safe_load(path.read_text())
        self.defaults: dict[str, str] = cfg.get("defaults", {})
        self.models: dict[str, ModelInfo] = {
            name: ModelInfo(name, bool(v.get("vision")), *map(float, v.get("price", [0, 0])))
            for name, v in (cfg.get("allowed") or {}).items()
        }
        self.provider: dict = cfg.get("provider", {})
        self.limits: dict = cfg.get("limits", {})
        self.http_client = http_client  # tests inject a fake transport here
        for role, name in self.defaults.items():
            if name not in self.models:
                raise LLMError(f"default model for {role!r} ({name}) isn't in the allowed list")

    def resolve(self, role: str, model: str | None = None, *, vision: bool = False) -> ModelInfo:
        """The allowed model for this role (given or default); ModelNotAllowed if unknown or lacking vision."""
        name = model or self.defaults.get(role)
        if not name:
            raise ModelNotAllowed(f"no model given and no default for role {role!r}")
        info = self.models.get(name)
        if not info:
            raise ModelNotAllowed(f"{name!r} isn't in config/models.yaml; allowed: {', '.join(self.models)}")
        if vision and not info.vision:
            raise ModelNotAllowed(f"{name} can't read images; pick a model with vision: true")
        return info

    def get(self, role: str, model: str | None = None, *, vision: bool = False) -> OpenRouterModel:
        """A ready client for the resolved model; LLMError if OPENROUTER_API_KEY is not set."""
        info = self.resolve(role, model, vision=vision)
        load_env()
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise LLMError("OPENROUTER_API_KEY is not set (add it to .env or the environment)")
        return OpenRouterModel(
            info.name,
            api_key=key,
            provider=self.provider,
            max_output_tokens=int(self.limits.get("max_output_tokens", 4000)),
            timeout_s=float(self.limits.get("timeout_seconds", 60)),
            max_retries=int(self.limits.get("max_retries", 2)),
            http_client=self.http_client,
        )


@lru_cache(maxsize=1)
def get_registry() -> Registry:
    """The model registry from config/models.yaml, loaded once per process (cached)."""
    return Registry()


def complete(
    role: str,
    messages: list[dict],
    *,
    schema: type[BaseModel] | None = None,
    model: str | None = None,
    vision: bool = False,
    max_output_tokens: int | None = None,
) -> Completion:
    """Run a completion with the role's default model, or the given allowed one."""
    return (
        get_registry()
        .get(role, model, vision=vision)
        .complete(messages, schema=schema, max_output_tokens=max_output_tokens)
    )
