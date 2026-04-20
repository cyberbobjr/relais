# RELAIS Bundle System

Bundles are ZIP packages that extend a RELAIS installation with new capabilities — subagents, skills, tools, or any combination. They are installed to a per-user directory and picked up automatically via hot-reload, without restarting any service.

## Table of Contents

1. [Bundle Format](#bundle-format)
   - [Directory Layout](#directory-layout)
   - [bundle.yaml Reference](#bundleyaml-reference)
2. [What Each Component Does](#what-each-component-does)
3. [Post-Install Setup](#post-install-setup)
4. [Installing a Bundle](#installing-a-bundle)
5. [Uninstalling a Bundle](#uninstalling-a-bundle)
6. [Listing Installed Bundles](#listing-installed-bundles)
7. [Role-Gating for Subagents](#role-gating-for-subagents)
8. [Creating a Bundle](#creating-a-bundle)
9. [Security](#security)
10. [Conflict Resolution](#conflict-resolution)

---

## Bundle Format

### Directory Layout

A bundle is a ZIP file containing a single root folder whose name matches the bundle name. Every path inside the archive must be under that root folder.

```
my-bundle.zip
└── my-bundle/
    ├── bundle.yaml          # required manifest
    ├── subagents/           # optional — subagent packs
    │   └── my-agent/
    │       ├── subagent.yaml
    │       ├── tools/       # subagent-local tools
    │       └── skills/      # subagent-local skills
    ├── skills/              # optional — global skills
    │   └── my-skill/
    │       └── SKILL.md
    └── tools/               # optional — global tools
        └── my_tool.py
```

Only `bundle.yaml` is strictly required. A bundle may contain any subset of the three optional component directories.

### bundle.yaml Reference

```yaml
name: my-bundle           # required: [a-z0-9][a-z0-9-]*, must match root folder name
description: |            # required
  What this bundle provides.
version: "1.0.0"          # optional — semver string for display purposes
author: "Name <email>"    # optional
tools: []                 # optional — declared tool names (used for conflict detection)
setup: setup.md           # optional — path to a Markdown setup guide (see Post-Install Setup)
```

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Bundle identifier. Pattern: `[a-z0-9][a-z0-9-]*`. Must match the ZIP root folder name exactly. |
| `description` | Yes | Human-readable description of what the bundle provides. |
| `version` | No | Version string. Used for display; no version enforcement between installs. |
| `author` | No | Author name and contact. |
| `tools` | No | Explicit list of tool names exported by `tools/`. Used at install time to detect conflicts with already-installed bundles. |
| `setup` | No | Relative path (inside the bundle) to a Markdown file containing post-install setup instructions. When present, RELAIS automatically forwards the instructions to the assistant after installation. See [Post-Install Setup](#post-install-setup). |

---

## What Each Component Does

### tools/

Python modules placed in `tools/` are loaded into the global `ToolRegistry`. Every `BaseTool` instance exported by these modules becomes available to all agents and subagents, subject to the `ToolPolicy` in effect for the current request.

Tool modules must only import from prefixes that are on the allowed list (`aiguilleur.channels.`, `atelier.tools.`, `relais_tools.`). Modules that import from other prefixes are rejected at load time.

### skills/

Skill directories placed in `skills/` are loaded globally. A skill directory must contain at least a `SKILL.md` file. Once loaded, the skill is available to all agents via the `list_skills` / `read_skill` mechanism provided by DeepAgents.

Subagent-local skills placed under `subagents/<name>/skills/` are only exposed to that specific subagent.

### subagents/

Each subdirectory of `subagents/` is a subagent pack. It must contain a `subagent.yaml` and may optionally contain `tools/` and `skills/` subdirectories that are local to that subagent.

Subagent packs are loaded by `SubagentRegistry`. Installing a bundle does not automatically grant users access to its subagents — access is role-gated separately via `portail.yaml` (see [Role-Gating for Subagents](#role-gating-for-subagents)).

**Scope comparison:**

| Component | Scope | Access control |
|---|---|---|
| `tools/` | Global — all agents | ToolPolicy (per-profile) |
| `skills/` | Global — all agents | None beyond agent capability |
| `subagents/` | Available to orchestrator | `allowed_subagents` in portail.yaml |

---

## Post-Install Setup

Some bundles require configuration steps before they can be used — for example, providing credentials, creating a config file, or enabling a system service. The `setup` field in `bundle.yaml` points to a Markdown file inside the bundle that describes those steps.

### How it works

1. After a successful install, Commandant reads the file at the path declared in `setup`.
2. The file content is forwarded to Atelier as a task with the preamble:
   > *"The bundle '…' was just installed. Follow the setup instructions below to complete its configuration:"*
3. The assistant guides the user through the steps conversationally, using its available tools to create files, run commands, and update configuration.

### Writing a setup file

The setup file is plain Markdown. Write it as a numbered checklist of actions the assistant should perform or ask about. Example (`setup.md` for a mail bundle):

```markdown
# Himalaya mail bundle — setup

Guide the user through the following steps:

1. Ask which email provider to use (Gmail, Outlook, or custom IMAP/SMTP).
2. Collect the user's email address and app password — store them securely, never log them.
3. Write the Himalaya config file to `~/.config/himalaya/config.toml` using the collected values.
4. Run `himalaya account list` to verify the connection. If it fails, show the error and ask the user to correct the credentials.
5. Inform the user that the `himalaya-mail` subagent is now ready and requires the `himalaya-*` pattern in their role's `allowed_subagents` to be used.
```

### What happens if the setup file is missing

If `setup` is declared in `bundle.yaml` but the file does not exist at the declared path, the install still succeeds and the user receives a warning message explaining that setup must be completed manually.

### Re-running setup

There is no automatic re-run mechanism. To repeat setup, type a message such as:
```
/bundle install ./my-bundle.zip
```
Re-installing the bundle triggers the setup flow again.

---

## Installing a Bundle

Bundles install to `~/.relais/bundles/<bundle-name>/`. The base path can be overridden with the `RELAIS_HOME` environment variable.

### Method 1 — CLI

```bash
relais bundle install /path/to/my-bundle.zip
```

### Method 2 — Slash command

Type in any RELAIS chat channel:

```
/bundle install /path/to/my-bundle.zip
```

### Method 3 — TUI

Open the **Bundles** tab in the RELAIS TUI and use the file picker to select a `.zip` file, then click **Install**.

After installation completes, `watchfiles` detects the new directory and triggers an automatic hot-reload of the affected registries. No service restart is required.

---

## Uninstalling a Bundle

Uninstalling removes the bundle directory from `~/.relais/bundles/` and triggers hot-reload.

### CLI

```bash
relais bundle uninstall my-bundle
```

### Slash command

```
/bundle uninstall my-bundle
```

### TUI

Open the **Bundles** tab, locate the bundle in the list, and click **Uninstall**.

---

## Listing Installed Bundles

### CLI

```bash
relais bundle list
```

Sample output:

```
NAME            VERSION   AUTHOR                  DESCRIPTION
my-bundle       1.0.0     Alice <alice@example>   Adds calendar tools and a scheduling subagent.
another-bundle  -         -                       Extra MCP wrappers.
```

### Slash command

```
/bundle list
```

---

## Role-Gating for Subagents

Installing a bundle that contains subagents does not automatically grant any user access to them. Access is controlled by the `allowed_subagents` field on each role in `portail.yaml`.

The field accepts fnmatch patterns. Examples:

```yaml
roles:
  admin:
    allowed_subagents:
      - "*"                  # all subagents (including all bundles)

  power_user:
    allowed_subagents:
      - "my-bundle-*"        # only subagents whose names start with "my-bundle-"
      - "relais-config"      # plus the built-in config subagent

  standard:
    allowed_subagents: []    # no subagent access
```

A user whose role has no matching pattern for a given subagent will receive an authorization error if they attempt to invoke it.

---

## Creating a Bundle

### Step-by-step

1. **Create the root directory**, named after your bundle using the pattern `[a-z0-9][a-z0-9-]*`:

   ```bash
   mkdir my-bundle
   ```

2. **Write `bundle.yaml`** at the root:

   ```yaml
   name: my-bundle
   description: |
     Provides a scheduling subagent and a calendar tool.
   version: "1.0.0"
   author: "Alice <alice@example.com>"
   tools:
     - get_calendar_events
     - create_calendar_event
   ```

3. **Add components** as needed:

   ```bash
   # A global tool
   mkdir -p my-bundle/tools
   # → write my-bundle/tools/calendar_tool.py

   # A global skill
   mkdir -p my-bundle/skills/calendar-usage
   # → write my-bundle/skills/calendar-usage/SKILL.md

   # A subagent with local tools and skills
   mkdir -p my-bundle/subagents/scheduler/tools
   mkdir -p my-bundle/subagents/scheduler/skills/scheduler-ops
   # → write my-bundle/subagents/scheduler/subagent.yaml
   # → write my-bundle/subagents/scheduler/tools/scheduler_helpers.py
   # → write my-bundle/subagents/scheduler/skills/scheduler-ops/SKILL.md
   ```

4. **Package as ZIP**:

   ```bash
   zip -r my-bundle.zip my-bundle/
   ```

   The ZIP must contain exactly one root folder whose name matches `name` in `bundle.yaml`.

5. **Test locally**:

   ```bash
   relais bundle install ./my-bundle.zip
   relais bundle list
   ```

6. **Distribute** the `.zip` file.

### subagent.yaml skeleton

```yaml
name: scheduler                        # must match directory name
description: Books and manages events on behalf of the user.
system_prompt: |
  You are a scheduling assistant. Use your tools to read and write calendar events.
tools:
  - get_calendar_events                # static tool from ToolRegistry
  - mcp:calendar_*                     # MCP tools matching a glob
  - inherit                            # pass all MCP tools the parent agent received
delegation_snippet: |
  To schedule an event, delegate to the `scheduler` subagent with the event details.
```

---

## Security

The installer enforces the following constraints before extracting any archive.

### Zip bomb protection

Total uncompressed size of all members must not exceed **50 MB**. Archives exceeding this limit are rejected before extraction begins.

### Path traversal prevention

Every member path in the archive is validated. Members whose paths contain `../` or whose resolved extraction path falls outside the bundle's root folder are rejected, and the entire archive is refused.

### Module import validation

Tool modules (`tools/*.py`, `subagents/*/tools/*.py`) are loaded in an isolated pass before registration. Any module that attempts to import from a prefix not in the allowlist (`aiguilleur.channels.`, `atelier.tools.`, `relais_tools.`) is refused.

### Name constraints

`bundle.yaml` `name` must match `[a-z0-9][a-z0-9-]*` and must match the ZIP root folder name exactly. Mismatches cause the install to fail.

### Why these constraints exist

| Constraint | Threat mitigated |
|---|---|
| 50 MB size cap | Zip bomb / disk exhaustion |
| Path traversal check | Arbitrary file write outside bundle dir |
| Module import allowlist | Privilege escalation via tool code |
| Name validation | Ambiguous installs, directory injection |

---

## Conflict Resolution

When two installed bundles export a tool with the same name, the **bundle that sorts lexicographically last (alphabetically) wins**. A `WARNING` is emitted to the logs at load time:

```
WARNING ToolRegistry: bundle tool name conflict — 'get_calendar_events' from bundle 'my-bundle' 
        replaces previous registration (last-wins)
```

This means that if you have bundles named `calendar-v1` and `calendar-v2` both exporting the same tool, `calendar-v2` will be used (because it sorts after `calendar-v1` alphabetically).

To declare your bundle's tool names explicitly and surface conflicts earlier, list them in `bundle.yaml` under `tools:`. The installer compares this list against already-installed bundles and logs conflicts immediately upon install.

To resolve a conflict intentionally, either:
- Rename one of the conflicting bundles so it sorts after the other (e.g., rename `calendar-v1` to `calendar-v0-deprecated`)
- Uninstall the bundle whose version of the tool you do not want:

```bash
relais bundle uninstall calendar-v1
```
