#!/usr/bin/env python3
"""
Dependency validation tool for SSIS migration orchestrator.

Verifies that the orchestrator's topological sort correctly honors 
all dependencies defined in the DataFlow DAG XML edges.
"""
import argparse
from pathlib import Path
from typing import List, Tuple, Dict, Set
from utils import load_dag, topo_order


def parse_args():
    p = argparse.ArgumentParser(description='Validate orchestrator dependency ordering')
    p.add_argument('--split-dir', required=True, help='Path to the split output directory')
    p.add_argument('--verbose', '-v', action='store_true', help='Show detailed dependency information')
    return p.parse_args()


def validate_topological_order(ordered_nodes: List[str], edges: List[Tuple[str, str, Dict[str, str]]]) -> List[str]:
    """
    Validate that the topologically ordered nodes respect all edge dependencies.
    
    Returns list of violated dependencies as "from_node -> to_node" strings.
    """
    # Create position map for quick lookup
    node_positions = {node: i for i, node in enumerate(ordered_nodes)}
    
    violations = []
    
    for from_node, to_node, edge_attrs in edges:
        if from_node not in node_positions or to_node not in node_positions:
            violations.append(f"MISSING_NODE: {from_node} -> {to_node} (node not found in ordering)")
            continue
            
        from_pos = node_positions[from_node]
        to_pos = node_positions[to_node]
        
        if from_pos >= to_pos:
            violations.append(f"ORDER_VIOLATION: {from_node} (pos {from_pos}) -> {to_node} (pos {to_pos})")
    
    return violations


def analyze_dependency_chains(edges: List[Tuple[str, str, Dict[str, str]]]) -> Dict[str, List[str]]:
    """
    Find all dependency chains in the DAG.
    
    Returns dict mapping starting nodes to their complete dependency chains.
    """
    # Build adjacency list
    adj = {}
    in_degree = {}
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
    
    def build_chain(node: str, current_chain: List[str] = None) -> List[str]:
        if current_chain is None:
            current_chain = []
        
        current_chain = current_chain + [node]
        
        # If no outgoing edges, return the chain
        if node not in adj:
            return current_chain
        
        # Find the longest chain from this node
        longest_chain = current_chain
        for child in adj[node]:
            child_chain = build_chain(child, current_chain)
            if len(child_chain) > len(longest_chain):
                longest_chain = child_chain
        
        return longest_chain
    
    for root in root_nodes:
        chains[root] = build_chain(root)
    
    return chains


def find_parallel_groups(edges: List[Tuple[str, str, Dict[str, str]]], all_nodes: Set[str]) -> List[List[str]]:
    """
    Find groups of nodes that can execute in parallel (no dependencies between them).
    """
    # Build dependency sets
    depends_on = {}  # node -> set of nodes it depends on (transitively)
    
    for node in all_nodes:
        depends_on[node] = set()
    
    # Add direct dependencies
    for from_node, to_node, _ in edges:
        depends_on[to_node].add(from_node)
    
    # Add transitive dependencies using topological approach
    changed = True
    while changed:
        changed = False
        for node in all_nodes:
            old_size = len(depends_on[node])
            # Add dependencies of dependencies
            new_deps = set()
            for dep in depends_on[node]:
                new_deps.update(depends_on[dep])
            depends_on[node].update(new_deps)
            if len(depends_on[node]) > old_size:
                changed = True
    
    # Group nodes by their dependency signature
    dependency_groups = {}
    for node in all_nodes:
        dep_key = tuple(sorted(depends_on[node]))
        if dep_key not in dependency_groups:
            dependency_groups[dep_key] = []
        dependency_groups[dep_key].append(node)
    
    # Filter out single-node groups and return parallel groups
    parallel_groups = [group for group in dependency_groups.values() if len(group) > 1]
    
    return parallel_groups


def main():
    args = parse_args()
    split_dir = Path(args.split_dir)
    
    # Load the DAG
    dag_xml = next(split_dir.glob('*.dataflow_dag.xml'))
    if not dag_xml.exists():
        raise FileNotFoundError(f"No dataflow_dag.xml found in {split_dir}")
    
    print(f"Loading DAG from: {dag_xml}")
    dag = load_dag(dag_xml)
    
    print(f"Found {len(dag.nodes)} nodes and {len(dag.edges)} edges")
    
    # Get current orchestrator ordering
    ordered_nodes = topo_order(list(dag.nodes.keys()), dag.edges)
    print(f"Orchestrator produced {len(ordered_nodes)} nodes in topological order")
    
    # Validate the ordering
    print("\n" + "="*60)
    print("DEPENDENCY VALIDATION RESULTS")
    print("="*60)
    
    violations = validate_topological_order(ordered_nodes, dag.edges)
    
    if violations:
        print(f"❌ FOUND {len(violations)} DEPENDENCY VIOLATIONS:")
        print()
        for violation in violations:
            print(f"  {violation}")
        print()
    else:
        print("✅ All dependencies are correctly honored!")
        print()
    
    if args.verbose:
        print("\n" + "="*60)
        print("DEPENDENCY ANALYSIS")
        print("="*60)
        
        # Show dependency chains
        chains = analyze_dependency_chains(dag.edges)
        print(f"\nFound {len(chains)} dependency chains:")
        for root, chain in chains.items():
            if len(chain) > 1:
                print(f"  {root}: {' → '.join(chain)}")
        
        # Show parallel groups
        all_nodes = set(dag.nodes.keys())
        parallel_groups = find_parallel_groups(dag.edges, all_nodes)
        print(f"\nFound {len(parallel_groups)} parallel execution groups:")
        for i, group in enumerate(parallel_groups, 1):
            print(f"  Group {i}: {len(group)} nodes can run in parallel")
            if len(group) <= 5:  # Show small groups
                print(f"    {', '.join(sorted(group))}")
        
        # Show execution statistics
        independent_nodes = [node for node in all_nodes 
                           if not any(edge[1] == node for edge in dag.edges)]
        dependent_nodes = [node for node in all_nodes 
                         if any(edge[1] == node for edge in dag.edges)]
        
        print(f"\nExecution Statistics:")
        print(f"  Independent nodes (can start immediately): {len(independent_nodes)}")
        print(f"  Dependent nodes (must wait for prerequisites): {len(dependent_nodes)}")
        print(f"  Total edges (dependencies): {len(dag.edges)}")
    
    # Return appropriate exit code
    return len(violations)


if __name__ == '__main__':
    exit_code = main()
    exit(exit_code)