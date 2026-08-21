"""Mutable environment state tracked by the orchestrator across the loop.

There is no execution provider: the environment only ever changes by applying a solution
step's scripted ``output`` after the model's plan follows the expected command. This keeps
the whole run deterministic and infra-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from evaluation_pipeline.scenario import EvaluationScenario, StepOutput


@dataclass(slots=True)
class EnvState:
    held_permissions: set[str] = field(default_factory=set)
    discovered_resources: set[str] = field(default_factory=set)
    capabilities: set[str] = field(default_factory=set)
    completed_actions: list[str] = field(default_factory=list)

    @classmethod
    def initial(cls, scenario: EvaluationScenario) -> EnvState:
        env = scenario.environment
        return cls(
            held_permissions=set(env.starting_permissions),
            discovered_resources=set(env.discovered_resources),
            capabilities=set(env.capabilities),
            completed_actions=[],
        )

    def apply(self, command: str, output: StepOutput) -> None:
        """Apply a followed step's scripted delta and record the executed action."""
        self.held_permissions.update(output.gained_permissions)
        self.discovered_resources.update(output.discovered_resources)
        self.capabilities.update(output.gained_capabilities)
        self.completed_actions.append(f"technique:{command}")

    def snapshot(self) -> dict[str, list[str]]:
        return {
            "held_permissions": sorted(self.held_permissions),
            "discovered_resources": sorted(self.discovered_resources),
            "capabilities": sorted(self.capabilities),
            "completed_actions": list(self.completed_actions),
        }
