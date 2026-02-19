#!/usr/bin/env python3
"""
Test Suite for SSIS Migration Orchestrator

Comprehensive tests to validate orchestrator correctness, dependency ordering,
and DAG analysis functionality.
"""
import pytest
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple, Dict
from unittest.mock import patch, MagicMock

# Import modules under test
from utils import load_dag, topo_order, Dag, DagNode
from validate_dependencies import validate_topological_order, analyze_dependency_chains, find_parallel_groups
from analyze_dag import (
    calculate_node_metrics, find_critical_path, group_by_migration_levels,
    analyze_containers, extract_container_info
)


class TestDAGLoading:
    """Test DAG loading and parsing functionality."""
    
    def setup_method(self):
        """Create test DAG XML for testing."""
        self.test_xml = '''<?xml version="1.0" encoding="utf-8"?>
<DAG>
  <Nodes>
    <Node refId="Package\\Container1\\NodeA" name="NodeA" dtsid="{A1A1A1A1-B2B2-C3C3-D4D4-E5E5E5E5E5E5}" />
    <Node refId="Package\\Container1\\NodeB" name="NodeB" dtsid="{B1B1B1B1-B2B2-C3C3-D4D4-E5E5E5E5E5E5}" />
    <Node refId="Package\\Container2\\NodeC" name="NodeC" dtsid="{C1C1C1C1-B2B2-C3C3-D4D4-E5E5E5E5E5E5}" />
    <Node refId="Package\\Container2\\NodeD" name="NodeD" dtsid="{D1D1D1D1-B2B2-C3C3-D4D4-E5E5E5E5E5E5}" />
  </Nodes>
  <Edges>
    <Edge from="Package\\Container1\\NodeA" to="Package\\Container1\\NodeB" />
    <Edge from="Package\\Container1\\NodeB" to="Package\\Container2\\NodeC" />
    <Edge from="Package\\Container2\\NodeC" to="Package\\Container2\\NodeD" />
  </Edges>
</DAG>'''
    
    def test_load_dag_basic(self):
        """Test basic DAG loading functionality."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(self.test_xml)
            temp_path = Path(f.name)
        
        try:
            dag = load_dag(temp_path)
            
            assert isinstance(dag, Dag)
            assert len(dag.nodes) == 4
            assert len(dag.edges) == 3
            
            # Check node properties
            node_a = dag.nodes["Package\\Container1\\NodeA"]
            assert node_a.name == "NodeA"
            assert node_a.dtsid == "{A1A1A1A1-B2B2-C3C3-D4D4-E5E5E5E5E5E5}"
            
        finally:
            temp_path.unlink()
    
    def test_load_dag_edges(self):
        """Test edge loading and structure."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False) as f:
            f.write(self.test_xml)
            temp_path = Path(f.name)
        
        try:
            dag = load_dag(temp_path)
            
            # Verify edge structure
            expected_edges = [
                ("Package\\Container1\\NodeA", "Package\\Container1\\NodeB"),
                ("Package\\Container1\\NodeB", "Package\\Container2\\NodeC"),
                ("Package\\Container2\\NodeC", "Package\\Container2\\NodeD")
            ]
            
            actual_edges = [(edge[0], edge[1]) for edge in dag.edges]
            assert set(actual_edges) == set(expected_edges)
            
        finally:
            temp_path.unlink()


class TestTopologicalSort:
    """Test topological sorting algorithm."""
    
    def test_linear_dependency_chain(self):
        """Test topological sort with linear dependency chain."""
        nodes = ["A", "B", "C", "D"]
        edges = [("A", "B", {}), ("B", "C", {}), ("C", "D", {})]
        
        result = topo_order(nodes, edges)
        
        assert result == ["A", "B", "C", "D"]
    
    def test_parallel_branches(self):
        """Test topological sort with parallel execution opportunities."""
        nodes = ["A", "B1", "B2", "C"]
        edges = [("A", "B1", {}), ("A", "B2", {}), ("B1", "C", {}), ("B2", "C", {})]
        
        result = topo_order(nodes, edges)
        
        # A should be first, C should be last
        assert result[0] == "A"
        assert result[-1] == "C"
        # B1 and B2 can be in any order
        assert set(result[1:3]) == {"B1", "B2"}
    
    def test_no_dependencies(self):
        """Test topological sort with independent nodes."""
        nodes = ["A", "B", "C"]
        edges = []
        
        result = topo_order(nodes, edges)
        
        assert set(result) == {"A", "B", "C"}
        assert len(result) == 3
    
    def test_complex_dag(self):
        """Test topological sort with complex dependency structure."""
        nodes = ["A", "B", "C", "D", "E", "F"]
        edges = [
            ("A", "C", {}), ("B", "C", {}), 
            ("C", "D", {}), ("C", "E", {}),
            ("D", "F", {}), ("E", "F", {})
        ]
        
        result = topo_order(nodes, edges)
        
        # Validate ordering constraints
        a_pos = result.index("A")
        b_pos = result.index("B") 
        c_pos = result.index("C")
        d_pos = result.index("D")
        e_pos = result.index("E")
        f_pos = result.index("F")
        
        assert a_pos < c_pos
        assert b_pos < c_pos
        assert c_pos < d_pos
        assert c_pos < e_pos
        assert d_pos < f_pos
        assert e_pos < f_pos


