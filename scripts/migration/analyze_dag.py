#!/usr/bin/env python3
"""
DAG Analysis Tool for SSIS Migration Planning

Provides comprehensive analysis and visualization of SSIS package dependencies
to support migration planning and strategy development.
"""
import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple, Set, Optional
from collections import defaultdict, deque
from utils import load_dag, DagNode, Dag


def parse_args():
    p = argparse.ArgumentParser(description='Analyze DAG structure for migration planning')
    p.add_argument('--split-dir', required=True, help='Path to the split output directory')
    p.add_argument('--output', help='Save analysis results to JSON file')
    p.add_argument('--format', choices=['text', 'json', 'graphviz'], default='text',
                   help='Output format (default: text)')
    
    # Analysis options
    p.add_argument('--critical-path', action='store_true', help='Show critical path analysis')
    p.add_argument('--migration-levels', action='store_true', help='Group nodes by migration complexity levels')
    p.add_argument('--container-analysis', action='store_true', help='Analyze sequence container groupings')
    p.add_argument('--complexity-metrics', action='store_true', help='Calculate migration complexity metrics')
    
    # Visualization options
    p.add_argument('--dependency-matrix', action='store_true', help='Generate dependency matrix')
    p.add_argument('--flow-hierarchy', action='store_true', help='Show hierarchical flow structure')
    
    p.add_argument('--verbose', '-v', action='store_true', help='Show detailed analysis')
    return p.parse_args()


def extract_container_info(ref_id: str) -> Dict[str, str]:
    """Extract container hierarchy information from reference ID."""
    parts = ref_id.split('\\')
    return {
        'package': parts[0] if len(parts) > 0 else 'Unknown',
        'sequence_container': parts[1] if len(parts) > 1 else 'Root',
        'dataflow': parts[2] if len(parts) > 2 else parts[-1],
        'full_path': ref_id,
        'depth': len(parts) - 1
    }


def calculate_node_metrics(node_ref: str, dag: Dag) -> Dict[str, int]:
    """Calculate complexity metrics for a single node."""
    # Count direct dependencies (in-degree)
    in_degree = sum(1 for _, to_node, _ in dag.edges if to_node == node_ref)
    
    # Count dependents (out-degree)
    out_degree = sum(1 for from_node, _, _ in dag.edges if from_node == node_ref)
    
    # Estimate migration complexity based on name patterns
    node_name = dag.nodes[node_ref].name.lower()
    complexity_score = 0
    
    # Add complexity for common SSIS components
    complexity_indicators = {
        'lookup': 3, 'merge': 2, 'union': 1, 'sort': 2, 'aggregate': 3,
        'pivot': 4, 'unpivot': 4, 'fuzzy': 5, 'script': 4, 'ole_db': 2,
        'xml': 3, 'ftp': 3, 'web': 3, 'cache': 2, 'slowly_changing': 5
    }
    
    for indicator, score in complexity_indicators.items():
        if indicator in node_name:
            complexity_score += score
    
    return {
        'in_degree': in_degree,
        'out_degree': out_degree,
        'total_degree': in_degree + out_degree,
        'complexity_score': complexity_score
    }


def find_critical_path(dag: Dag) -> List[Tuple[str, int]]:
    """Find the longest path through the DAG (critical path)."""
    # Build adjacency list and calculate distances
    adj = defaultdict(list)
    distances = {node_ref: 0 for node_ref in dag.nodes.keys()}
    
    for from_node, to_node, _ in dag.edges:
        adj[from_node].append(to_node)
    
    # Topological sort for longest path calculation
    in_degree = defaultdict(int)
    for node_ref in dag.nodes.keys():
        in_degree[node_ref] = 0
    
    for from_node, to_node, _ in dag.edges:
        in_degree[to_node] += 1
    
    queue = deque([node for node, degree in in_degree.items() if degree == 0])
    topo_order = []
    
    while queue:
        node = queue.popleft()
        topo_order.append(node)
        
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
    
    # Calculate longest distances
    for node in topo_order:
        for neighbor in adj[node]:
            distances[neighbor] = max(distances[neighbor], distances[node] + 1)
    
    # Build critical path
    max_distance = max(distances.values())
    critical_nodes = [(node, dist) for node, dist in distances.items() if dist == max_distance]
    
    return sorted(critical_nodes, key=lambda x: x[1], reverse=True)


