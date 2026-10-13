"""LLM Decision Agent: the only step in the pipeline that calls an LLM.

Takes the Revenue Optimizer's shortlists plus the raw signals (comp median,
pace, gap flags, guardrails) and asks Claude to pick a final price per
listing per date and explain why in plain language — including how it's
weighing the twin-listing tradeoff. Everything upstream is deterministic;
this step exists because *synthesizing competing signals into one number
with a defensible rationale* is the one part that benefits from reasoning.
"""

import json
import os

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

DECISION_TOOL = {
    "name": "submit_pricing_decisions",
    "description": "Submit final nightly price recommendations with rationale.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "listing_id": {"type": "string"},
                        "date": {"type": "string"},
                        "recommended_price": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["listing_id", "date", "recommended_price", "rationale"],
                },
            }
        },
        "required": ["decisions"],
    },
}


def _build_prompt(signals_by_listing: dict, guardrails: dict) -> str:
    return f"""You are pricing two nearly-identical 4bd/2ba Airbnb listings that are
one block apart and effectively compete for the same guests.

Guardrails (must not be violated): {json.dumps(guardrails)}

For each listing/date below, you're given: the Revenue Optimizer's top
candidate prices with their expected-revenue scores, the comp market
median, the booking pace ratio (below 1.0 = behind historical pace), and
whether it's an orphan gap night.

Pick ONE final price per listing per date. Favor the optimizer's top
candidate unless the signals justify a different pick — explain why in
one or two sentences, including any twin-listing tradeoff (e.g. steering
one listing cheaper to fill it while the other captures a higher rate).

Signals:
{json.dumps(signals_by_listing, indent=2)}

Call submit_pricing_decisions with your final decisions.
"""


def _fallback_decisions(signals_by_listing: dict) -> list[dict]:
    """Used when no ANTHROPIC_API_KEY is set: skips the LLM synthesis step and
    just takes the Revenue Optimizer's top-scored candidate per listing/date.
    """
    decisions = []
    for listing_id, signals in signals_by_listing.items():
        for d, shortlist in signals.get("shortlists", {}).items():
            if not shortlist:
                continue
            decisions.append(
                {
                    "listing_id": listing_id,
                    "date": d,
                    "recommended_price": shortlist[0]["price"],
                    "rationale": "LLM decision step skipped (no ANTHROPIC_API_KEY) — used Revenue Optimizer's top candidate.",
                }
            )
    return decisions


def run(signals_by_listing: dict, guardrails: dict) -> list[dict]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _fallback_decisions(signals_by_listing)

    client = anthropic.Anthropic()
    prompt = _build_prompt(signals_by_listing, guardrails)

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "submit_pricing_decisions"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_pricing_decisions":
            return block.input["decisions"]
    raise RuntimeError("Decision agent did not return a tool call — check the model response.")
