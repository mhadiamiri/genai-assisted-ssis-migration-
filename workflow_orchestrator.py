#!/usr/bin/env python3
"""
SSIS Migration Workflow Orchestrator

This script orchestrates the complete SSIS migration workflow by:
1. Loading agent/command definitions from .claude/ directory
2. Formatting prompts with dynamic arguments
3. Calling claude-code or codex locally via bash commands
4. Chaining the 5-step workflow sequentially
"""

import os
import subprocess
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import yaml

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class WorkflowOrchestrator:
    """Coordinate the multi‑step SSIS migration workflow.

    This orchestrator provides helpers to:
    - Load agent and command definitions from the project's `.claude` folder
    - Interpolate runtime arguments into prompt templates
    - Invoke local AI tooling (e.g., `claude` CLI or `codex` CLI)
    - Execute Python scripts that implement analysis and migration utilities
    - Chain the end‑to‑end, 5‑step migration workflow

    The class assumes a specific repository layout where `.claude/agents` and
    `.claude/commands` contain Markdown files with YAML front matter and prompt
    bodies. Methods expose granular steps so callers may run individual phases
    or the complete workflow.
    """
    def __init__(self, project_root: str, ai_tool: str = "claude-code"):
        """Initialize the orchestrator.

        Parameters:
        - project_root: Path to the repository root used to resolve all
          relative paths (e.g., `.claude/agents`, `.claude/commands`, script
          locations, and analysis output directories).
        - ai_tool: Name of the AI tool to invoke for prompt execution. Accepted
          values are `"claude-code"` and `"codex"`.

        Raises:
        - FileNotFoundError: If `.claude/agents` or `.claude/commands` cannot
          be found under `project_root`.
        """
        self.project_root = Path(project_root)
        self.ai_tool = ai_tool  # "claude-code" or "codex"
        self.agents_dir = self.project_root / ".claude" / "agents"
        self.commands_dir = self.project_root / ".claude" / "commands"
        
        # Verify directories exist
        if not self.agents_dir.exists():
            raise FileNotFoundError(f"Agents directory not found: {self.agents_dir}")
        if not self.commands_dir.exists():
            raise FileNotFoundError(f"Commands directory not found: {self.commands_dir}")
    
    def list_agent_names(self) -> List[str]:
        """Return all agent names discovered under `.claude/agents`.

        The names are derived from `*.md` filenames without the extension.

        Returns:
        - A list of agent names (strings). The list is unsorted with respect to
          dependencies between agents; callers may apply their own ordering.
        """
        if not self.agents_dir.exists():
            return []
        return [p.stem for p in self.agents_dir.glob("*.md") if p.is_file()]

    # Note: Specific execution ordering is left to the developer. Use
    # `list_agent_names()` and apply any ordering rules externally.
    
    def load_agent_definition(self, agent_name: str) -> Dict:
        """Load and parse an agent definition file.

        Agent definitions live under `.claude/agents/{agent_name}.md` and
        typically include a YAML front matter block followed by a Markdown
        prompt body. This method parses the YAML into `metadata` and returns
        both the metadata and the raw prompt content.

        Parameters:
        - agent_name: The base name of the agent file (without extension).

        Returns:
        - A dictionary with keys:
          - `name` (str): The agent name.
          - `metadata` (dict): Parsed YAML metadata (may be empty if absent or
            unparsable).
          - `prompt` (str): The prompt Markdown content used for AI execution.

        Raises:
        - FileNotFoundError: If the agent file does not exist.
        """
        agent_file = self.agents_dir / f"{agent_name}.md"
        if not agent_file.exists():
            raise FileNotFoundError(f"Agent definition not found: {agent_file}")
        
        with open(agent_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Parse YAML front matter and markdown content
        parts = content.split('---', 2)
        if len(parts) >= 3:
            yaml_content = parts[1].strip()
            markdown_content = parts[2].strip()
            
            try:
                metadata = yaml.safe_load(yaml_content)
            except yaml.YAMLError as e:
                logger.warning(f"Could not parse YAML metadata for {agent_name}: {e}")
                metadata = {}
        else:
            metadata = {}
            markdown_content = content
        
        return {
            'name': agent_name,
            'metadata': metadata,
            'prompt': markdown_content
        }
    
    def load_command_definition(self, command_path: str) -> Dict:
        """Load and parse a command definition file.

        Command definitions live under `.claude/commands/{command_path}.md` and
        mirror the structure of agent files with YAML front matter and a
        Markdown prompt body.

        Parameters:
        - command_path: A path‑like stem under `.claude/commands` (without the
          `.md` extension). For example, `"SSIS/lang_translate"`.

        Returns:
        - A dictionary with keys:
          - `name` (str): The command path stem provided.
          - `metadata` (dict): Parsed YAML metadata (empty if absent or not
            parseable).
          - `prompt` (str): The prompt Markdown body for AI execution.

        Raises:
        - FileNotFoundError: If the command file does not exist.
        """
        command_file = self.commands_dir / f"{command_path}.md"
        if not command_file.exists():
            raise FileNotFoundError(f"Command definition not found: {command_file}")
        
        with open(command_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Parse YAML front matter and markdown content
        parts = content.split('---', 2)
        if len(parts) >= 3:
            yaml_content = parts[1].strip()
            markdown_content = parts[2].strip()
            
            try:
                metadata = yaml.safe_load(yaml_content)
            except yaml.YAMLError as e:
                logger.warning(f"Could not parse YAML metadata for {command_path}: {e}")
                metadata = {}
        else:
            metadata = {}
            markdown_content = content
        
        return {
            'name': command_path,
            'metadata': metadata,
            'prompt': markdown_content
        }
    
    def format_prompt(self, prompt_template: str, arguments: Optional[str] = None) -> str:
        """Interpolate runtime arguments into a prompt template.

        This performs a simple string replacement of the token `$ARGUMENTS`
        within the provided template.

        Parameters:
        - prompt_template: The prompt text containing an optional `$ARGUMENTS`
          placeholder.
        - arguments: Optional string value to substitute for `$ARGUMENTS`. When
          not provided, the `$ARGUMENTS` token is removed (replaced with an
          empty string).

        Returns:
        - The formatted prompt string with substitutions applied.
        """
        replacement = arguments if isinstance(arguments, str) else ""
        return prompt_template.replace('$ARGUMENTS', replacement)
    
    def run_ai_command(self, prompt: str, timeout: int = 3600, ai_tool: Optional[str] = None) -> Tuple[bool, str, str]:
        """Invoke the configured AI CLI with a prompt.

        Depending on `self.ai_tool`, this method shells out to either the
        `claude` CLI (via `echo | claude`) or the `codex` CLI. Output and
        errors are captured and returned to the caller.

        Parameters:
        - prompt: The full prompt text to pass to the AI tool.
        - timeout: Maximum number of seconds to allow the process to run
          before aborting with a timeout error.
        - ai_tool: Optional tool override for this call. If not provided,
          falls back to the orchestrator's default (`self.ai_tool`).

        Returns:
        - A tuple `(success, stdout, stderr)` where:
          - `success` (bool): True if the command returned exit code 0.
          - `stdout` (str): Captured standard output from the tool.
          - `stderr` (str): Captured standard error from the tool.

        Raises:
        - ValueError: If `self.ai_tool` is not one of the supported values.
        - No exception is raised on command failure; instead, `success` will be
          False. Unexpected runtime errors are caught and summarized in the
          returned tuple.
        """
        
        tool = (ai_tool or self.ai_tool or "").strip().lower()
        if tool == "claude":
            tool = "claude-code"

        if tool == "claude-code":
            # Use echo + pipe for claude
            cmd = f'echo "{prompt}" | claude --print --allowedTools "Edit,Bash"'
            shell_mode = True
        elif tool == "codex":
            cmd = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", prompt]
            shell_mode = False
        else:
            raise ValueError(f"Unsupported AI tool: {ai_tool or self.ai_tool}")
        
        logger.info(f"Running {tool} command...")
        if shell_mode:
            logger.debug(f"Command: echo [PROMPT_TRUNCATED] | claude --print --allowedTools \"Edit,Bash\"")
        else:
            logger.debug(f"Command: {' '.join(cmd[:2])} [PROMPT_TRUNCATED]")
        
        try:
            # Run the command with timeout
            result = subprocess.run(
                cmd,
                shell=shell_mode,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr
            
            if not success:
                logger.error(f"Command failed with return code {result.returncode}")
                logger.error(f"STDERR: {stderr}")
            
            return success, stdout, stderr
            
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out after {timeout} seconds")
            return False, "", "Command timed out"
        except Exception as e:
            logger.error(f"Error running command: {e}")
            return False, "", str(e)
    
    def run_python_script(self, script_path: str, args: List[str] = None, timeout: int = 3600) -> Tuple[bool, str, str]:
        """Execute a Python script within the project context.

        This method runs `python <script_path> [args...]` with the current
        working directory set to `project_root`. It captures stdout/stderr and
        returns them with a success flag.

        Parameters:
        - script_path: Path to the Python script relative to `project_root`.
        - args: Optional list of command‑line arguments to pass to the script.

        Returns:
        - A tuple `(success, stdout, stderr)` with the script's exit status and
          captured output streams.

        Raises:
        - FileNotFoundError: If `script_path` does not exist.
        - No exception is raised on a non‑zero script exit; `success` will be
          False and `stderr` will contain details. Unexpected runtime errors are
          caught and summarized in the returned tuple.
        """
        if args is None:
            args = []
        
        script_full_path = self.project_root / script_path
        if not script_full_path.exists():
            raise FileNotFoundError(f"Script not found: {script_full_path}")
        
        cmd = ["python", str(script_full_path)] + args
        
        logger.info(f"Running Python script: {script_path}")
        logger.debug(f"Command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            
            success = result.returncode == 0
            stdout = result.stdout
            stderr = result.stderr
            
            if not success:
                logger.error(f"Script failed with return code {result.returncode}")
                logger.error(f"STDERR: {stderr}")
            else:
                logger.info(f"Script completed successfully")
                if stdout:
                    logger.info(f"STDOUT: {stdout}")
            
            return success, stdout, stderr
            
        except subprocess.TimeoutExpired:
            logger.error(f"Script timed out after {timeout} seconds")
            return False, "", "Script timed out"
        except Exception as e:
            logger.error(f"Error running script: {e}")
            return False, "", str(e)
    
    def run_agent(self, agent_name: str, arguments: Optional[str] = None) -> bool:
        """Load an agent, format its prompt, and run it.

        A convenience wrapper that combines loading the agent definition,
        substituting `$ARGUMENTS`, and invoking the AI tool.

        Parameters:
        - agent_name: The agent definition name under `.claude/agents`.
        - arguments: Optional string to substitute for `$ARGUMENTS` in the
          agent prompt.

        Returns:
        - True if the AI command completed with exit code 0; otherwise False.

        Notes:
        - Uses the orchestrator's default backend selected at construction
          time. To choose a backend per call, use
          `run_agent_claude`, `run_agent_codex`, or `run_agent_with_tool`.
        """
        logger.info(f"=== Running Agent: {agent_name} ===")
        
        try:
            # Load agent definition
            agent_def = self.load_agent_definition(agent_name)

            # Format prompt with arguments
            formatted_prompt = self.format_prompt(agent_def['prompt'], arguments)
            
            # Run the AI command using the orchestrator's default backend
            success, stdout, stderr = self.run_ai_command(formatted_prompt)
            
            if success:
                logger.info(f"Agent {agent_name} completed successfully")
                if stdout:
                    logger.info(f"Output: {stdout}")
                return True
            else:
                logger.error(f"Agent {agent_name} failed")
                return False
                
        except Exception as e:
            logger.error(f"Error running agent {agent_name}: {e}")
            return False

    def run_agent_with_tool(self, agent_name: str, arguments: Optional[str] = None, ai_tool: str = "claude-code") -> bool:
        """Run a single agent using the specified backend tool.

        Parameters:
        - agent_name: The agent definition name under `.claude/agents`.
        - arguments: Optional string to substitute for `$ARGUMENTS` in the
          agent prompt.
        - ai_tool: Backend to use; `"claude"`/`"claude-code"` or `"codex"`.

        Returns:
        - True if the AI command completed successfully; otherwise False.
        """
        logger.info(f"=== Running {ai_tool} Agent: {agent_name} ===")
        try:
            agent_def = self.load_agent_definition(agent_name)
            formatted_prompt = self.format_prompt(agent_def['prompt'], arguments)
            success, stdout, stderr = self.run_ai_command(formatted_prompt, ai_tool=ai_tool)
            if success:
                logger.info(f"Agent {agent_name} completed successfully")
                if stdout:
                    logger.info(f"Output: {stdout}")
                return True
            logger.error(f"Agent {agent_name} failed")
            return False
        except Exception as e:
            logger.error(f"Error running agent {agent_name} with tool {ai_tool}: {e}")
            return False

    def run_agent_claude(self, agent_name: str, arguments: Optional[str] = None) -> bool:
        """Run a single agent using the Claude backend."""
        return self.run_agent_with_tool(agent_name, arguments, ai_tool="claude-code")

    def run_agent_codex(self, agent_name: str, arguments: Optional[str] = None) -> bool:
        """Run a single agent using the Codex backend."""
        return self.run_agent_with_tool(agent_name, arguments, ai_tool="codex")

    # Batch execution helpers were removed to keep the orchestrator minimal.
    # Developers should compose their own workflows explicitly.
    
    def run_command(self, command_path: str, arguments: Optional[str] = None) -> bool:
        """Load a command, format its prompt, and run it.

        Similar to `run_agent` but looks up definitions under
        `.claude/commands` using a path‑like stem.

        Parameters:
        - command_path: Path stem to the command definition under
          `.claude/commands` (without `.md`).
        - arguments: Optional string to substitute for `$ARGUMENTS` in the
          command prompt.

        Returns:
        - True if the AI command completed successfully; otherwise False.

        Notes:
        - Uses the orchestrator's default backend selected at construction
          time. To choose a backend per call, use
          `run_command_claude` or `run_command_codex`.
        """
        logger.info(f"=== Running Command: {command_path} ===")
        
        try:
            # Load command definition
            cmd_def = self.load_command_definition(command_path)
            
            # Format prompt with arguments
            formatted_prompt = self.format_prompt(cmd_def['prompt'], arguments)
            
            # Run the AI command using the orchestrator's default backend
            success, stdout, stderr = self.run_ai_command(formatted_prompt)
            
            if success:
                logger.info(f"Command {command_path} completed successfully")
                if stdout:
                    logger.info(f"Output: {stdout}")
                return True
            else:
                logger.error(f"Command {command_path} failed")
                return False
                
        except Exception as e:
            logger.error(f"Error running command {command_path}: {e}")
            return False

    def run_command_with_tool(self, command_path: str, arguments: Optional[str] = None, ai_tool: str = "claude-code") -> bool:
        """Run a command using the specified backend tool.

        Parameters:
        - command_path: Path stem to the command definition under
          `.claude/commands` (without `.md`).
        - arguments: Optional string to substitute for `$ARGUMENTS` in the
          command prompt.
        - ai_tool: Backend to use; `"claude"`/`"claude-code"` or `"codex"`.

        Returns:
        - True if the command completed successfully; otherwise False.
        """
        logger.info(f"=== Running Command with {ai_tool}: {command_path} ===")
        try:
            cmd_def = self.load_command_definition(command_path)
            formatted_prompt = self.format_prompt(cmd_def['prompt'], arguments)
            success, stdout, stderr = self.run_ai_command(formatted_prompt, ai_tool=ai_tool)
            if success:
                logger.info(f"Command {command_path} completed successfully")
                if stdout:
                    logger.info(f"Output: {stdout}")
                return True
            logger.error(f"Command {command_path} failed")
            return False
        except Exception as e:
            logger.error(f"Error running command {command_path} with tool {ai_tool}: {e}")
            return False

    def run_command_claude(self, command_path: str, arguments: Optional[str] = None) -> bool:
        """Run a command using the Claude backend."""
        return self.run_command_with_tool(command_path, arguments, ai_tool="claude-code")

    def run_command_codex(self, command_path: str, arguments: Optional[str] = None) -> bool:
        """Run a command using the Codex backend."""
        return self.run_command_with_tool(command_path, arguments, ai_tool="codex")
    
    # The previous hardcoded 5-step workflow has been removed. Compose your
    # workflow explicitly in your own entry point or in `main()` below.


def main():
    """Minimal developer-driven entry point.

    This module is intended to be used programmatically. Define your workflow
    explicitly by calling the orchestrator methods for the agents and tools you
    need (e.g., `run_agent_claude` or `run_agent_codex`). The default `main()`
    does not parse CLI arguments or run a fixed workflow.

    Example usage:
        orchestrator = WorkflowOrchestrator(project_root='.', ai_tool='claude-code')
        input_dir = 'SSIS_Packages'
        arguments = str(Path(input_dir).resolve())
        orchestrator.run_agent_claude('ssis-package-analyzer', arguments)
        orchestrator.run_agent_codex('ssis-connection-manager', arguments)
        orchestrator.run_command_claude('SSIS/lang_translate', args_str)
        orchestrator.run_command_codex('some/other_command', args_str)
    """
    # logger.info("No default CLI. Define your workflow in code using WorkflowOrchestrator.")



if __name__ == "__main__":
    main()
