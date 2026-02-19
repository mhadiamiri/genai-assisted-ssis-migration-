# GenAI Assisted SSIS Migration

This repository contains utilities to analyze and decompose SSIS packages, inspect and validate control/data‑flow DAGs, and orchestrate agent‑driven migration and translation tasks using local AI tools (Claude CLI or Codex CLI). It also includes a small, developer‑oriented `workflow_orchestrator.py` to load prompts from `.claude/` and execute them programmatically.

> ⚠️ **ATTENTION:** At the time the code was pushed, Claude Code was the only assistant that supported multi-agents (subagents). The file `workflow_orchestrator.py` simply provides a mechanism to invoke other assistants—such as Codex—in an interactive way. This approach ensures that each task runs within its own clean context. It’s important to note that this is just a workflow. There is no central or master agent interacting directly with the user to collect prompts. Instead, the orchestrator routes tasks to the appropriate subagent.

## Directory Layout

- `.claude/`
  - `agents/` – Markdown agents with YAML front matter + prompt body. These are free‑form prompts that may use `$ARGUMENTS` as input.
  - `commands/` – Markdown commands grouped by domain (e.g., `SSIS/`). Also use YAML front matter + prompt body, and may use `$ARGUMENTS`.
  - `settings.local.json` – Local settings (not required for the orchestrator).
- `scripts/`
  - `1_package_analysis/` – SSIS splitter and helpers:
    - `split_ssis_package.py` – Split `.dtsx` into structured artifacts (data flows, control flows, connections, variables, and DAG).
    - `extract_control_flow_tasks.py` – Utility to extract control flow tasks from package XML.
  - `migration/` – Orchestration and analysis:
    - `orchestrate.py` – Basic, CLI‑driven DAG runner (Python modules + optional SSIS fallback).
    - `orchestrate_enhanced.py` – Enhanced analysis and YAML export; container grouping and control‑flow visualization.
    - `analyze_dag.py` – DAG inspection utilities.
    - `validate_dependencies.py` – Validates DAG ordering and dependencies.
    - `utils.py` – Helpers used by the migration tools.
    - `test_orchestrator.py` – Example tests for orchestrator behavior.
  - `combine_chain_yamls.py` – Combine YAML outputs from enhanced analysis.
  - `generate_connections_fabric.py` – Sample utility to help map connections.
- `SSIS_Packages/` – Example input packages and connection manager files.
- `workflow_orchestrator.py` – Developer‑focused agent/command runner for `.claude` prompts.

## .claude Agents

Agents are Markdown files in `.claude/agents/*.md` with optional YAML front matter followed by a free‑form prompt body. They may include the placeholder `$ARGUMENTS`, which can be replaced at run time by the orchestrator.

Discovered agents in this repo:
- `ssis-package-analyzer.md` – Analyze a package tree and generate a summary.
- `connection-manager.md` – Inspect and summarize connection managers.
- `ssis-fabric-migration-agent.md` – Guidance and patterns for migrating SSIS to Fabric‑style flows.

Key points:
- Front matter is parsed, but backend selection is NOT read from YAML; you choose the backend (Claude/Codex) when calling the orchestrator.
- `$ARGUMENTS` is optional; if not supplied, the token is removed when formatting the prompt.

## .claude Commands

Commands mirror agents but are organized by topic/path under `.claude/commands/`. They also support `$ARGUMENTS` substitution.

Included example:
- `SSIS/lang_translate.md` – Converts analysis YAMLs into downstream representation, reports, or code stubs.

## Scripts Overview

Core workflows and their primary scripts:

1) Split a package into artifacts
- `scripts/1_package_analysis/split_ssis_package.py`
- Usage:
  - `python scripts/1_package_analysis/split_ssis_package.py SSIS_Packages/<Package>.dtsx --out-dir Analysis_first_step/<Package>_split`

