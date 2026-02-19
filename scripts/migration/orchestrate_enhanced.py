#!/usr/bin/env python3
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Set, Optional
import xml.etree.ElementTree as ET
import yaml
from utils import load_dag, topo_order, try_import_migrated_module, run_dtexec, load_variables, find_dataflow_xml


class ControlFlowTask:
    def __init__(self, name: str, task_type: str, dtsid: str, container_path: str = ""):
        self.name = name
        self.task_type = task_type
        self.dtsid = dtsid
        self.container_path = container_path
        self.sql_statement = None
        self.connection_id = None
        self.precedence_constraints = []
        
    def __repr__(self):
        return f"ControlFlowTask(name='{self.name}', type='{self.task_type}', container='{self.container_path}')"


def parse_control_flow_xml(xml_path: Path) -> ControlFlowTask:
    """Parse a control flow XML file and extract task information."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    # Extract basic task info from root attributes
    name = root.get('name', '')
    task_type = root.get('taskType', '')
    dtsid = root.get('dtsid', '')
    container_path = root.get('containerPath', '')
    
    task = ControlFlowTask(name, task_type, dtsid, container_path)
    
    # Extract SQL statement for ExecuteSQL tasks
    if task_type == 'ExecuteSQL':
        sql_data = root.find('.//{www.microsoft.com/sqlserver/dts/tasks/sqltask}SqlTaskData')
        if sql_data is not None:
            task.sql_statement = sql_data.get('{www.microsoft.com/sqlserver/dts/tasks/sqltask}SqlStatementSource', '')
            task.connection_id = sql_data.get('{www.microsoft.com/sqlserver/dts/tasks/sqltask}Connection', '')
    
    # Extract precedence constraints for sequence containers
    if task_type == 'SequenceContainer':
        ns = {'DTS': 'www.microsoft.com/SqlServer/Dts'}
        precedence_constraints = root.findall('.//DTS:PrecedenceConstraint', ns)
        for constraint in precedence_constraints:
            from_ref = constraint.get('{www.microsoft.com/SqlServer/Dts}From', '')
            to_ref = constraint.get('{www.microsoft.com/SqlServer/Dts}To', '')
            constraint_name = constraint.get('{www.microsoft.com/SqlServer/Dts}ObjectName', '')
            task.precedence_constraints.append({
                'from': from_ref,
                'to': to_ref,
                'name': constraint_name
            })
    
    return task


def load_control_flows(split_dir: Path) -> Dict[str, ControlFlowTask]:
    """Load all control flow tasks from the controlflows directory."""
    controlflows_dir = split_dir / 'controlflows'
    control_flows = {}
    
    if not controlflows_dir.exists():
        return control_flows
    
    for xml_file in controlflows_dir.glob('*.xml'):
        try:
            task = parse_control_flow_xml(xml_file)
            control_flows[task.dtsid] = task
        except ET.ParseError as e:
            print(f"Warning: Could not parse {xml_file}: {e}")
        except Exception as e:
            print(f"Warning: Error processing {xml_file}: {e}")
    
    return control_flows


def analyze_container_control_flows(container_name: str, control_flows: Dict[str, ControlFlowTask]) -> Dict[str, List[ControlFlowTask]]:
    """Analyze control flows within a specific container."""
    container_tasks = {}
    
    for task in control_flows.values():
        if task.container_path == container_name or task.name == container_name:
            task_type = task.task_type
            if task_type not in container_tasks:
                container_tasks[task_type] = []
            container_tasks[task_type].append(task)
    
    return container_tasks


def extract_truncate_tables(sql_statement: str) -> List[str]:
    """Extract table names from TRUNCATE TABLE statements."""
    if not sql_statement:
        return []
    
    tables = []
    lines = sql_statement.split('\n')
    for line in lines:
        line = line.strip()
        if line.upper().startswith('TRUNCATE TABLE'):
            # Extract table name (remove TRUNCATE TABLE and semicolon)
            table_part = line[14:].strip()  # Remove "TRUNCATE TABLE "
            if table_part.endswith(';'):
                table_part = table_part[:-1]
            # Remove dbo. prefix if present
            if table_part.startswith('dbo.'):
                table_part = table_part[4:]
            tables.append(table_part)
        elif 'truncate table' in line.lower():
            # Handle variations like "truncate table  table_name"
            truncate_idx = line.lower().find('truncate table')
            if truncate_idx >= 0:
                table_part = line[truncate_idx + 14:].strip()  # Remove "truncate table "
                if table_part.endswith(';'):
                    table_part = table_part[:-1]
                # Remove dbo. prefix if present
                if table_part.startswith('dbo.'):
                    table_part = table_part[4:]
                if table_part:  # Only add non-empty table names
                    tables.append(table_part)
    
    return tables


def find_dataflow_xml_file(split_dir: Path, dtsid: str, flow_name: str) -> str:
    """Find the XML file for a specific data flow."""
    dataflows_dir = split_dir / 'dataflows'
    if not dataflows_dir.exists():
        return f"[XML not found]"
    
    # Try to find by DTSID first
    for xml_file in dataflows_dir.glob('*.xml'):
        if dtsid.replace('{', '').replace('}', '') in xml_file.name:
            return f"[{xml_file.name}]"
    
    # Try to find by flow name
    for xml_file in dataflows_dir.glob('*.xml'):
        if flow_name.replace(' ', '_').replace('-', '_') in xml_file.name:
            return f"[{xml_file.name}]"
    
    return f"[XML not found for {flow_name}]"


def find_controlflow_xml_file(split_dir: Path, dtsid: str, task_name: str) -> str:
    """Find the XML file for a specific control flow task."""
    controlflows_dir = split_dir / 'controlflows'
    if not controlflows_dir.exists():
        return f"[XML not found]"
    
    # Try to find by DTSID first
    clean_dtsid = dtsid.replace('{', '').replace('}', '')
    for xml_file in controlflows_dir.glob('*.xml'):
        if clean_dtsid in xml_file.name:
            return f"[{xml_file.name}]"
    
    # Try to find by task name
    clean_task_name = task_name.replace(' ', '_').replace('-', '_')
    for xml_file in controlflows_dir.glob('*.xml'):
        if clean_task_name in xml_file.name:
            return f"[{xml_file.name}]"
    
    return f"[XML not found for {task_name}]"


def parse_args():
    p = argparse.ArgumentParser(description='Enhanced orchestration with control flow integration')
    p.add_argument('--package', help='Path to original .dtsx package (for fallback)')
    p.add_argument('--split-dir', required=True, help='Path to the split output directory for the package')
    p.add_argument('--module-base', default='migrated', help='Base Python package containing migrated modules')
    
    # Execution modes
    p.add_argument('--list', action='store_true', help='Only list the execution order (no run)')
    p.add_argument('--run-ssis-fallback', action='store_true', help='Run SSIS for nodes without a migrated module')
    
    # Analysis modes
    p.add_argument('--validate', action='store_true', help='Validate dependency ordering before execution')
    p.add_argument('--show-chains', action='store_true', help='Show dependency chains in the DAG')
    p.add_argument('--group-by-container', action='store_true', help='Group flows by original sequence containers')
    p.add_argument('--show-parallel', action='store_true', help='Show nodes that can execute in parallel')
    p.add_argument('--show-control-flows', action='store_true', help='Show control flow tasks for each container')
    
    # Output options
    p.add_argument('--verbose', '-v', action='store_true', help='Show detailed information')
    p.add_argument('--save-yaml', action='store_true', help='Save analysis as YAML file to Analysis_second_step directory')
    
    return p.parse_args()


def extract_container_name(ref_id: str) -> str:
    """Extract sequence container name from a reference ID."""
    parts = ref_id.split('\\')
    if len(parts) >= 2:
        return parts[1]  # Second part is usually the sequence container
    return "Unknown"


def build_dependency_chains(edges: List[Tuple[str, str, Dict[str, str]]], all_dag_nodes: Set[str] = None, control_flows: Dict[str, ControlFlowTask] = None) -> Dict[str, List[str]]:
    """Build dependency chains showing node execution sequences."""
    # Build adjacency list
    adj = {}
    in_degree = {}
    
    # Start with all DAG nodes if provided, otherwise use nodes from edges
    if all_dag_nodes:
        all_nodes = all_dag_nodes.copy()
        # Initialize in_degree for all nodes
        for node in all_nodes:
            in_degree[node] = 0
    else:
        all_nodes = set()
    
    for from_node, to_node, _ in edges:
        all_nodes.add(from_node)
        all_nodes.add(to_node)
        
        if from_node not in adj:
            adj[from_node] = []
        adj[from_node].append(to_node)
        
        in_degree[to_node] = in_degree.get(to_node, 0) + 1
        if from_node not in in_degree:
            in_degree[from_node] = 0
    
    # Find root nodes (no incoming edges)
    root_nodes = [node for node in all_nodes if in_degree.get(node, 0) == 0]
    
    # Build chains from each root
    chains = {}
    
    def build_chain(node: str) -> List[str]:
        chain = [node]
        current = node
        
        # Follow the longest path
        while current in adj:
            # If multiple children, pick the first one (could be enhanced)
            if adj[current]:
                current = adj[current][0]
                chain.append(current)
            else:
                break
        
        return chain
    
    for root in root_nodes:
        chains[root] = build_chain(root)
    
    return chains


def build_enhanced_container_chains(container_name: str, data_flow_chains: Dict[str, List[str]], control_flows: Dict[str, ControlFlowTask], dag, split_dir: Path) -> Dict[str, List[dict]]:
    """Build enhanced chains that include control flow tasks with data flows."""
    enhanced_chains = {}
    
    # Get control flow tasks for this container
    container_control_flows = analyze_container_control_flows(container_name, control_flows)
    
    # Get ExecuteSQL tasks (typically TRUNCATE operations)
    truncate_tasks = container_control_flows.get('ExecuteSQL', [])
    
    # For each data flow chain in the container, prepend relevant control flows
    chain_num = 1
    for chain_root, chain_nodes in data_flow_chains.items():
        # Check if any node in the chain belongs to this container
        chain_in_container = []
        for chain_node in chain_nodes:
            if extract_container_name(chain_node) == container_name:
                chain_in_container.append(chain_node)
        
        if chain_in_container:
            enhanced_chain = []
            
            # Add relevant ExecuteSQL tasks first
            if truncate_tasks:
                # For the first chain, show all truncate operations
                if chain_num == 1:
                    for truncate_task in truncate_tasks:
                        tables = extract_truncate_tables(truncate_task.sql_statement) if truncate_task.sql_statement else []
                        xml_link = find_controlflow_xml_file(split_dir, truncate_task.dtsid, truncate_task.name)
                        enhanced_chain.append({
                            'type': 'control_flow',
                            'task_type': 'ExecuteSQL',
                            'name': truncate_task.name,
                            'operation': 'TRUNCATE',
                            'tables': tables,
                            'xml_link': xml_link
                        })
                else:
                    # For subsequent chains, reference the common truncate step
                    enhanced_chain.append({
                        'type': 'reference',
                        'name': '⤴ Common TRUNCATE step',
                        'reference_to': 'Chain 1 ExecuteSQL operations'
                    })
            
            # Add data flow tasks with XML file links
            for data_flow_ref in chain_in_container:
                data_flow_node = dag.nodes[data_flow_ref]
                xml_link = find_dataflow_xml_file(split_dir, data_flow_node.dtsid, data_flow_node.name)
                enhanced_chain.append({
                    'type': 'data_flow',
                    'name': data_flow_node.name,
                    'dtsid': data_flow_node.dtsid,
                    'ref': data_flow_ref,
                    'xml_link': xml_link
                })
            
            enhanced_chains[f"Chain {chain_num}"] = enhanced_chain
            chain_num += 1
    
    return enhanced_chains


def find_parallel_nodes(edges: List[Tuple[str, str, Dict[str, str]]], all_nodes: Set[str]) -> List[str]:
    """Find nodes that can execute in parallel (no dependencies between them)."""
    in_degree = {}
    for node in all_nodes:
        in_degree[node] = 0
    
    for from_node, to_node, _ in edges:
        in_degree[to_node] += 1
    
    # Nodes with no dependencies can start in parallel
    parallel_nodes = [node for node, degree in in_degree.items() if degree == 0]
    return parallel_nodes


def validate_dependencies(ordered_nodes: List[str], edges: List[Tuple[str, str, Dict[str, str]]]) -> List[str]:
    """Validate that ordered nodes respect all dependencies. Returns violations."""
    node_positions = {node: i for i, node in enumerate(ordered_nodes)}
    violations = []
    
    for from_node, to_node, _ in edges:
        if from_node in node_positions and to_node in node_positions:
            if node_positions[from_node] >= node_positions[to_node]:
                violations.append(f"{from_node} -> {to_node}")
    
    return violations


def extract_package_name(split_dir: Path) -> str:
    """Extract package name from split directory path."""
    # Example: Analysis_first_step/CONSULAR_ORBIS_10_Landing_split -> CONSULAR_ORBIS_10_Landing
    dir_name = split_dir.name
    if dir_name.endswith('_split'):
        return dir_name[:-6]  # Remove '_split' suffix
    return dir_name


def build_yaml_data(container_groups: Dict[str, List[str]], enhanced_chains_data: Dict[str, Dict], dag, split_dir: Path) -> Dict:
    """Build YAML data structure without emojis."""
    package_name = extract_package_name(split_dir)
    
    yaml_data = {
        'package_name': package_name,
        'total_containers': len(container_groups),
        'total_flows': sum(len(flows) for flows in container_groups.values()),
        'containers': {}
    }
    
    for container_name, flows in container_groups.items():
        container_data = {
            'flow_count': len(flows),
            'execution_chains': []
        }
        
        # Convert enhanced chains to YAML format
        if container_name in enhanced_chains_data:
            for chain_name, chain_steps in enhanced_chains_data[container_name].items():
                chain_data = {
                    'chain_name': chain_name,
                    'steps': []
                }
                
                for step in chain_steps:
                    step_data = {
                        'type': step['type'],
                        'name': step['name']
                    }
                    
                    if step['type'] == 'control_flow':
                        step_data['task_type'] = step.get('task_type', 'Unknown')
                        step_data['xml_file'] = step.get('xml_link', '').strip('[]')
                    elif step['type'] == 'data_flow':
                        step_data['xml_file'] = step.get('xml_link', '').strip('[]')
                        step_data['dtsid'] = step.get('dtsid', '')
                    elif step['type'] == 'reference':
                        step_data['reference_to'] = step.get('reference_to', '')
                    
                    chain_data['steps'].append(step_data)
                
                container_data['execution_chains'].append(chain_data)
        
        yaml_data['containers'][container_name] = container_data
    
    return yaml_data


def save_yaml_analysis(yaml_data: Dict, package_name: str) -> Path:
    """Save YAML analysis to Analysis_second_step directory."""
    # Create Analysis_second_step directory if it doesn't exist
    output_dir = Path('Analysis_second_step')
    output_dir.mkdir(exist_ok=True)
    
    # Create YAML filename
    yaml_filename = f"{package_name}_planned.yml"
    yaml_path = output_dir / yaml_filename
    
    # Write YAML file
    with open(yaml_path, 'w', encoding='utf-8') as yaml_file:
        yaml.dump(yaml_data, yaml_file, default_flow_style=False, sort_keys=False, indent=2)
    
    return yaml_path


def main():
    args = parse_args()
    split_dir = Path(args.split_dir)
    
    # Load DAG
    dag_xml = next(split_dir.glob('*.dataflow_dag.xml'))
    if not dag_xml.exists():
        raise FileNotFoundError(f"No dataflow_dag.xml found in {split_dir}")
    
    dag = load_dag(dag_xml)
    order: List[str] = topo_order(list(dag.nodes.keys()), dag.edges)
    
    # Load control flows
    control_flows = load_control_flows(split_dir)
    
    if args.verbose:
        print(f"Loaded DAG with {len(dag.nodes)} nodes and {len(dag.edges)} edges")
        print(f"Loaded {len(control_flows)} control flow tasks")
        print(f"DAG file: {dag_xml}")
        print()
    
    # Validation mode
    if args.validate:
        violations = validate_dependencies(order, dag.edges)
        if violations:
            print(f"❌ Found {len(violations)} dependency violations:")
            for violation in violations:
                print(f"  {violation}")
            return 1
        else:
            print("✅ All dependencies are correctly honored!")
            if not args.verbose:
                return 0
    
    # Show dependency chains
    if args.show_chains:
        chains = build_dependency_chains(dag.edges, set(dag.nodes.keys()))
        print(f"\nDependency Chains ({len(chains)} chains found):")
        print("=" * 50)
        for i, (root, chain) in enumerate(chains.items(), 1):
            if len(chain) > 1:
                chain_names = [dag.nodes[ref].name for ref in chain]
                print(f"Chain {i}: {' → '.join(chain_names)}")
            else:
                print(f"Chain {i}: {dag.nodes[root].name} (single node)")
        print()
    
    # Show parallel execution opportunities
    if args.show_parallel:
        parallel_nodes = find_parallel_nodes(dag.edges, set(dag.nodes.keys()))
        print(f"\nParallel Execution Opportunities ({len(parallel_nodes)} independent nodes):")
        print("=" * 50)
        for node_ref in parallel_nodes:
            print(f"  {dag.nodes[node_ref].name}")
        print()
    
    # Enhanced Group by container with control flows
    if args.group_by_container:
        container_groups = {}
        for ref in order:
            container = extract_container_name(ref)
            if container not in container_groups:
                container_groups[container] = []
            container_groups[container].append(ref)
        
        # Build chains for analysis
        chains = build_dependency_chains(dag.edges, set(dag.nodes.keys()))
        
        # Collect enhanced chains data for all containers (for YAML export)
        enhanced_chains_data = {}
        
        print(f"\nGrouped by Sequence Container ({len(container_groups)} containers):")
        print("=" * 50)
        for container, nodes in container_groups.items():
            print(f"\n{container} ({len(nodes)} flows):")
            
            # Control flows are now only shown in Enhanced Execution Chains
            
            # Show chains within this container
            container_chains = []
            for chain_root, chain_nodes in chains.items():
                # Check if any node in the chain belongs to this container
                chain_in_container = []
                for chain_node in chain_nodes:
                    if extract_container_name(chain_node) == container:
                        chain_in_container.append(chain_node)
                
                if chain_in_container:
                    container_chains.append(chain_in_container)
            
            # Build enhanced chains that include control flows
            enhanced_chains = build_enhanced_container_chains(container, chains, control_flows, dag, split_dir)
            enhanced_chains_data[container] = enhanced_chains
            
            if enhanced_chains:
                print("  Enhanced Execution Chains:")
                for chain_name, chain_steps in enhanced_chains.items():
                    print(f"    {chain_name}:")
                    for i, step in enumerate(chain_steps):
                        if step['type'] == 'control_flow':
                            xml_link = step.get('xml_link', '')
                            print(f"      {'├─' if i < len(chain_steps)-1 else '└─'} 🔧 {step['name']} {xml_link}")
                        elif step['type'] == 'reference':
                            print(f"      {'├─' if i < len(chain_steps)-1 else '└─'} 📋 {step['name']}")
                        else:  # data_flow
                            print(f"      {'├─' if i < len(chain_steps)-1 else '└─'} 📊 {step['name']} {step['xml_link']}")
            else:
                print("  Enhanced Execution Chains: None (all nodes independent)")
        
        # Save YAML analysis if requested
        if args.save_yaml:
            package_name = extract_package_name(split_dir)
            yaml_data = build_yaml_data(container_groups, enhanced_chains_data, dag, split_dir)
            yaml_path = save_yaml_analysis(yaml_data, package_name)
            print(f"\n✅ YAML analysis saved to: {yaml_path}")
    
    # Standard list mode
    if args.list:
        print(f"\nExecution Order ({len(order)} flows):")
        print("=" * 50)
        for i, ref in enumerate(order, 1):
            node = dag.nodes[ref]
            container = extract_container_name(ref) if args.verbose else ""
            container_info = f" [{container}]" if container and args.verbose else ""
            print(f"{i:2d}. {node.name}{container_info}")
            if args.verbose:
                print(f"    DTSID: {node.dtsid}")
                print(f"    RefID: {ref}")
        return
    
    # Execution mode
    if not any([args.validate, args.show_chains, args.group_by_container, args.show_parallel, args.show_control_flows]):
        if not args.package:
            raise ValueError("--package is required for execution mode")
        
        variables_xml = next(split_dir.glob('*.variables.xml'), None)
        variables = load_variables(variables_xml) if variables_xml else {}

        context = {
            'split_dir': str(split_dir),
            'variables': variables,
            'package_path': str(Path(args.package).resolve()),
        }

        print(f"Executing {len(order)} data flows...")
        
        for i, ref in enumerate(order, 1):
            node = dag.nodes[ref]
            module = try_import_migrated_module(args.module_base, node.name)
            df_path = find_dataflow_xml(split_dir, node.dtsid, node.name)
            
            execution_mode = 'python' if module else ('ssis' if args.run_ssis_fallback else 'skip')
            print(f"\n[{i}/{len(order)}] {node.name} -> {execution_mode}")
            
            if args.verbose:
                print(f"  DTSID: {node.dtsid}")
                print(f"  XML: {df_path}")
            
            if module:
                ok = bool(module.run(context))
                if not ok:
                    raise SystemExit(f"Migrated data flow failed: {node.name}")
                print(f"  ✅ Completed successfully")
            else:
                if args.run_ssis_fallback:
                    rc = run_dtexec(Path(args.package))
                    if rc != 0:
                        raise SystemExit(f"SSIS fallback failed for: {node.name}")
                    print(f"  ✅ SSIS fallback completed")
                else:
                    print(f"  ⏭️  Skipped (no Python module found)")


if __name__ == '__main__':
    main()