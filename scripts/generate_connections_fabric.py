#!/usr/bin/env python3
"""
Generate a minimal Fabric-oriented YAML mapping from SSIS connections.

Inputs scanned by default:
- Project-level connection managers: SSIS_Packages/CONSULAR_ORBIS/*.conmgr
- Package-level connections:     Analysis_first_step/*_split/*.connections.xml

Output (default): Analysis_first_step/connections_fabric.yaml

Schema (minimal):
connections:
  <CONNECTION_NAME>:
    role: source|destination
    current:
      type: dynamics365|sqlserver|other
      host: <for cloud>
      server: <for sqlserver>
      database: <for sqlserver>
    target:
      lakehouse: <fabric_lakehouse_name>
      schema: <schema_name>
"""
from __future__ import annotations

import argparse
import sys
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple, List
import xml.etree.ElementTree as ET

DTS_NS = 'www.microsoft.com/SqlServer/Dts'
NS = {'DTS': DTS_NS}


@dataclass
class CurrentConn:
    conn_type: str  # 'sqlserver'|'dynamics365'|'other'
    host: Optional[str] = None
    server: Optional[str] = None
    database: Optional[str] = None


@dataclass
class ConnRecord:
    name: str
    current: CurrentConn
    role: str  # 'source'|'destination'
    target_lakehouse: str
    target_schema: str


def read_xml(path: Path) -> Optional[ET.Element]:
    try:
        return ET.parse(path).getroot()
    except Exception:
        return None


def extract_connection_string(cm_elem: ET.Element) -> Optional[str]:
    # Try attribute first
    cs = cm_elem.attrib.get(f'{{{DTS_NS}}}ConnectionString')
    if cs:
        return cs
    # Try nested DTS:ObjectData/DTS:ConnectionManager attribute
    inner_cm = cm_elem.find('DTS:ObjectData/DTS:ConnectionManager', NS)
    if inner_cm is not None:
        inner_cs = inner_cm.attrib.get(f'{{{DTS_NS}}}ConnectionString')
        if inner_cs:
            return inner_cs
    # Try nested DTS:Property with Name="ConnectionString"
    for prop in cm_elem.findall('.//DTS:Property', NS):
        if prop.attrib.get(f'{{{DTS_NS}}}Name', '').lower() == 'connectionstring':
            if prop.text and prop.text.strip():
                return prop.text.strip()
    # Try any element named ConnectionString
    node = cm_elem.find('.//ConnectionString')
    if node is not None and node.text:
        return node.text.strip()
    return None


def parse_conn_string_pairs(conn_str: str) -> Dict[str, str]:
    pairs: Dict[str, str] = {}
    # Split by semicolons, handle escaped semicolons minimally
    for part in re.split(r';(?=(?:[^"]*\"[^"]*\")*[^"]*$)', conn_str):
        if not part:
            continue
        if '=' in part:
            k, v = part.split('=', 1)
            # Keys are case-insensitive; preserve original value casing
            pairs[k.strip().lower()] = v.strip()
    return pairs


def normalize_type(creation_name: Optional[str], raw_type_hint: Optional[str]) -> str:
    hint = (raw_type_hint or '').lower()
    c = (creation_name or '').lower()
    text = f"{hint} {c}"
    if 'ole db' in text or 'oledb' in text or 'sqlncli' in text or 'sql server' in text or 'mssql' in text:
        return 'sqlserver'
    if 'dynamics' in text or 'crm' in text or 'cds' in text or 'dataverse' in text:
        return 'dynamics365'
    return 'other'


def infer_role(name: str, curr: CurrentConn) -> str:
    n = name.lower()
    db = (curr.database or '').lower()
    if curr.conn_type == 'dynamics365' or any(x in n for x in ['source']) or n in ['ccem_source', 'orbis_cloud_source']:
        return 'source'
    if any(x in db for x in ['_ods', '_staging', '_mart']) or any(x in n for x in ['ods', 'staging', 'mart', 'mart_live']):
        return 'destination'
    if n in ['dw_admin', 'csp_arcgis']:
        return 'destination'
    # Default destination for SQL targets, source otherwise
    return 'destination' if curr.conn_type == 'sqlserver' else 'source'


