"""The assembled graph, driven end to end with no API and no backends.

The scripted run is the api-gateway timeout scenario: a deterministic opening
sweep, one reason turn that makes the discriminating move, and a second that
settles it. Two model calls, six tool calls.

The rest are termination tests, and they are the ones that matter most. A loop
that can hang is worse than one that stops early, so: a model that would look
forever still stops; a model that never works still produces a report; and
nothing ever reaches END without one.
"""

import asyncio
import json

from agent import config
from agent.graph import RECURSION_LIMIT, build_graph, investigate
from agent.graph.hypothesis import UPDATE_HYPOTHESIS_NAME
from agent.graph.llm import ModelResponse
from agent.graph.state import AlertSummary, InvestigationReport
from agent.tools.base import ToolResult

ALERT = AlertSummary(
    alertname="GatewayTimeouts",
    service="api-gateway",
    severity="critical",
    summary="api-gateway is returning 504s",
)

# One stable query per tool, so a scripted citation can match what was issued.
QUERIES = {
    "query_metrics": "promql for the gateway",
    "query_logs": "log filter levels=ERROR,WARNING",
    "query_deploy_history": "select * from deploys where ...",
}


class Runner:
    def __init__(self, *, ok=True):
        self.calls = []
        self._ok = ok

    async def __call__(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        return ToolResult(
            tool=name,
            ok=self._ok,
            summary=f"summary from {name}",
            source="fake",
            query=QUERIES.get(name, "unknown query"),
        )


def _hypothesis_block(**overrides):
    return {
        "type": "tool_use",
        "id": f"toolu_h{overrides.get('confidence', 0)}",
        "name": UPDATE_HYPOTHESIS_NAME,
        "input": {
            "fault_type": "timeout",
            "service": "api-gateway",
            "statement": "the gateway's calls to data-service exceed its budget",
            "confidence": 0.72,
            "rationale": "p99 is pinned at the ceiling and the log names data-service.",
            "citations": [QUERIES["query_metrics"], QUERIES["query_logs"]],
            **overrides,
        },
    }


def _metrics_block(block_id, metric):
    return {
        "type": "tool_use",
        "id": block_id,
        "name": "query_metrics",
        "input": {"metric": metric, "service": "api-gateway"},
    }


class ScriptedModel:
    """Returns the scripted turns in order, then repeats the last one."""

    def __init__(self, *turns):
        self._turns = list(turns)
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        blocks = self._turns[min(self.calls, len(self._turns)) - 1]
        return ModelResponse(
            content=blocks,
            stop_reason="tool_use",
            input_tokens=1000,
            output_tokens=200,
            cost_usd=0.01,
            latency_ms=1000,
        )


def _investigate(model, runner=None):
    runner = runner or Runner()
    report = asyncio.run(investigate(ALERT, call=model, run=runner))
    return report, runner


# -- the graph itself -------------------------------------------------

def test_the_graph_compiles():
    assert build_graph() is not None


def test_the_recursion_limit_sits_above_our_own_iteration_ceiling():
    """Ours must fire first: LangGraph's raises and loses the whole run, while
    ours produces a report."""
    assert RECURSION_LIMIT > config.MAX_ITERATIONS * 4


# -- the scripted timeout investigation -------------------------------

def _timeout_script():
    return ScriptedModel(
        # Turn 1: a hypothesis, plus the two calls that discriminate between
        # "the gateway is at fault" and "downstream is slow".
        [
            {"type": "text", "text": "Checking the gateway's own counter."},
            _hypothesis_block(),
            _metrics_block("toolu_a1", "upstream_timeouts_total"),
            _metrics_block("toolu_a2", "http_request_duration_seconds"),
        ],
        # Turn 2: settled. No read tools requested.
        [
            _hypothesis_block(
                confidence=0.91,
                ruled_out=["downstream latency - data-service p99 flat at 0.03s"],
            )
        ],
    )


def test_the_scripted_investigation_reaches_a_confident_diagnosis():
    report, _ = _investigate(_timeout_script())

    assert isinstance(report, InvestigationReport)
    assert report.fault_type == "timeout"
    assert report.service == "api-gateway"
    assert report.confidence == 0.91
    assert report.stop_reason == "confident"
    assert report.status == "completed"


def test_it_takes_two_model_calls_and_six_tool_calls():
    """The deterministic opening sweep is what keeps it to two model calls."""
    model = _timeout_script()

    _, runner = _investigate(model)

    assert model.calls == 2
    assert len(runner.calls) == 6


def test_the_sweep_runs_before_any_reasoning():
    _, runner = _investigate(_timeout_script())

    assert [name for name, _ in runner.calls[:4]] == [
        "query_metrics",
        "query_metrics",
        "query_logs",
        "query_deploy_history",
    ]


def test_every_citation_in_the_report_resolves_to_a_query_that_was_issued():
    """The property the whole citation mechanism exists to guarantee."""
    report, _ = _investigate(_timeout_script())
    issued = {entry["result"]["query"] for entry in report.evidence["results"]}

    assert report.citations
    assert set(report.citations) <= issued


def test_the_report_records_the_confidence_trajectory():
    report, _ = _investigate(_timeout_script())

    assert [step["confidence"] for step in report.evidence["transcript"]] == [0.72, 0.91]


def test_the_report_counts_its_steps_and_its_cost():
    report, _ = _investigate(_timeout_script())

    assert report.steps == 2
    assert report.llm_calls == 2
    assert report.cost_usd == 0.02


def test_the_report_round_trips_through_json():
    report, _ = _investigate(_timeout_script())

    json.dumps(report.model_dump(mode="json"))


def test_what_was_ruled_out_reaches_the_report():
    report, _ = _investigate(_timeout_script())

    assert "data-service p99 flat" in report.ruled_out[0]


# -- termination ------------------------------------------------------

def _endless_model():
    """Always asks for one more thing, and never gets much surer."""
    turns = [
        [
            {
                "type": "tool_use",
                "id": f"toolu_h{n}",
                "name": UPDATE_HYPOTHESIS_NAME,
                "input": {
                    "fault_type": "unknown",
                    "statement": "still looking",
                    "confidence": 0.1 * n,
                    "rationale": "r",
                    "citations": [QUERIES["query_logs"]],
                },
            },
            {
                "type": "tool_use",
                "id": f"toolu_a{n}",
                "name": "query_logs",
                "input": {"lookback_minutes": n},
            },
        ]
        for n in range(1, config.MAX_ITERATIONS + 3)
    ]
    return ScriptedModel(*turns)


def test_a_model_that_would_look_forever_still_stops():
    model = _endless_model()

    report, _ = _investigate(model)

    assert report.stop_reason == "max_iterations"
    assert model.calls == config.MAX_ITERATIONS


def test_a_model_that_never_answers_still_produces_a_report():
    class Broken:
        calls = 0

        async def __call__(self, **kwargs):
            Broken.calls += 1
            return ModelResponse(ok=False, error="APIConnectionError: no route")

    report, _ = _investigate(Broken())

    assert report.status == "failed"
    assert report.stop_reason == "llm_error"
    assert "no route" in report.error
    assert report.diagnosis  # a readable row, not a null one


def test_tools_that_all_fail_do_not_stop_the_investigation():
    """A broken tool is not a broken system - the run continues and says so."""
    report, _ = _investigate(_timeout_script(), Runner(ok=False))

    assert report.status == "completed"
    assert all(
        entry["result"]["ok"] is False for entry in report.evidence["results"]
    )


def test_an_uncited_diagnosis_cannot_reach_a_confident_stop():
    model = ScriptedModel([_hypothesis_block(confidence=0.99, citations=[])])

    report, _ = _investigate(model)

    assert report.confidence == config.UNCITED_CONFIDENCE_CAP
    assert report.stop_reason != "confident"


def test_no_path_reaches_the_end_without_a_report():
    for model in (_timeout_script(), _endless_model()):
        assert _investigate(model)[0] is not None
