"""Assemble Context: decide exactly what the model is sent for one reply.

The model sees three things, in this order:

    1. system instructions   (config: system_prompt)
    2. recent turns           earlier messages of this chat, newest kept first
    3. the new message

…and all of it must fit in a budget:

    window  = min(config context_tokens, the model's own context length)
    budget  = window - reply_tokens         (room kept free for the answer)

Two limits drop older turns, and both are reported, never silent:
  - recent_turns: at most this many earlier messages, however short;
  - the token budget: if they still do not fit, the oldest go first.

If the system instructions plus the new message alone do not fit, nothing is
sent and the person is told the message is too long.

Token counts here are an *estimate* (about 4 characters per token, plus a few
per message for formatting). Models count differently; after the reply,
Ollama's real count is shown next to the estimate. The window is also passed to
Ollama as num_ctx, so Ollama does not quietly cut the prompt to its own default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

CHARS_PER_TOKEN = 4
TOKENS_PER_MESSAGE = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN + TOKENS_PER_MESSAGE


@dataclass(frozen=True)
class Turn:
    role: str      # "user" or "assistant"
    content: str


class MessageTooLong(ValueError):
    code = "message_too_long"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ContextPlan:
    messages: List[dict]          # exactly what goes to the model
    window: int                   # num_ctx passed to the model
    budget: int                   # window minus reply room
    reply_tokens: int
    estimated_tokens: int
    turns_available: int          # earlier messages in this chat
    turns_included: int
    dropped_by_turn_limit: int
    dropped_by_budget: int
    recent_turns_limit: int
    notices: List[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "window": self.window, "budget": self.budget, "reply_tokens": self.reply_tokens,
            "estimated_tokens": self.estimated_tokens, "turns_available": self.turns_available,
            "turns_included": self.turns_included, "dropped_by_turn_limit": self.dropped_by_turn_limit,
            "dropped_by_budget": self.dropped_by_budget, "recent_turns_limit": self.recent_turns_limit,
            "notices": self.notices,
        }


def assemble(system_prompt: str, history: Sequence[Turn], new_message: str, *, recent_turns: int,
             context_tokens: int, reply_tokens: int, model_context: Optional[int] = None) -> ContextPlan:
    window = min(context_tokens, model_context) if model_context else context_tokens
    reply_tokens = min(reply_tokens, window // 2)  # never reserve more than half the window
    budget = window - reply_tokens

    fixed = estimate_tokens(system_prompt) + estimate_tokens(new_message)
    if fixed > budget:
        raise MessageTooLong(
            f"This message is too long for the model's context: about {fixed} tokens with the system "
            f"instructions, but only {budget} fit (window {window}, {reply_tokens} kept for the reply). "
            "Shorten it, or raise context_tokens if the model supports a larger window.")

    candidates = list(history)[-recent_turns:] if recent_turns > 0 else []
    dropped_by_turn_limit = len(history) - len(candidates)

    kept: List[Turn] = []
    used = fixed
    for turn in reversed(candidates):  # newest first
        cost = estimate_tokens(turn.content)
        if used + cost > budget:
            break
        kept.append(turn)
        used += cost
    kept.reverse()
    dropped_by_budget = len(candidates) - len(kept)

    notices = []
    if dropped_by_turn_limit:
        notices.append(f"{dropped_by_turn_limit} earlier message(s) left out: only the last {recent_turns} are sent (recent_turns).")
    if dropped_by_budget:
        notices.append(f"{dropped_by_budget} more earlier message(s) left out to fit the {budget}-token budget.")

    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": t.role, "content": t.content} for t in kept]
    messages.append({"role": "user", "content": new_message})
    return ContextPlan(messages, window, budget, reply_tokens, used, len(history), len(kept),
                       dropped_by_turn_limit, dropped_by_budget, recent_turns, notices)
