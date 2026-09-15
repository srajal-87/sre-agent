"""Run one tool or one action by hand and print what the agent would see.

This is how each tool is exercised individually against the live stack during a
real injected fault, before the reasoning loop ever calls one. The write side
gets the same treatment: every action is runnable here before the graph's
``act`` node is allowed to invoke it.

Usage:
    python -m agent.tools.probe --list
    python -m agent.tools.probe --tool query_metrics \
        --args '{"metric":"http_request_duration_seconds","service":"downstream-dep",\
"path":"/data","aggregation":"p99"}'
    python -m agent.tools.probe --tool query_logs --args '{"levels":["ERROR"],"lookback_minutes":5}'
    python -m agent.tools.probe --tool query_deploy_history --args '{"lookback_minutes":120}'

    python -m agent.tools.probe --list-actions
    python -m agent.tools.probe --action restart_service --args '{"service":"downstream-dep"}'
    python -m agent.tools.probe --action restart_service --args '{"service":"downstream-dep"}' --execute

An action is a **dry run unless --execute is passed**, whatever the environment
says. Note the converse too: --execute writes even with AGENT_ALLOW_WRITES off.
That switch exists to stop the *agent* acting on its own judgement, and here a
person has typed the action, the target and --execute on one line. Requiring
both would only teach people to export the env var globally, which is worse.

Exit codes: 0 = ok result, 1 = ok=False result, 2 = bad usage.
"""

import argparse
import asyncio
import json

from agent.tools import TOOLS, run_tool
from agent.tools.actions import ACTIONS, run_action


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _parse_args_json(raw: str) -> dict:
    """Parse --args, raising ValueError with a message fit for stdout."""
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--args is not valid json: {exc}") from None
    if not isinstance(arguments, dict):
        raise ValueError("--args must be a json object")
    return arguments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one agent tool or action and print its result."
    )
    parser.add_argument("--tool", help="read-only tool name; see --list")
    parser.add_argument("--action", help="write action name; see --list-actions")
    parser.add_argument("--args", default="{}", help="arguments as a JSON object")
    parser.add_argument("--list", action="store_true", help="list the available tools")
    parser.add_argument(
        "--list-actions", action="store_true", help="list the available actions"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually perform the action (default: dry run)",
    )
    args = parser.parse_args(argv)

    if args.list:
        for name in sorted(TOOLS):
            print(f"{name}\n    {TOOLS[name]['description']}\n")
        return 0

    if args.list_actions:
        for name in sorted(ACTIONS):
            print(f"{name}\n    {ACTIONS[name]['description']}\n")
        return 0

    if args.tool and args.action:
        _emit({"ok": False, "error": "name either --tool or --action, not both"})
        return 2

    if not args.tool and not args.action:
        _emit(
            {
                "ok": False,
                "error": (
                    "--tool or --action is required (or use --list / --list-actions)"
                ),
            }
        )
        return 2

    try:
        arguments = _parse_args_json(args.args)
    except ValueError as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2

    if args.action:
        result = asyncio.run(
            run_action(args.action, arguments, allow_writes=args.execute)
        )
    else:
        result = asyncio.run(run_tool(args.tool, arguments))

    _emit(result.model_dump(mode="json"))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
