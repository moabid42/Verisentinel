# Architecture

This directory records the accepted boundaries and planned evolution of the
Verisentinel application. The current application remains the deterministic,
human-gated planner described in [`code/README.md`](../../code/README.md). These
documents define future changes; they do not claim that the planned providers,
commands, or copilot adapters are implemented.

## Decision Records

| Record | Status | Decision |
| --- | --- | --- |
| [ADR-0001](decisions/0001-copilot-orchestration-boundaries.md) | Accepted | Separate model-driven copilot sessions from deterministic authorization and execution authority. |

## Reading Order

1. Read ADR-0001 for orchestration ownership, knowledge boundaries, and the
   optional DeepSeek adapter.
2. Follow later decision records for credential handling and execution
   isolation.
3. Use the staged roadmap, once added, to sequence implementation and acceptance
   gates.

## Repository Guidance

Repository changes follow the root and component-specific `AGENTS.md` files.
Codex composes those files from the repository root toward the working directory,
with nearer guidance taking precedence, as described in the
[official OpenAI documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