2) Analyze and orchestrate the DAG (CLI tools)
- `scripts/migration/orchestrate.py` – list and execute DAG with optional SSIS fallback
- `scripts/migration/orchestrate_enhanced.py` – enhanced analysis, grouping, and YAML export
- Example commands:
  - List execution order: `python scripts/migration/orchestrate.py --package SSIS_Packages/<Package>.dtsx --split-dir Analysis_first_step/<Package>_split --list -v`
  - Validate dependencies: `python scripts/migration/validate_dependencies.py --split-dir Analysis_first_step/<Package>_split -v`
  - Analyze DAG: `python scripts/migration/analyze_dag.py --split-dir Analysis_first_step/<Package>_split --format text`
  - Enhanced YAML: `python scripts/migration/orchestrate_enhanced.py --split-dir Analysis_first_step/<Package>_split --group-by-container --show-control-flows --save-yaml`

3) Post‑processing and utilities
- `scripts/combine_chain_yamls.py` – combines YAML chain outputs from enhanced analysis.
- `scripts/generate_connections_fabric.py` – generates mappings for connection managers (sample).

## workflow_orchestrator.py

`workflow_orchestrator.py` provides a thin, developer‑oriented API to execute `.claude` agents and commands with either Claude CLI or Codex CLI. It does not expose a CLI; you compose your workflow in code.

Key capabilities:
- Load definitions:
  - `load_agent_definition(agent_name) -> {name, metadata, prompt}`
  - `load_command_definition(command_path) -> {name, metadata, prompt}`
- Prompt formatting:
  - `format_prompt(prompt_template, arguments=None)` – Replaces `$ARGUMENTS` with the provided string or removes it if `None`.
- Execution helpers:
  - `run_ai_command(prompt, timeout=300, ai_tool=None)` – Runs via Claude or Codex; captures stdout/stderr.
  - `run_python_script(script_path, args=None)` – Executes Python utilities relative to the project root.
- Agent runners:
  - `run_agent(agent_name, arguments=None)` – Uses the orchestrator’s default backend.
  - `run_agent_claude(agent_name, arguments=None)` – Force Claude.
  - `run_agent_codex(agent_name, arguments=None)` – Force Codex.
- Command runners:
  - `run_command(command_path, arguments=None)` – Uses default backend.
  - `run_command_claude(command_path, arguments=None)` – Force Claude.
  - `run_command_codex(command_path, arguments=None)` – Force Codex.
- Discovery:
  - `list_agent_names() -> List[str]`

Constructor:
- `WorkflowOrchestrator(project_root: str, ai_tool: str = "claude-code")`
  - `project_root` is used to resolve `.claude/agents`, `.claude/commands`, and scripts under `scripts/`.
  - `ai_tool` sets the default backend for `run_agent` / `run_command` (use explicit methods to override per call).

Return values and errors:
- All run methods return `(success, stdout, stderr)` internally and surface a boolean `True/False` at the convenience wrappers.
- Non‑zero exits do not raise; they return `False` and log stderr. Missing files raise `FileNotFoundError` early.

### Usage Examples

Run two agents with different backends and then invoke a command:

```python
from pathlib import Path
from workflow_orchestrator import WorkflowOrchestrator

orchestrator = WorkflowOrchestrator(project_root='.', ai_tool='claude-code')  # default

# Arguments are optional; when provided, `$ARGUMENTS` in prompts is replaced
args_str = str(Path('SSIS_Packages').resolve())

# Run agents with explicit backends
orchestrator.run_agent_claude('ssis-package-analyzer', arguments=args_str)
orchestrator.run_agent_codex('connection-manager', arguments=args_str)

# Run a command with Claude
orchestrator.run_command_claude('SSIS/lang_translate', arguments='Analysis_second_step')

# Or run with the default backend configured at construction
orchestrator.run_agent('ssis-fabric-migration-agent', arguments=None)
```

Execute a Python helper script from this repo:

```python
ok, out, err = orchestrator.run_python_script(
    'scripts/migration/orchestrate_enhanced.py',
    [
        '--split-dir', 'Analysis_first_step/<Package>_split',
        '--group-by-container',
        '--show-control-flows',
        '--save-yaml',
    ],
)
```

### Logging

- Logging is enabled globally via the `logging` module. By default it logs at INFO level; set the root logger to DEBUG in your entrypoint for more detail.
- `run_ai_command` logs which backend is called and captures stdout/stderr. Future enhancements may add line‑by‑line streaming and per‑run log files under a `.logs/` folder.

