"""Agent actions: the write surface, kept deliberately apart from ``TOOLS``.

``TOOLS`` is what the model is handed. ``ACTIONS`` is not — the model may
*propose* an action by name in its hypothesis, but only the deterministic policy
gate in ``agent/policy/`` decides whether one runs, and only the graph's ``act``
node invokes it. Keeping the two registries separate is what makes that
structural rather than a convention someone has to remember.

Everything the read tools promise holds here too: an action never raises for an
expected failure, and every result carries ``source`` + ``query`` so an action
can be cited the same way an observation can.

The kill switch lives here, once. With ``AGENT_ALLOW_WRITES`` off, a validated
call comes back ``ok=True, executed=False, dry_run=True`` describing what would
have run — so the whole path, gate included, is exercisable against the live
stack without touching it.
"""

from pydantic import ValidationError

from agent import config

# Re-exported: ``agent.tools.actions`` is the write side's front door, even
# though the envelope itself lives in base.py so the tools can import it
# without importing this registry.
from agent.tools.base import ActionInput, ActionResult, action_query, failure
from agent.tools.restart import RestartInput, RestartResult, restart_service
from agent.tools.rollback import RollbackInput, RollbackResult, rollback_deploy
from agent.tools.toggle import ToggleInput, ToggleResult, toggle_config

ACTIONS_SOURCE = "agent action registry"


# The model is never handed this registry; agent/tests/test_actions_registry.py
# holds that line. The descriptions are written for a human reading an audit
# trail, not for a model choosing between them.
ACTIONS: dict[str, dict] = {
    "restart_service": {
        "description": (
            "Restart one service's container, then poll its /health until it "
            "answers. Clears any state held in that process, and drops whatever "
            "requests were in flight."
        ),
        "input_model": RestartInput,
        "result_model": RestartResult,
        "runner": restart_service,
    },
    "toggle_config": {
        "description": (
            "Reset one service's runtime configuration to its default, without "
            "restarting it or dropping requests. The service confirms the reset "
            "itself; the independent check is that its error metric stops rising."
        ),
        "input_model": ToggleInput,
        "result_model": ToggleResult,
        "runner": toggle_config,
    },
    "rollback_deploy": {
        "description": (
            "Revert a service to the version that preceded its most recent "
            "deploy, recording the reversal in the deploy ledger. Changes what "
            "is running, so it is the broadest action available."
        ),
        "input_model": RollbackInput,
        "result_model": RollbackResult,
        "runner": rollback_deploy,
    },
}


def get_action(name: str) -> dict:
    """Return the registry entry for ``name``, or raise KeyError naming the rest."""
    try:
        return ACTIONS[name]
    except KeyError:
        raise KeyError(f"unknown action '{name}'; known: {sorted(ACTIONS)}") from None


async def run_action(
    name: str,
    arguments: dict,
    *,
    registry: dict | None = None,
    allow_writes: bool | None = None,
    **kwargs,
) -> ActionResult:
    """Validate raw arguments into the action's input model and run it.

    Order matters: unknown name, then arguments, then the kill switch. A dry run
    reported for a call that could never have run would be a lie.
    """
    registry = ACTIONS if registry is None else registry
    if allow_writes is None:
        allow_writes = config.AGENT_ALLOW_WRITES

    entry = registry.get(name)
    if entry is None:
        return failure(
            tool=name,
            source=ACTIONS_SOURCE,
            query="",
            error=f"unknown action '{name}'; known: {sorted(registry)}",
            summary=(
                f"There is no action called '{name}'. Available: "
                f"{', '.join(sorted(registry))}."
            ),
            model=ActionResult,
        )

    try:
        parsed = entry["input_model"](**arguments)
    except ValidationError as exc:
        return failure(
            tool=name,
            source=ACTIONS_SOURCE,
            query="",
            error=f"invalid arguments for {name}: {exc}",
            summary=(
                f"The arguments for {name} were not valid; check the field names "
                f"and types and try again."
            ),
            model=entry["result_model"],
        )

    query = action_query(name, parsed)

    if not allow_writes:
        return entry["result_model"](
            tool=name,
            ok=True,
            summary=(
                f"Dry run: would have run {query}, but writes are disabled "
                f"(set AGENT_ALLOW_WRITES=1 to enable). Nothing was changed."
            ),
            source=ACTIONS_SOURCE,
            query=query,
            target=parsed.service,
            executed=False,
            dry_run=True,
        )

    return await entry["runner"](parsed, **kwargs)


__all__ = [
    "ACTIONS",
    "ACTIONS_SOURCE",
    "ActionInput",
    "ActionResult",
    "action_query",
    "get_action",
    "run_action",
]
