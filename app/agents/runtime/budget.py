"""What one agent run may spend. The run stops, and says why, when a limit is reached."""
from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    def __init__(self, what: str, limit: int):
        super().__init__(f"This run reached its limit of {limit} {what}.")
        self.what = what
        self.limit = limit


@dataclass
class Budget:
    llm_calls: int = 12
    provider_calls: int = 3
    worker_jobs: int = 4
    used: dict[str, int] = field(default_factory=lambda: {"llm_calls": 0, "provider_calls": 0, "worker_jobs": 0})

    def spend(self, what: str, amount: int = 1) -> None:
        limit = getattr(self, what)
        if self.used[what] + amount > limit:
            raise BudgetExceeded(what.replace("_", " "), limit)
        self.used[what] += amount

    def snapshot(self) -> dict:
        return {"limits": {"llm_calls": self.llm_calls, "provider_calls": self.provider_calls, "worker_jobs": self.worker_jobs},
                "used": dict(self.used)}
