from types import SimpleNamespace
from typing import Any, Callable

from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock

TEXT_SAMPLE = """2026.11.04 星期三
20:49 王小明 因應新產品上市調整成本價說明，價格折扣的物料群組需與原先拆開，故新增物料群組：
G01：測試品項C
G02：成品-甲款-48
G02-1：成品-甲款-28
G03：成品-乙款-48
G03-1：成品-乙款-28"""

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def text(t: str):
    return SimpleNamespace(stop_reason="end_turn", content=[BetaTextBlock.model_construct(type="text", text=t)])


_n = 0


def tool(name: str, inp: dict, say: str | None = None):
    global _n
    _n += 1
    blocks = []
    if say:
        blocks.append(BetaTextBlock.model_construct(type="text", text=say))
    blocks.append(BetaToolUseBlock.model_construct(type="tool_use", id=f"toolu_{_n}", name=name, input=inp))
    return SimpleNamespace(stop_reason="tool_use", content=blocks)


class ScriptedLLM:
    """Each step is a response, or a callable(params) -> response (to assert on inputs)."""

    def __init__(self, steps: list[Any]):
        self.steps = list(steps)
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        step = self.steps.pop(0)
        return step(params) if isinstance(step, Callable) else step

    def last_tool_result(self) -> dict:
        msgs = self.calls[-1]["messages"]
        return msgs[-1]["content"][0]
