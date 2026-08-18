"""codex_bridge — a headless driver to run an enforced, adversarial code review
between two strong models: one implements, the other (sandboxed read-only) verifies.

See bridge.ask_codex() and the README for the debate loop.
"""
from .bridge import (
    CodexReply,
    MIN_ROUNDS,
    ROUND_CAP,
    VALID_EFFORT,
    VALID_SANDBOX,
    ask_codex,
    consensus_reached,
    parse_verdict,
    should_continue_past_cap,
)

__all__ = [
    "CodexReply",
    "ask_codex",
    "parse_verdict",
    "consensus_reached",
    "should_continue_past_cap",
    "MIN_ROUNDS",
    "ROUND_CAP",
    "VALID_SANDBOX",
    "VALID_EFFORT",
]
__version__ = "0.2.0"