### Timeouts

- Default timeout is 3600 seconds (about 60 minutes) for:
  - `run_ai_command(prompt, timeout=3600, ai_tool=...)`
  - `run_python_script(script_path, args=None, timeout=3600)`
- You can override per call by passing a different `timeout` value.
- On timeout, the orchestrator logs a clear message and returns `False` with `stderr` set to a timeout description.

## Typical End‑to‑End Flow

1. Split the SSIS package:
   - `python scripts/1_package_analysis/split_ssis_package.py SSIS_Packages/<Package>.dtsx --out-dir Analysis_first_step/<Package>_split`
2. Perform enhanced analysis and export YAML:
   - `python scripts/migration/orchestrate_enhanced.py --split-dir Analysis_first_step/<Package>_split --group-by-container --show-control-flows --save-yaml`
3. Optionally validate dependencies:
   - `python scripts/migration/validate_dependencies.py --split-dir Analysis_first_step/<Package>_split -v`
4. Use agents/commands via `workflow_orchestrator.py` to translate, refactor, or scaffold migrated code.
5. Combine YAML artifacts for downstream processing:
   - `python scripts/combine_chain_yamls.py`

## Environment Setup

- Python 3.12+
- Tooling: this repo uses `uv` (fast Python package manager) with `pyproject.toml` and `uv.lock`.

### Quick start with uv (recommended)

- Install uv (see https://docs.astral.sh/uv/ for platform options). Example:
  - macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  - Windows (PowerShell): `iwr https://astral.sh/uv/install.ps1 -UseBasicParsing | iex`
- Create and sync a virtual environment from `pyproject.toml`/`uv.lock`:
  - `uv sync`
    - Creates `.venv/` (if missing) and installs all dependencies.
- Activate the venv:
  - macOS/Linux: `source .venv/bin/activate`
  - Windows (PowerShell): `.venv\Scripts\Activate.ps1`
- Editable install / development extras (if needed):
  - `uv pip install -e .`
- Run scripts with the environment:
  - `uv run python scripts/1_package_analysis/split_ssis_package.py <args>`

### Classic venv + pip (alternative)

- Create a venv and install project (editable):
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install -e .`

### Prerequisites: AI tools (Claude CLI / Codex CLI)

- To run agents/commands via the orchestrator, install at least one of:
  - Claude CLI (aka "claude-code")
  - Codex CLI
- Verify installation:
  - `claude --help`  (for Claude)
  - `codex --help`   (for Codex)
- Choose the backend per call in code:
  - Claude: `orchestrator.run_agent_claude('agent-name', arguments)` or `run_command_claude('SSIS/lang_translate', arguments)`
  - Codex: `orchestrator.run_agent_codex('agent-name', arguments)` or `run_command_codex('SSIS/lang_translate', arguments)`
- If only one tool is installed, use the corresponding methods. If both are installed, you can mix them within a workflow.

## Security and Configuration

- Do not commit secrets or real connection strings from `.conmgr` or variables. Prefer environment variables or redacted samples.
- Large SSIS/XML artifacts should be referenced, not embedded in PRs when possible.
- If using Spark for migrated flows, assume a `spark` session is provided by the runtime (e.g., Databricks) and avoid local secrets in code.

## Roadmap / Next Steps

- Containerized AI runtime
  - Run Claude CLI and Codex CLI inside Docker images for reproducible environments and easier onboarding. Provide images, compose files, and wrapper scripts to route orchestrator calls into containers.
- Local Spark session for testing
  - Add a lightweight Spark initialization utility and fixtures to spin up a `SparkSession` in unit/integration tests for migrated flows (no secrets; local mode by default).
- Scripts refactor
  - Review and reorganize `scripts/` for clarity and cohesion. Consolidate overlapping utilities, improve naming, and align CLI options and outputs across tools.
- Documentation consolidation
  - Stitch the SSIS package documentation from the [previous repository](https://github.com/Revolution-Data-Platforms/SSIS_Migration_Assistant) and link it here for a single source of truth.