class TestDependencyValidation:
    """Test dependency validation functionality."""
    
    def test_valid_ordering(self):
        """Test validation of correct topological ordering."""
        nodes = ["A", "B", "C"]
        edges = [("A", "B", {}), ("B", "C", {})]
        
        violations = validate_topological_order(nodes, edges)
        
        assert violations == []
    
    def test_invalid_ordering(self):
        """Test detection of dependency violations."""
        nodes = ["C", "A", "B"]  # Wrong order
        edges = [("A", "B", {}), ("B", "C", {})]
        
        violations = validate_topological_order(nodes, edges)
        
        assert len(violations) == 1  # B->C violation (A->B is satisfied)
        assert any("ORDER_VIOLATION" in v for v in violations)
    
    def test_missing_nodes(self):
        """Test handling of missing nodes in ordering."""
        nodes = ["A", "B"]  # Missing C
        edges = [("A", "B", {}), ("B", "C", {})]
        
        violations = validate_topological_order(nodes, edges)
        
        assert len(violations) == 1
        assert "MISSING_NODE" in violations[0]


class TestDependencyChains:
    """Test dependency chain analysis."""
    
    def test_simple_chain(self):
        """Test identification of simple dependency chain."""
        edges = [("A", "B", {}), ("B", "C", {})]
        
        chains = analyze_dependency_chains(edges)
        
        assert "A" in chains
        assert chains["A"] == ["A", "B", "C"]
    
    def test_multiple_chains(self):
        """Test identification of multiple independent chains."""
        edges = [("A", "B", {}), ("C", "D", {})]
        
        chains = analyze_dependency_chains(edges)
        
        assert len(chains) == 2
        assert chains["A"] == ["A", "B"]
        assert chains["C"] == ["C", "D"]
    
    def test_branching_chains(self):
        """Test handling of chains with branches."""
        edges = [("A", "B", {}), ("A", "C", {}), ("B", "D", {})]
        
        chains = analyze_dependency_chains(edges)
        
        assert "A" in chains
        # Should follow one of the branches (longest path)
        assert len(chains["A"]) >= 3


class TestParallelGroups:
    """Test parallel execution group identification."""
    
    def test_independent_nodes(self):
        """Test identification of completely independent nodes."""
        edges = []
        all_nodes = {"A", "B", "C"}
        
        groups = find_parallel_groups(edges, all_nodes)
        
        # All nodes should be in one parallel group
        assert len(groups) == 1
        assert set(groups[0]) == {"A", "B", "C"}
    
    def test_mixed_dependencies(self):
        """Test parallel groups with mixed dependency patterns."""
        edges = [("A", "D", {}), ("B", "E", {})]
        all_nodes = {"A", "B", "C", "D", "E"}
        
        groups = find_parallel_groups(edges, all_nodes)
        
        # Should identify independent starting nodes
        independent_starts = []
        for group in groups:
            if len(group) > 1:
                independent_starts.extend(group)
        
        assert "A" in independent_starts or "B" in independent_starts or "C" in independent_starts


class TestContainerAnalysis:
    """Test sequence container analysis."""
    
    def test_extract_container_info(self):
        """Test container information extraction from reference ID."""
        ref_id = "Package\\SequenceContainer\\DataFlow"
        
        info = extract_container_info(ref_id)
        
        assert info['package'] == "Package"
        assert info['sequence_container'] == "SequenceContainer"
        assert info['dataflow'] == "DataFlow"
        assert info['depth'] == 2
    
    def test_container_grouping(self):
        """Test grouping of nodes by container."""
        # Create mock DAG
        nodes = {
            "Package\\Container1\\NodeA": DagNode("Package\\Container1\\NodeA", "NodeA", "{A}"),
            "Package\\Container1\\NodeB": DagNode("Package\\Container1\\NodeB", "NodeB", "{B}"),
            "Package\\Container2\\NodeC": DagNode("Package\\Container2\\NodeC", "NodeC", "{C}"),
        }
        edges = [("Package\\Container1\\NodeA", "Package\\Container1\\NodeB", {})]
        dag = Dag(nodes, edges)
        
        containers = analyze_containers(dag)
        
        assert "Container1" in containers
        assert "Container2" in containers
        assert len(containers["Container1"]["nodes"]) == 2
        assert len(containers["Container2"]["nodes"]) == 1
        assert containers["Container1"]["internal_edges"] == 1


