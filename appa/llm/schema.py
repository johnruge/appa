"""Turn a pydantic model into a JSON schema that strict structured-output modes accept:
every property required, no extra properties, no defaults."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema made strict (all fields required, no extras, no defaults), $defs included."""
    schema = model.model_json_schema()
    _tighten(schema)
    for sub in schema.get("$defs", {}).values():
        _tighten(sub)
    return schema


def _tighten(node: Any) -> None:
    """Recursively drop defaults and make every object require all its properties and forbid others."""
    if isinstance(node, dict):
        node.pop("default", None)
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        for key, value in node.items():
            if key != "$defs":
                _tighten(value)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)
