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

## Connected sandbox workspace

Development infrastructure can be provisioned, connected, inspected, analysed,
and destroyed without leaving the persistent shell. Start the shell in
development mode:

```bash
.venv/bin/verisentinel --dev
```

Then enter:

```text
infra create --scenario scenarios/gcs_action_delivery_flag/scenario.yaml
sandbox connect infra_REPLACE_WITH_RETURNED_ID
```

The infrastructure record retains its exact scenario path, so the development
connect command does not require `--scenario`. A successful connection enters a
sandbox-scoped application prompt named for the scenario service account. This
is not an operating-system or container shell; it accepts only maintained
Verisentinel commands.

Use the connected environment commands at that prompt:

```text
env show
env analyse --rebuild-snapshot
```

`env show` reports the exact connected identity, scenario target, opaque
credential reference, declared effective permissions, detection profile, and,
after analysis, the current Environment Brain version, discovered resources,
capabilities, completed actions, and engagement status. It does not print a
token or claim to enumerate permissions outside the scenario's authorized
scope.

`env analyse` reconstructs the exact service-account impersonation source in
development mode and runs the maintained GreenAgent workflow. The proposer may
suggest options, but the validator filters them and supplies rejection feedback
for bounded retries. Only admissible candidates reach the terminal. The
operator must then approve, reject, request alternatives, reject all, or
terminate; approval is never inferred. An approved option is revalidated before
the one-time execution authorization is consumed.

Enter `exit` or `/back` to return to the top-level prompt without disconnecting.
Use `/sandbox` to re-enter the active connection. Cleanup is explicit:

```text
sandbox disconnect
infra destroy infra_REPLACE_WITH_RETURNED_ID
```

`infra destroy` asks for confirmation, requires the same ADC principal that
created the deployment, applies a Terraform destroy plan from the persisted
state, verifies the state is empty, and marks the record destroyed. Use
`infra destroy ID --yes` only in automation after independently checking the
exact ID. Destroying an actively connected deployment first deactivates its
fixed gateway and clears the connection.

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
| `/back` | Leave the sandbox prompt without disconnecting. |
| `/sandbox` | Re-enter the active sandbox connection. |
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