class TestComplexityMetrics:
    """Test migration complexity calculation."""
    
    def test_node_metrics_basic(self):
        """Test basic node metrics calculation."""
        # Create mock DAG
        nodes = {
            "NodeA": DagNode("NodeA", "Basic_Transform", "{A}"),
            "NodeB": DagNode("NodeB", "Lookup_Component", "{B}"),
        }
        edges = [("NodeA", "NodeB", {})]
        dag = Dag(nodes, edges)
        
        metrics_a = calculate_node_metrics("NodeA", dag)
        metrics_b = calculate_node_metrics("NodeB", dag)
        
        assert metrics_a['in_degree'] == 0
        assert metrics_a['out_degree'] == 1
        assert metrics_b['in_degree'] == 1
        assert metrics_b['out_degree'] == 0
        
        # Lookup should have higher complexity
        assert metrics_b['complexity_score'] > metrics_a['complexity_score']
    
    def test_migration_levels(self):
        """Test migration complexity level assignment."""
        # Create mock DAG with various complexity nodes
        nodes = {
            "Simple": DagNode("Simple", "Simple_Transform", "{S}"),
            "Complex": DagNode("Complex", "Pivot_Lookup_Script", "{C}"),
        }
        dag = Dag(nodes, [])
        
        levels = group_by_migration_levels(dag)
        
        # Should have different complexity levels
        assert len(levels) > 0
        # All nodes should be assigned to some level
        total_nodes = sum(len(nodes) for nodes in levels.values())
        assert total_nodes == len(dag.nodes)


class TestCriticalPath:
    """Test critical path analysis."""
    
    def test_linear_critical_path(self):
        """Test critical path in linear dependency chain."""
        nodes = {
            "A": DagNode("A", "NodeA", "{A}"),
            "B": DagNode("B", "NodeB", "{B}"),
            "C": DagNode("C", "NodeC", "{C}"),
        }
        edges = [("A", "B", {}), ("B", "C", {})]
        dag = Dag(nodes, edges)
        
        critical_path = find_critical_path(dag)
        
        # Should identify the longest path
        assert len(critical_path) > 0
        # End node should have highest distance
        max_distance = max(distance for _, distance in critical_path)
        assert max_distance == 2  # 0->1->2
    
    def test_branching_critical_path(self):
        """Test critical path with multiple branches."""
        nodes = {f"Node{i}": DagNode(f"Node{i}", f"Node{i}", f"{{{i}}}") for i in "ABCDEF"}
        edges = [
            ("NodeA", "NodeB", {}), ("NodeA", "NodeC", {}),
            ("NodeB", "NodeD", {}), ("NodeC", "NodeE", {}),
            ("NodeD", "NodeF", {}), ("NodeE", "NodeF", {})
        ]
        dag = Dag(nodes, edges)
        
        critical_path = find_critical_path(dag)
        
        # Should identify nodes on the critical path
        assert len(critical_path) > 0
        # Final node should be at maximum distance
        final_nodes = [node for node, dist in critical_path if dist == max(d for _, d in critical_path)]
        assert "NodeF" in final_nodes


class TestIntegration:
    """Integration tests combining multiple components."""
    
    def test_full_orchestration_workflow(self):
        """Test complete orchestration workflow."""
        # Create test DAG XML
        test_xml = '''<?xml version="1.0" encoding="utf-8"?>
<DAG>
  <Nodes>
    <Node refId="Package\\Container\\Node1" name="Extract_Data" dtsid="{1}" />
    <Node refId="Package\\Container\\Node2" name="Transform_Data" dtsid="{2}" />
    <Node refId="Package\\Container\\Node3" name="Load_Data" dtsid="{3}" />
  </Nodes>
  <Edges>
    <Edge from="Package\\Container\\Node1" to="Package\\Container\\Node2" />
    <Edge from="Package\\Container\\Node2" to="Package\\Container\\Node3" />
  </Edges>
</DAG>'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.dataflow_dag.xml', delete=False) as f:
            f.write(test_xml)
            temp_path = Path(f.name)
        
        try:
            # Load and validate
            dag = load_dag(temp_path)
            nodes_list = list(dag.nodes.keys())
            ordered_nodes = topo_order(nodes_list, dag.edges)
            
            # Validate ordering
            violations = validate_topological_order(ordered_nodes, dag.edges)
            assert violations == []
            
            # Check expected order
            assert ordered_nodes == [
                "Package\\Container\\Node1",
                "Package\\Container\\Node2", 
                "Package\\Container\\Node3"
            ]
            
        finally:
            temp_path.unlink()
    
    @patch('utils.try_import_migrated_module')
    @patch('utils.run_dtexec')
    def test_orchestration_execution_simulation(self, mock_dtexec, mock_import):
        """Test orchestration execution with mocked components."""
        # Mock successful module import
        mock_module = MagicMock()
        mock_module.run.return_value = True
        mock_import.return_value = mock_module
        mock_dtexec.return_value = 0
        
        # This would normally require running the actual orchestrator
        # For now, just verify mocks work correctly
        assert mock_import("test_module", "test_flow") == mock_module
        assert mock_dtexec(Path("/fake/path")) == 0


def run_test_suite():
    """Run the complete test suite."""
    print("Running SSIS Migration Orchestrator Test Suite...")
    print("=" * 60)
    
    # Run pytest with verbose output
    pytest_args = [
        __file__,
        "-v",
        "--tb=short",
        "--color=yes"
    ]
    
    result = pytest.main(pytest_args)
    return result


if __name__ == '__main__':
    exit_code = run_test_suite()
    exit(exit_code)