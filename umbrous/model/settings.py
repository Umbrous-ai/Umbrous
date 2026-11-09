"""Validated per-request generation controls, shared by HTTP and the run loop."""

from dataclasses import dataclass
from typing import Optional

from umbrous.model.base import ModelInfo, ProviderError


@dataclass(frozen=True)
class GenerationSettings:
    reasoning_effort: Optional[str] = None
    speed: str = "standard"

    @classmethod
    def parse(cls, reasoning_effort=None, speed=None):
        if reasoning_effort is not None and (not isinstance(reasoning_effort, str)
                                            or not 1 <= len(reasoning_effort) <= 32):
            raise ProviderError("Reasoning must be one of the selected model's advertised levels.")
        if speed is not None and (not isinstance(speed, str) or speed not in ("standard", "fast")):
            raise ProviderError("Speed must be standard or fast.")
        return cls(reasoning_effort, speed or "standard")

    def for_model(self, model: ModelInfo) -> dict:
        if self.speed not in model.speeds:
            raise ProviderError("That model does not advertise the selected speed. Choose an available speed.")
        if self.reasoning_effort is not None and self.reasoning_effort not in model.reasoning_efforts:
            raise ProviderError("That model does not advertise the selected reasoning level. Choose an available level.")
        if model.provider == "claude":
            # No level chosen: Claude Code's own default for the model.
            return {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        if model.provider != "codex":
            return {}
        effort = self.reasoning_effort or model.default_reasoning_effort
        if not effort or effort not in model.reasoning_efforts:
            raise ProviderError("The ChatGPT model catalog has no usable default reasoning level.")
        return {"reasoning_effort": effort, "speed": self.speed}
