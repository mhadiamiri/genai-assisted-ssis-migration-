import os
import shutil
import importlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional

DTS_NS = 'www.microsoft.com/SqlServer/Dts'
NS = {'DTS': DTS_NS}


@dataclass
class DagNode:
    ref_id: str
    name: str
    dtsid: str


@dataclass
class Dag:
    nodes: Dict[str, DagNode]
    edges: List[Tuple[str, str, Dict[str, str]]]


def load_dag(dag_xml_path: Path) -> Dag:
    root = ET.parse(dag_xml_path).getroot()
    nodes_elem = root.find('Nodes')
    edges_elem = root.find('Edges')
    nodes: Dict[str, DagNode] = {}
    edges: List[Tuple[str, str, Dict[str, str]]] = []
    if nodes_elem is not None:
        for n in nodes_elem:
            nodes[n.attrib['refId']] = DagNode(
                ref_id=n.attrib['refId'],
                name=n.attrib.get('name', ''),
                dtsid=n.attrib.get('dtsid', ''),
            )
    if edges_elem is not None:
        for e in edges_elem:
            attrs = dict(e.attrib)
            edges.append((attrs['from'], attrs['to'], attrs))
    return Dag(nodes=nodes, edges=edges)


def topo_order(nodes: List[str], edges: List[Tuple[str, str, Dict[str, str]]]) -> List[str]:
    indeg = {n: 0 for n in nodes}
    out_edges: Dict[str, List[str]] = {n: [] for n in nodes}
    for f, t, _ in edges:
        if f in nodes and t in nodes:
            indeg[t] += 1
            out_edges[f].append(t)
    queue = [n for n, d in indeg.items() if d == 0]
    ordered = []
    while queue:
        n = queue.pop(0)
        ordered.append(n)
        for t in out_edges.get(n, []):
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    # If cycle exists, append remaining in arbitrary order
    if len(ordered) != len(nodes):
        remaining = [n for n in nodes if n not in ordered]
        ordered.extend(remaining)
    return ordered


def sanitize_to_module_name(name: str) -> str:
    base = ''.join(c.lower() if c.isalnum() else '_' for c in name.strip())
    while '__' in base:
        base = base.replace('__', '_')
    return base.strip('_')


def find_dataflow_xml(split_dir: Path, dtsid: str, name: str) -> Optional[Path]:
    df_dir = split_dir / 'dataflows'
    if not df_dir.is_dir():
        return None
    dtsid_clean = dtsid.strip('{}')
    # Prefer GUID match
    by_guid = list(df_dir.glob(f"*_{dtsid_clean}.xml"))
    if by_guid:
        return by_guid[0]
    # Fallback to name contains
    safe = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in name)
    candidates = sorted(df_dir.glob(f"*{safe}*.xml"))
    return candidates[0] if candidates else None


def try_import_migrated_module(module_base_pkg: str, name: str):
    mod_name = f"{module_base_pkg}.{sanitize_to_module_name(name)}"
    try:
        return importlib.import_module(mod_name)
    except ModuleNotFoundError:
        return None


def which_dtexec() -> Optional[str]:
    return shutil.which('dtexec') or shutil.which('dtexec.exe')


def run_dtexec(package_path: Path) -> int:
    exe = which_dtexec()
    if not exe:
        raise RuntimeError('dtexec not found in PATH')
    import subprocess
    proc = subprocess.run([exe, '/F', str(package_path)])
    return proc.returncode


def load_variables(variables_xml_path: Path) -> Dict[str, str]:
    if not variables_xml_path.exists():
        return {}
    root = ET.parse(variables_xml_path).getroot()
    vars_map: Dict[str, str] = {}
    for var in root.findall('.//DTS:Variable', NS):
        name = var.attrib.get(f'{{{DTS_NS}}}ObjectName')
        val_node = var.find('DTS:VariableValue', NS)
        val = val_node.text if val_node is not None else None
        if name:
            vars_map[name] = val
    return vars_map
