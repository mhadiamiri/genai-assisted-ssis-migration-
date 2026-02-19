#!/usr/bin/env python3
import argparse
from pathlib import Path
from typing import List, Dict, Tuple, Set
from utils import load_dag, topo_order, try_import_migrated_module, run_dtexec, load_variables, find_dataflow_xml


def parse_args():
    p = argparse.ArgumentParser(description='Orchestrate migrated data flows using the generated DAG')
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
    
    # Output options
    p.add_argument('--verbose', '-v', action='store_true', help='Show detailed information')
    
    return p.parse_args()


def extract_container_name(ref_id: str) -> str:
    """Extract sequence container name from a reference ID."""
    parts = ref_id.split('\\')
    if len(parts) >= 2:
        return parts[1]  # Second part is usually the sequence container
    return "Unknown"


def build_dependency_chains(edges: List[Tuple[str, str, Dict[str, str]]], all_dag_nodes: Set[str] = None) -> Dict[str, List[str]]:
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


def main():
    args = parse_args()
    split_dir = Path(args.split_dir)
    
    # Load DAG
    dag_xml = next(split_dir.glob('*.dataflow_dag.xml'))
    if not dag_xml.exists():
        raise FileNotFoundError(f"No dataflow_dag.xml found in {split_dir}")
    
    dag = load_dag(dag_xml)
    order: List[str] = topo_order(list(dag.nodes.keys()), dag.edges)
    
    if args.verbose:
        print(f"Loaded DAG with {len(dag.nodes)} nodes and {len(dag.edges)} edges")
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
    
    # Group by container
    if args.group_by_container:
        container_groups = {}
        for ref in order:
            container = extract_container_name(ref)
            if container not in container_groups:
                container_groups[container] = []
            container_groups[container].append(ref)
        
        # Build chains for analysis
        chains = build_dependency_chains(dag.edges, set(dag.nodes.keys()))
        
        print(f"\nGrouped by Sequence Container ({len(container_groups)} containers):")
        print("=" * 50)
        for container, nodes in container_groups.items():
            print(f"\n{container} ({len(nodes)} flows):")
            
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
            
            if container_chains:
                print("  Dependency Chains:")
                for j, chain in enumerate(container_chains, 1):
                    if len(chain) > 1:
                        chain_names = [dag.nodes[ref].name for ref in chain]
                        print(f"    Chain {j}: {' → '.join(chain_names)}")
                    else:
                        chain_names = [dag.nodes[ref].name for ref in chain]
                        print(f"    Chain {j}: {chain_names[0]} (single node)")
            else:
                print("  Dependency Chains: None (all nodes independent)")
    
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
    if not args.validate and not args.show_chains and not args.group_by_container and not args.show_parallel:
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
