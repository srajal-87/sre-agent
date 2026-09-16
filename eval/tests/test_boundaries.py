"""The one-way boundary between the agent and the thing that scores it."""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
PRODUCTION_TREES = ("agent", "api", "injector", "services")

IMPORTS_EVAL = re.compile(r"^\s*(?:from\s+eval[\s.]|import\s+eval\b)", re.MULTILINE)


def production_sources():
    for tree in PRODUCTION_TREES:
        for path in (REPO / tree).rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def test_no_agent_module_imports_eval():
    """The eval reads agent.policy.table so that no second copy of the policy
    exists - that direction is legitimate and deliberate. The reverse never is:
    an agent that can import the scorer can be tuned to it, and the scoring
    rules would become part of the system under test."""
    offenders = [
        str(path.relative_to(REPO))
        for path in production_sources()
        if IMPORTS_EVAL.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_scorer_does_not_reach_into_the_stack():
    """`cd eval && pytest` must stay green with no DATABASE_URL, no AWS key and
    no Docker, because Stage 6's CI gate has nothing else to run. Importing a
    tool, the LLM client or the database from the pure core would end that."""
    source = (REPO / "eval" / "score.py").read_text(encoding="utf-8")

    for forbidden in ("agent.tools", "agent.graph.llm", "api.app", "httpx", "docker"):
        assert forbidden not in source, f"score.py must not import {forbidden}"


@pytest.mark.parametrize("module", ["eval.score", "eval.truth"])
def test_the_pure_modules_import_with_no_environment(module, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    __import__(module)