def group_by_migration_levels(dag: Dag) -> Dict[int, List[str]]:
    """Group nodes by migration complexity levels."""
    levels = defaultdict(list)
    
    for node_ref, node in dag.nodes.items():
        metrics = calculate_node_metrics(node_ref, dag)
        
        # Determine migration level based on complexity
        level = 1  # Basic level
        
        if metrics['complexity_score'] >= 8:
            level = 4  # Advanced
        elif metrics['complexity_score'] >= 5:
            level = 3  # Intermediate
        elif metrics['complexity_score'] >= 2:
            level = 2  # Basic+
        
        # Adjust for dependency complexity
        if metrics['total_degree'] >= 6:
            level += 1
        elif metrics['total_degree'] >= 3:
            level += 0.5
        
        final_level = min(4, int(level))
        levels[final_level].append(node_ref)
    
    return dict(levels)


def analyze_containers(dag: Dag) -> Dict[str, Dict]:
    """Analyze sequence container groupings and their characteristics."""
    containers = defaultdict(lambda: {
        'nodes': [],
        'internal_edges': 0,
        'external_in_edges': 0,
        'external_out_edges': 0,
        'depth': 0
    })
    
    # Group nodes by container
    for node_ref, node in dag.nodes.items():
        container_info = extract_container_info(node_ref)
        container_name = container_info['sequence_container']
        
        containers[container_name]['nodes'].append(node_ref)
        containers[container_name]['depth'] = container_info['depth']
    
    # Analyze edges
    for from_node, to_node, _ in dag.edges:
        from_container = extract_container_info(from_node)['sequence_container']
        to_container = extract_container_info(to_node)['sequence_container']
        
        if from_container == to_container:
            containers[from_container]['internal_edges'] += 1
        else:
            containers[from_container]['external_out_edges'] += 1
            containers[to_container]['external_in_edges'] += 1
    
    return dict(containers)


def generate_dependency_matrix(dag: Dag) -> Dict[str, Dict[str, bool]]:
    """Generate a dependency matrix showing all node relationships."""
    matrix = {}
    node_refs = list(dag.nodes.keys())
    
    for node_ref in node_refs:
        matrix[node_ref] = {other_ref: False for other_ref in node_refs}
    
    # Mark direct dependencies
    for from_node, to_node, _ in dag.edges:
        if from_node in matrix and to_node in matrix:
            matrix[from_node][to_node] = True
    
    return matrix


def generate_graphviz_output(dag: Dag, analysis_results: Dict) -> str:
    """Generate Graphviz DOT format for visualization."""
    dot_lines = ['digraph SSIS_Migration_DAG {', '  rankdir=TB;', '  node [shape=box, style=rounded];']
    
    # Add nodes with complexity coloring
    migration_levels = analysis_results.get('migration_levels', {})
    level_colors = {1: 'lightgreen', 2: 'yellow', 3: 'orange', 4: 'red'}
    
    for node_ref, node in dag.nodes.items():
        # Find migration level
        level = 1
        for lvl, nodes in migration_levels.items():
            if node_ref in nodes:
                level = lvl
                break
        
        color = level_colors.get(level, 'lightgray')
        clean_name = node.name.replace('"', '\\"')
        dot_lines.append(f'  "{node_ref}" [label="{clean_name}", fillcolor={color}, style="rounded,filled"];')
    
    # Add edges
    for from_node, to_node, edge_attrs in dag.edges:
        dot_lines.append(f'  "{from_node}" -> "{to_node}";')
    
    dot_lines.append('}')
    return '\n'.join(dot_lines)


