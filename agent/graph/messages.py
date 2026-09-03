"""Assembling the message list the Anthropic API receives.

Rebuilt from state on every turn rather than accumulated in place, so that older
evidence can collapse to its summary line. That costs nothing: the cache
breakpoint sits on the last tool definition, so the cached prefix is tools +
system and the messages were never part of it.

Two API rules shape everything here:

1. **Every ``tool_use`` block must be answered by a ``tool_result`` in the next
   user message.** We replay the model's own assistant turns verbatim, so a call
   the executor declined to run - a duplicate, or one over the per-turn cap -
   cannot simply be dropped; it needs a synthetic result saying so. That is why
   ``gather_evidence`` produces exactly one evidence entry per read-tool block.
2. **With extended thinking on, the latest assistant turn's ``thinking`` blocks
   must come back unmodified**, signature included. Earlier turns do not need
   them, so they are stripped there - they are pure tokens by then.
"""

from agent.graph.hypothesis import UPDATE_HYPOTHESIS_NAME
from agent.graph.render import alert_brief, render_result
from agent.graph.state import AssistantTurn, InvestigationState, ToolCall

_THINKING_TYPES = {"thinking", "redacted_thinking"}

# Answers for the blocks that have no tool result of their own.
HYPOTHESIS_ACK = "Hypothesis recorded."
MISSING_RESULT = (
    "No result was recorded for this call. Treat the signal as unavailable and "
    "ask for something else."
)


def synthetic_assistant_turn(iteration: int, calls: list[ToolCall]) -> AssistantTurn:
    """Build the assistant turn for calls the graph made on its own.

    Used for the deterministic opening sweep, which no model requested. Giving
    it the same ``tool_use``/``tool_result`` shape as every later turn means
    there is exactly one representation of evidence in the conversation, at the
    price of fabricating ids - which the model never inspects.

    The generated id is written back onto each ``ToolCall``, because that id is
    the only thing that later links a result to the block that asked for it.
    """
    content = []
    for index, call in enumerate(calls):
        call.id = call.id or f"toolu_seed_{iteration}_{index}"
        content.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
        )
    return AssistantTurn(iteration=iteration, content=content)


def _assistant_content(turn: AssistantTurn, *, keep_thinking: bool) -> list[dict]:
    if keep_thinking:
        return list(turn.content)
    kept = [b for b in turn.content if b.get("type") not in _THINKING_TYPES]
    # A turn of nothing but thinking would strip to empty, and an empty
    # assistant turn is a 400.
    return kept or list(turn.content)


def build_messages(state: InvestigationState) -> list[dict]:
    """Render the whole conversation so far, newest evidence in full."""
    messages: list[dict] = [
        {
            "role": "user",
            "content": alert_brief(
                state["alert"], reference_time=state["reference_time"]
            ),
        }
    ]

    turns = sorted(state["assistant_turns"], key=lambda turn: turn.iteration)
    if not turns:
        return messages

    latest_turn = turns[-1].iteration
    evidence = state["evidence"]
    newest_evidence = max((entry.iteration for entry in evidence), default=None)
    by_block = {entry.tool_use_id: entry for entry in evidence if entry.tool_use_id}

    for turn in turns:
        messages.append(
            {
                "role": "assistant",
                "content": _assistant_content(
                    turn, keep_thinking=turn.iteration == latest_turn
                ),
            }
        )

        results = []
        for block in turn.content:
            if block.get("type") != "tool_use":
                continue
            entry = by_block.get(block.get("id"))
            if entry is not None:
                content = render_result(
                    entry.result, detailed=entry.iteration == newest_evidence
                )
            elif block.get("name") == UPDATE_HYPOTHESIS_NAME:
                content = HYPOTHESIS_ACK
            else:
                content = MISSING_RESULT
            # No is_error flag: a tool reporting ok=False did its job, and
            # flagging it would tell the model its call was malformed.
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": content,
                }
            )

        if results:
            messages.append({"role": "user", "content": results})

    return messages
