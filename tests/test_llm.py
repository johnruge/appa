"""Model layer and eval, against a fake OpenRouter (no network, no API key, no cost)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import appa.llm.registry as reg_mod
from appa import evals, extract, llm
from appa.core.models import Extraction
from appa.llm.schema import strict_schema

GOOD = {
    "company": "Acme",
    "title": "Software Engineer",
    "location": "NYC",
    "is_staffing_agency": False,
    "sponsorship": "not_mentioned",
    "sponsorship_quote": "",
    "citizenship_required": False,
    "citizenship_quote": "",
    "clearance_required": False,
    "clearance_quote": "",
    "min_years_experience": None,
    "experience_quote": "",
    "is_entry_level": True,
    "seniority": "new_grad",
    "has_essay_questions": False,
    "suspicious_instructions": [],
}

MODELS_YAML = """
defaults: {screener: test/cheap, screenshot: test/vision}
allowed:
  test/cheap:  {vision: false, price: [0.1, 0.5]}
  test/vision: {vision: true,  price: [0.1, 0.5]}
provider: {data_collection: deny, require_parameters: true}
limits: {max_output_tokens: 1000, timeout_seconds: 5, max_retries: 0}
"""


class FakeOpenRouter:
    """Returns queued replies and records every request body."""

    def __init__(self):
        self.replies: list[httpx.Response] = []
        self.requests: list[dict] = []

    def reply(self, content=None, *, cost=0.001, finish="stop", refusal=None, status=200):
        if status != 200:
            self.replies.append(httpx.Response(status, json={"error": {"message": "nope"}}))
            return
        body = {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test/cheap",
            "provider": "FakeCloud",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {
                        "role": "assistant",
                        "refusal": refusal,
                        "content": content if isinstance(content, str) else json.dumps(content),
                    },
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": cost},
        }
        self.replies.append(httpx.Response(200, json=body))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai"
        assert request.headers["authorization"] == "Bearer test-key"
        self.requests.append(json.loads(request.content))
        return self.replies.pop(0)


@pytest.fixture()
def fake(tmp_path: Path, monkeypatch):
    f = FakeOpenRouter()
    path = tmp_path / "models.yaml"
    path.write_text(MODELS_YAML)
    registry = reg_mod.Registry(path, http_client=httpx.Client(transport=httpx.MockTransport(f)))
    monkeypatch.setattr(reg_mod, "get_registry", lambda: registry)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return f


def test_strict_schema_requires_everything():
    s = strict_schema(Extraction)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    assert "default" not in json.dumps(s)


def test_request_shape_and_cost(fake):
    fake.reply(GOOD, cost=0.0021)
    out = llm.complete("screener", [{"role": "user", "content": "hi"}], schema=Extraction)
    body = fake.requests[0]
    assert body["model"] == "test/cheap"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"] == {"data_collection": "deny", "require_parameters": True}
    assert body["max_tokens"] == 1000
    assert isinstance(out.parsed, Extraction) and out.parsed.company == "Acme"
    assert out.cost_usd == pytest.approx(0.0021) and out.input_tokens == 100
    assert out.served_by == "test/cheap/FakeCloud"


def test_schema_retry_then_success(fake):
    fake.reply('{"company": "Acme"}', cost=0.001)  # missing fields
    fake.reply("```json\n" + json.dumps(GOOD) + "\n```", cost=0.002)  # fenced, but valid
    out = llm.complete("screener", [{"role": "user", "content": "hi"}], schema=Extraction)
    assert out.attempts == 2 and out.cost_usd == pytest.approx(0.003)
    assert "did not match" in fake.requests[1]["messages"][-1]["content"]


def test_schema_fails_twice(fake):
    fake.reply("not json", cost=0.001)
    fake.reply("still not json", cost=0.001)
    with pytest.raises(llm.SchemaFailed) as e:
        llm.complete("screener", [{"role": "user", "content": "hi"}], schema=Extraction)
    assert e.value.cost_usd == pytest.approx(0.002)


def test_refusal_and_provider_errors(fake):
    fake.reply("", refusal="I can't help with that")
    with pytest.raises(llm.Refused):
        llm.complete("screener", [{"role": "user", "content": "x"}], schema=Extraction)
    fake.reply(status=429)
    with pytest.raises(llm.ProviderError, match="rate limited"):
        llm.complete("screener", [{"role": "user", "content": "x"}])


def test_allowlist_vision_and_key(fake, monkeypatch):
    with pytest.raises(llm.ModelNotAllowed, match="isn't in config/models.yaml"):
        llm.complete("screener", [], model="openai/some-expensive-model")
    with pytest.raises(llm.ModelNotAllowed, match="can't read images"):
        llm.complete("screenshot", [], model="test/cheap", vision=True)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setattr(reg_mod, "load_env", lambda: None)
    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY"):
        llm.complete("screener", [])


def test_extract_wraps_errors_with_cost(fake):
    from appa.core.models import Job

    fake.reply("nope", cost=0.004)
    fake.reply("nope", cost=0.004)
    job = Job(company="A", title="B", url="https://x", description="x" * 300)
    with pytest.raises(extract.ExtractionError) as e:
        extract.extract(job)
    assert e.value.cost_usd == pytest.approx(0.008)


def test_real_config_is_valid():
    r = reg_mod.Registry()
    assert r.resolve("screener").name in r.models
    assert r.resolve("screenshot", vision=True).vision
    assert r.provider.get("data_collection") == "deny"


# ---- eval ----------------------------------------------------------------------


def test_eval_cases_load():
    cases = evals.load_cases(dirs=[evals.ROOT / "evals" / "screening"])
    assert len(cases) >= 15
    assert all(len(c.posting) >= 200 for c in cases)


def test_eval_scoring(fake):
    cases = [
        evals.Case("ok", "A", "SWE", "Build things. " * 30, {"verdict": "candidate", "sponsorship": "not_mentioned"}),
        evals.Case(
            "should-reject",
            "B",
            "SWE",
            "Must be a U.S. citizen. " + "Build things. " * 30,
            {"verdict": "reject", "citizenship_required": True},
        ),
        evals.Case(
            "should-pass",
            "C",
            "SWE",
            "We are unable to sponsor visas now or in the future. " + "x " * 120,
            {"verdict": "candidate"},
        ),
    ]
    fake.reply(GOOD, cost=0.001)  # ok: correct
    fake.reply(GOOD, cost=0.001)  # should-reject: model misses citizenship
    fake.reply(
        {
            **GOOD,
            "sponsorship": "no_sponsorship_now_or_future",
            "sponsorship_quote": "We are unable to sponsor visas now or in the future.",
        },
        cost=0.001,
    )
    rep = evals.run(None, cases, workers=1, max_cost=1.0)
    assert rep.cases == 3 and rep.verdict_correct == 1
    assert rep.false_rejects == ["should-pass"]
    # The model said "no citizenship requirement", but the text mentions it: rules send it to review,
    # so it's a field miss, not an auto-apply to a job you can't take.
    assert rep.missed_rejects == []
    assert rep.field_correct["citizenship_required"] == 0
    assert rep.cost_usd == pytest.approx(0.003)
    text = evals.format_reports([rep])
    assert "should-reject" in text and "false rej" in text


def test_eval_max_cost_stops(fake):
    cases = [evals.Case(f"c{i}", "A", "SWE", "Build things. " * 30, {"verdict": "candidate"}) for i in range(5)]
    for _ in range(5):
        fake.reply(GOOD, cost=0.6)
    rep = evals.run(None, cases, workers=1, max_cost=1.0)
    assert rep.cases == 2 and rep.stopped_early
