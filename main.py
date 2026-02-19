from workflow_orchestrator import WorkflowOrchestrator
from pathlib import Path
import json
import yaml
import os

Agentic_workflow = WorkflowOrchestrator(project_root='.')
Package_location = 'SSIS_Packages'

# Run the SSIS Analyzer (Codex)
Agentic_workflow.run_agent_codex('ssis-package-analyzer', arguments=Package_location)

# Run Connection Manager Extractor (Claude)
Agentic_workflow.run_agent_claude('connection-manager', arguments=Package_location)

# Run DataFlow Extraction (Python)
ok, out, err = Agentic_workflow.run_python_script(
          '/Users/sam/CodeBase/genai-assisted-ssis-migration/scripts/migration/orchestrate_enhanced.py',
          args=[
              '--split-dir', 'Analysis_first_step/CONSULAR_ORBIS_40_Fact_split',
              '--group-by-container',
              '--show-control-flows',
              '--show-chains',
              '--save-yaml',
          ],
          timeout=3600,
      )


# Run I/O Mapping to Lakehouse 
ok, out, err = Agentic_workflow.run_python_script(
          'scripts/generate_connections_fabric.py',
          args=[
              '--project-dir', 'SSIS_Packages/CONSULAR_ORBIS',
              '--analysis-dir', 'Analysis_first_step',
              '--output', 'Analysis_first_step/connections_fabric.yaml',
          ],
          timeout=600,
      )


project_root = Path('.')
analysis_second = project_root / 'Analysis_second_step'
connection_mapper = project_root / 'Analysis_first_step' / 'connections_fabric.yaml'

if analysis_second.exists():
    planned_files = list(analysis_second.glob('*_planned.yml'))
    for planned_file in planned_files:
        try:
            with open(planned_file, 'r', encoding='utf-8') as f:
                planned = yaml.safe_load(f) or {}
        except Exception:
            planned = {}

        containers = planned.get('containers', {}) or {}
        for container_name, cdata in containers.items():
            exec_chains = (cdata or {}).get('execution_chains', []) or []
            for chain in exec_chains:
                chain_name = (chain or {}).get('chain_name', 'Chain_Unknown')
                chain_dir_name = chain_name.replace(' ', '_')
                save_dir = project_root / 'Analysis_third_step' / container_name / chain_dir_name
                save_dir.mkdir(parents=True, exist_ok=True)

                for step in (chain or {}).get('steps', []) or []:
                    if (step or {}).get('type') == 'data_flow':
                        xml_file = (step or {}).get('xml_file') or ''
                        candidate = Path(xml_file)
                        if not candidate.is_absolute():
                            candidate = project_root / xml_file
                        if not candidate.exists():
                            # Fallback: search by basename across project
                            basename = Path(xml_file).name
                            matches = list(project_root.glob(f'**/{basename}'))
                            if matches:
                                candidate = matches[0]

                        args_payload = {
                            "save_loc_dir": str(save_dir),
                            "task_xml_path": str(candidate),
                            "connection_mapper": str(connection_mapper),
                        }
                        print(f">>>>> Processing: {candidate}")
                        Agentic_workflow.run_agent_codex(
                            'ssis-fabric-migration-agent-compact',
                            arguments=json.dumps(args_payload)
                        )




# Combine Activities into Chains
#TODO

# Translate each Chain into pyspark Notebook
# TODO 
