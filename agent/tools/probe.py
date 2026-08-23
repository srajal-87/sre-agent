"""Run one tool by hand and print what the agent would see.

This is how each tool is exercised individually against the live stack during a
real injected fault, before the reasoning loop ever calls one.

Usage:
    python -m agent.tools.probe --list
    python -m agent.tools.probe --tool query_metrics \
        --args '{"metric":"http_request_duration_seconds","service":"downstream-dep",\
"path":"/data","aggregation":"p99"}'
    python -m agent.tools.probe --tool query_logs --args '{"levels":["ERROR"],"lookback_minutes":5}'
    python -m agent.tools.probe --tool query_deploy_history --args '{"lookback_minutes":120}'

Exit codes: 0 = ok result, 1 = ok=False result, 2 = bad usage.
"""

import argparse
import asyncio
import json

from agent.tools import TOOLS, run_tool


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one agent tool and print its result.")
    parser.add_argument("--tool", help="tool name; see --list")
    parser.add_argument("--args", default="{}", help="tool arguments as a JSON object")
    parser.add_argument("--list", action="store_true", help="list the available tools")
    args = parser.parse_args(argv)

    if args.list:
        for name in sorted(TOOLS):
            print(f"{name}\n    {TOOLS[name]['description']}\n")
        return 0

    if not args.tool:
        _emit({"ok": False, "error": "--tool is required (or use --list)"})
        return 2

    try:
        arguments = json.loads(args.args)
    except json.JSONDecodeError as exc:
        _emit({"ok": False, "error": f"--args is not valid json: {exc}"})
        return 2

    if not isinstance(arguments, dict):
        _emit({"ok": False, "error": "--args must be a json object"})
        return 2

    result = asyncio.run(run_tool(args.tool, arguments))
    _emit(result.model_dump(mode="json"))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