def infer_schema(name: str, curr: CurrentConn) -> str:
    n = name.lower()
    db = (curr.database or '').lower()
    # Source schemas
    if curr.conn_type == 'dynamics365' or n in ['ccem_source', 'orbis_cloud_source']:
        return 'raw_crm'
    if 'bi_conformed' in n or db == 'bi_conformed':
        return 'ref'
    # Destination schemas
    if db.endswith('_ods') or 'ods' in n:
        return 'ods'
    if db.endswith('_staging') or 'staging' in n:
        return 'staging'
    if db.endswith('_mart_live') or 'mart_live' in n:
        return 'mart_live'
    if db.endswith('_mart') or ('mart' in n and 'mart_live' not in n):
        return 'mart'
    if n == 'csp_arcgis' or (curr.database or '').lower() == 'csp_arcgis':
        return 'csp_arcgis'
    if n == 'dw_admin' or (curr.database or '').lower() == 'dw_admin':
        return 'admin'
    return 'raw'


def infer_host_from_conn_pairs(pairs: Dict[str, str]) -> Optional[str]:
    # Dynamics/HTTP hosts often live in URL-like fields; fallback none
    for key in ['url', 'host', 'server', 'resource', 'organizationurl']:
        if key in pairs and pairs[key]:
            # Try to extract hostname from URL
            val = pairs[key]
            m = re.match(r'^(?:https?://)?([^/]+)', val, flags=re.IGNORECASE)
            return m.group(1) if m else val
    return None


def build_current_from_cm(cm_elem: ET.Element) -> Tuple[str, CurrentConn]:
    name = cm_elem.attrib.get(f'{{{DTS_NS}}}ObjectName') or cm_elem.attrib.get('ObjectName') or 'Connection'
    creation = cm_elem.attrib.get(f'{{{DTS_NS}}}CreationName') or cm_elem.attrib.get('CreationName')
    conn_str = extract_connection_string(cm_elem) or ''
    pairs = parse_conn_string_pairs(conn_str) if conn_str else {}

    conn_type = normalize_type(creation, None)

    server = pairs.get('data source') or pairs.get('server')
    database = pairs.get('initial catalog') or pairs.get('database')
    host = None

    if conn_type == 'dynamics365':
        host = infer_host_from_conn_pairs(pairs)

    return name, CurrentConn(conn_type=conn_type, host=host, server=server, database=database)


def scan_connections_from_project(project_dir: Path) -> Dict[str, CurrentConn]:
    results: Dict[str, CurrentConn] = {}
    for cm_path in sorted(project_dir.glob('*.conmgr')):
        root = read_xml(cm_path)
        if root is None:
            continue
        # Use the outer ConnectionManager (has ObjectName); find nested for connection string
        name, current = build_current_from_cm(root)
        results[name] = current
    return results


def scan_connections_from_analysis(analysis_dir: Path) -> Dict[str, CurrentConn]:
    results: Dict[str, CurrentConn] = {}
    for split_dir in sorted(analysis_dir.glob('*_split')):
        for conn_xml in sorted(split_dir.glob('*.connections.xml')):
            root = read_xml(conn_xml)
            if root is None:
                continue
            for cm in root.findall('.//DTS:ConnectionManager', NS):
                # Only consider outer connection managers that carry an ObjectName
                obj_name = cm.attrib.get(f'{{{DTS_NS}}}ObjectName') or cm.attrib.get('ObjectName')
                if not obj_name:
                    continue
                name, current = build_current_from_cm(cm)
                # Prefer project-level later to override package-level
                if name not in results:
                    results[name] = current
    return results