def format_analysis_results(dag: Dag, analysis_results: Dict, format_type: str) -> str:
    """Format analysis results based on output format."""
    if format_type == 'json':
        return json.dumps(analysis_results, indent=2, default=str)
    
    elif format_type == 'graphviz':
        return generate_graphviz_output(dag, analysis_results)
    
    else:  # text format
        lines = []
        lines.append("SSIS DAG MIGRATION ANALYSIS")
        lines.append("=" * 50)
        lines.append(f"Total Nodes: {len(dag.nodes)}")
        lines.append(f"Total Dependencies: {len(dag.edges)}")
        lines.append("")
        
        # Migration levels
        if 'migration_levels' in analysis_results:
            lines.append("MIGRATION COMPLEXITY LEVELS:")
            lines.append("-" * 30)
            levels = analysis_results['migration_levels']
            level_names = {1: 'Basic', 2: 'Intermediate', 3: 'Advanced', 4: 'Complex'}
            
            for level in sorted(levels.keys()):
                level_name = level_names.get(level, f'Level {level}')
                nodes = levels[level]
                lines.append(f"{level_name} ({len(nodes)} nodes):")
                for node_ref in sorted(nodes):
                    node_name = dag.nodes[node_ref].name
                    lines.append(f"  • {node_name}")
                lines.append("")
        
        # Critical path
        if 'critical_path' in analysis_results:
            lines.append("CRITICAL PATH ANALYSIS:")
            lines.append("-" * 25)
            critical_path = analysis_results['critical_path']
            lines.append(f"Longest path length: {critical_path[0][1] + 1 if critical_path else 0} steps")
            if critical_path:
                lines.append("Critical nodes:")
                for node_ref, distance in critical_path[:5]:  # Show top 5
                    node_name = dag.nodes[node_ref].name
                    lines.append(f"  • {node_name} (depth: {distance})")
            lines.append("")
        
        # Container analysis
        if 'containers' in analysis_results:
            lines.append("CONTAINER ANALYSIS:")
            lines.append("-" * 20)
            containers = analysis_results['containers']
            for container_name, info in containers.items():
                lines.append(f"{container_name}:")
                lines.append(f"  Nodes: {len(info['nodes'])}")
                lines.append(f"  Internal edges: {info['internal_edges']}")
                lines.append(f"  External in/out: {info['external_in_edges']}/{info['external_out_edges']}")
                lines.append("")
        
        # Complexity metrics summary
        if 'complexity_summary' in analysis_results:
            lines.append("COMPLEXITY METRICS SUMMARY:")
            lines.append("-" * 30)
            summary = analysis_results['complexity_summary']
            lines.append(f"Average complexity score: {summary['avg_complexity']:.1f}")
            lines.append(f"Highest complexity nodes: {summary['high_complexity_count']}")
            lines.append(f"Independent nodes: {summary['independent_nodes']}")
            lines.append(f"Highly connected nodes: {summary['highly_connected_nodes']}")
        
        return '\n'.join(lines)


def main():
    args = parse_args()
    split_dir = Path(args.split_dir)
    
    # Load DAG
    dag_xml = next(split_dir.glob('*.dataflow_dag.xml'))
    if not dag_xml.exists():
        raise FileNotFoundError(f"No dataflow_dag.xml found in {split_dir}")
    
    dag = load_dag(dag_xml)
    analysis_results = {}
    
    # Perform requested analyses
    if args.migration_levels or args.format == 'text':
        analysis_results['migration_levels'] = group_by_migration_levels(dag)
    
    if args.critical_path or args.format == 'text':
        analysis_results['critical_path'] = find_critical_path(dag)
    
    if args.container_analysis or args.format == 'text':
        analysis_results['containers'] = analyze_containers(dag)
    
    if args.complexity_metrics or args.format == 'text':
        # Calculate summary metrics
        all_metrics = [calculate_node_metrics(ref, dag) for ref in dag.nodes.keys()]
        avg_complexity = sum(m['complexity_score'] for m in all_metrics) / len(all_metrics)
        high_complexity = sum(1 for m in all_metrics if m['complexity_score'] >= 5)
        independent = sum(1 for m in all_metrics if m['in_degree'] == 0)
        highly_connected = sum(1 for m in all_metrics if m['total_degree'] >= 4)
        
        analysis_results['complexity_summary'] = {
            'avg_complexity': avg_complexity,
            'high_complexity_count': high_complexity,
            'independent_nodes': independent,
            'highly_connected_nodes': highly_connected
        }
    
    if args.dependency_matrix:
        analysis_results['dependency_matrix'] = generate_dependency_matrix(dag)
    
    if args.flow_hierarchy:
        # Build hierarchical structure
        hierarchy = defaultdict(list)
        for node_ref in dag.nodes.keys():
            container_info = extract_container_info(node_ref)
            hierarchy[container_info['sequence_container']].append({
                'node_ref': node_ref,
                'name': dag.nodes[node_ref].name,
                'depth': container_info['depth']
            })
        analysis_results['flow_hierarchy'] = dict(hierarchy)
    
    # Format and output results
    output = format_analysis_results(dag, analysis_results, args.format)
    
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output)
        print(f"Analysis saved to: {output_path}")
    else:
        print(output)
    
    # Save full analysis as JSON if requested
    if args.output and args.format != 'json':
        json_path = Path(args.output).with_suffix('.json')
        json_path.write_text(json.dumps(analysis_results, indent=2, default=str))
        print(f"Full analysis data saved to: {json_path}")


if __name__ == '__main__':
    main()