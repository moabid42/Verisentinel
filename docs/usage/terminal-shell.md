# Interactive Terminal Shell

The maintained Verisentinel frontend is a persistent terminal shell. Run it
from `code/` in an interactive terminal:

```bash
.venv/bin/verisentinel
```

An interactive invocation creates a session and presents a short session prompt:

```text
VERISENTINEL  NEW SESSION
session_0123456789abcdef0123456789abcdef

01234567 ›
```

Direct subcommands remain available for scripts and automation. When standard
input or output is redirected, invoking `verisentinel` without a subcommand
prints the non-interactive command launcher instead of waiting for input.

## Commands

The shell accepts canonical CLI routes without the `verisentinel` prefix. It
also recognizes concise, operation-first forms:

```text
build corpus
build sandbox
check sandbox
validate scenario scenario.yaml
run scenario scenario.yaml --credential-source adc
session list
```

Entering `verisentinel corpus status` inside the prompt also works. Input is
parsed as arguments and dispatched only to known Verisentinel routes. It is not
a system shell: arbitrary programs, shell operators, and filesystem commands
are never executed.

The scenario review remains explicitly approval-gated. Empty input does not
select a candidate, and interruption never implies approval or execution.

## Session controls

Slash commands manage the current terminal workspace:

| Command | Behavior |
| --- | --- |
| `/help` | Show commands and session controls. |
| `/status` | Show the current session metadata and outcomes. |
| `/history` | Show the same sanitized command history. |
| `/sessions` | List saved sessions by recent activity. |
| `/new` | Close the current session and create another. |
| `/clear` | Clear the terminal display. |
| `/exit` | Save and close the session. |

`Ctrl+C` cancels the current prompt and keeps the session open. `Ctrl+D` saves
and closes it.

Outside the shell, inspect and resume sessions with:

```bash
.venv/bin/verisentinel session list
.venv/bin/verisentinel session show SESSION_ID
.venv/bin/verisentinel session resume SESSION_ID
```

The equivalent direct resume form is
`.venv/bin/verisentinel shell --resume SESSION_ID`.

## Persistence and privacy

Session records are atomically persisted under
`runtime/shell/sessions/`, which is generated and gitignored. Each record stores:

- the opaque session ID and lifecycle timestamps;
- active, closed, or interrupted status;
- a bounded history of sanitized operation names; and
- the exit code for each recorded operation.

Raw input lines, scenario paths, credential-source values, environment-variable
names, credentials, and command output are not stored in the session record.
Planner traces and runtime records continue to use their existing dedicated
locations and security boundaries.
