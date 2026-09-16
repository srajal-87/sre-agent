"""Turning a finished investigation into an ``investigations`` row.

Pure functions only: this module builds a dict and hands it back. The session,
the transaction and the commit all stay in ``app/repository.py``, which is what
keeps the shape of the row testable with nothing to connect to.

The split it encodes: **a field gets a column only if the migration gave it
one.** Everything else - the fault type, what was ruled out, the policy gate's
verdict, the token counts behind the cost - nests into the ``evidence`` jsonb,
flat at its top level rather than under a wrapper, so that
``evidence->>'fault_type'`` is a query the Phase 5 eval can write against
``incidents.ground_truth_fault``.

Nothing here imports the agent package. It needs a report only to be a Pydantic
model, which keeps the API layer's dependency on the graph to the one place that
actually runs it.
"""

# Report fields with no column of their own. Order is the order they read in.
NESTED_IN_EVIDENCE = (
    "fault_type",
    "service",
    "recommendation",
    "policy_decision",
    "citations",
    "ruled_out",
    "notes",
    "stop_reason",
    "llm_calls",
    "input_tokens",
    "output_tokens",
)


def investigation_row(report) -> dict:
    """The column values for one finished investigation.

    Dumped in json mode first, so every value in the row - UUIDs, the nested
    policy decision, anything datetime-shaped - is already something the jsonb
    column and the driver can take.
    """
    dumped = report.model_dump(mode="json")

    evidence = dict(dumped.get("evidence") or {})
    evidence.update({field: dumped.get(field) for field in NESTED_IN_EVIDENCE})

    return {
        "status": dumped["status"],
        "diagnosis": dumped["diagnosis"],
        "confidence": dumped["confidence"],
        "action_taken": dumped["action_taken"],
        "action_result": dumped["action_result"],
        "evidence": evidence,
        "steps": dumped["steps"],
        "cost_usd": dumped["cost_usd"],
        "latency_ms": dumped["latency_ms"],
        # A text column, not a uuid one - model_dump has already made it a
        # string, or left it null for a run that was never traced.
        "langsmith_trace_id": dumped["trace_id"],
        "error": dumped["error"],
    }