def choose_project_lakehouse_name(project_dir: Path) -> str:
    # Derive from project folder name, snake_case
    base = project_dir.name
    snake = re.sub(r'[^a-zA-Z0-9]+', '_', base).strip('_').lower()
    return snake or 'lakehouse'


def merge_sources(primary: Dict[str, CurrentConn], secondary: Dict[str, CurrentConn]) -> Dict[str, CurrentConn]:
    merged = dict(secondary)
    merged.update(primary)
    return merged


def build_records(conns: Dict[str, CurrentConn], lakehouse: str) -> Dict[str, ConnRecord]:
    records: Dict[str, ConnRecord] = {}
    for name, curr in conns.items():
        role = infer_role(name, curr)
        schema = infer_schema(name, curr)
        records[name] = ConnRecord(
            name=name,
            current=curr,
            role=role,
            target_lakehouse=lakehouse,
            target_schema=schema,
        )
    return records


def dump_yaml_minimal(records: Dict[str, ConnRecord]) -> str:
    # Try using PyYAML if available for simplicity
    try:
        import yaml  # type: ignore
        payload = {'connections': {}}
        for name, rec in sorted(records.items(), key=lambda kv: kv[0].lower()):
            entry = {
                'role': rec.role,
                'current': {
                    'type': rec.current.conn_type,
                },
                'target': {
                    'lakehouse': rec.target_lakehouse,
                    'schema': rec.target_schema,
                }
            }
            if rec.current.host:
                entry['current']['host'] = rec.current.host
            if rec.current.server:
                entry['current']['server'] = rec.current.server
            if rec.current.database:
                entry['current']['database'] = rec.current.database
            payload['connections'][name] = entry
        return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    except Exception:
        # Manual emitter for our limited schema; quote all scalars
        def q(val: str) -> str:
            # Always single-quote; escape existing quotes
            return "'" + str(val).replace("'", "''") + "'"

        lines: List[str] = []
        lines.append('connections:')
        for name, rec in sorted(records.items(), key=lambda kv: kv[0].lower()):
            lines.append(f"  {name}:")
            lines.append(f"    role: {rec.role}")
            lines.append(f"    current:")
            lines.append(f"      type: {rec.current.conn_type}")
            if rec.current.host:
                lines.append(f"      host: {q(rec.current.host)}")
            if rec.current.server:
                lines.append(f"      server: {q(rec.current.server)}")
            if rec.current.database:
                lines.append(f"      database: {q(rec.current.database)}")
            lines.append(f"    target:")
            lines.append(f"      lakehouse: {q(rec.target_lakehouse)}")
            lines.append(f"      schema: {q(rec.target_schema)}")
        return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description='Generate minimal Fabric YAML from SSIS connections')
    parser.add_argument('--project-dir', default='SSIS_Packages/CONSULAR_ORBIS', help='Path to SSIS project dir containing .conmgr files')
    parser.add_argument('--analysis-dir', default='Analysis_first_step', help='Path to Analysis_first_step directory with *_split folders')
    parser.add_argument('--lakehouse', help='Target Fabric lakehouse name (default: derived from project dir name)')
    parser.add_argument('--output', default='Analysis_first_step/connections_fabric.yaml', help='Output YAML file path')
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    analysis_dir = Path(args.analysis_dir)

    project_conns = scan_connections_from_project(project_dir) if project_dir.is_dir() else {}
    analysis_conns = scan_connections_from_analysis(analysis_dir) if analysis_dir.is_dir() else {}

    merged_conns = merge_sources(project_conns, analysis_conns)
    if not merged_conns:
        print('No connections found. Check input paths.', file=sys.stderr)
        return 1

    lakehouse = args.lakehouse or choose_project_lakehouse_name(project_dir)
    records = build_records(merged_conns, lakehouse)

    yaml_text = dump_yaml_minimal(records)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text, encoding='utf-8')

    print(f"Wrote {len(records)} connections to {out_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
