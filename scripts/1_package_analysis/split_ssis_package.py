#!/usr/bin/env python3

import argparse
import os
import sys
from copy import deepcopy
import xml.etree.ElementTree as ET
from xml.dom import minidom

DTS_NS = 'www.microsoft.com/SqlServer/Dts'
NS = {'DTS': DTS_NS}
ET.register_namespace('DTS', DTS_NS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Split an SSIS .dtsx package into subfiles: data flows, per-dataflow internals, individual control flow tasks, control flow, connections, constraints, variables, and data-flow DAG.'
    )
    parser.add_argument('package_path', help='Path to the .dtsx package file')
    parser.add_argument('--out-dir', help='Output directory (default: Analysis_first_step/<package>_split)')
    return parser.parse_args()


def read_package(package_path: str) -> ET.Element:
    tree = ET.parse(package_path)
    return tree.getroot()


def get_attr(elem: ET.Element, attr_name: str):
    return elem.attrib.get(f'{{{DTS_NS}}}{attr_name}')


def extract_data_flows(root: ET.Element):
    data_flows = []
    for exe in root.findall('.//DTS:Executable', NS):
        if get_attr(exe, 'ExecutableType') == 'Microsoft.Pipeline':
            data_flows.append(deepcopy(exe))
    return data_flows


def sanitize_name(name: str) -> str:
    safe = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in name.strip())
    while '__' in safe:
        safe = safe.replace('__', '_')
    return safe.strip('_') or 'dataflow'


def strip_braces_guid(guid: str) -> str:
    if not guid:
        return ''
    return guid.strip('{}')


def prune_pipelines_from_executable(executable_elem: ET.Element):
    # Remove child pipeline "DTS:Executable" nodes from the subtree in-place
    executables_container = executable_elem.find('DTS:Executables', NS)
    if executables_container is None:
        return
    to_remove = []
    for child_exe in executables_container.findall('DTS:Executable', NS):
        if get_attr(child_exe, 'ExecutableType') == 'Microsoft.Pipeline':
            to_remove.append(child_exe)
        else:
            prune_pipelines_from_executable(child_exe)
    for child in to_remove:
        executables_container.remove(child)


def extract_control_flow(root: ET.Element):
    control_flow = []
    for exe in root.findall('.//DTS:Executable', NS):
        if get_attr(exe, 'ExecutableType') != 'Microsoft.Pipeline':
            copy_exe = deepcopy(exe)
            prune_pipelines_from_executable(copy_exe)
            control_flow.append(copy_exe)
    return control_flow


def extract_connections(root: ET.Element):
    conns_root = root.find('DTS:ConnectionManagers', NS)
    if conns_root is None:
        return []
    return [deepcopy(cm) for cm in conns_root.findall('DTS:ConnectionManager', NS)]


def map_package_connections_by_id_and_name(root: ET.Element):
    by_id = {}
    by_name = {}
    for cm in extract_connections(root):
        dtsid = get_attr(cm, 'DTSID')
        name = get_attr(cm, 'ObjectName')
        if dtsid:
            by_id[dtsid] = cm
        if name:
            by_name[name] = cm
    return by_id, by_name


def extract_constraints(root: ET.Element):
    constraints = []
    for pcs in root.findall('.//DTS:PrecedenceConstraints', NS):
        for pc in pcs.findall('DTS:PrecedenceConstraint', NS):
            constraints.append(deepcopy(pc))
    return constraints


def extract_variables(root: ET.Element):
    vars_root = root.find('DTS:Variables', NS)
    if vars_root is None:
        return []
    return [deepcopy(v) for v in vars_root.findall('DTS:Variable', NS)]


def find_project_conmgr_xml(dtsx_dir: str, ref_name: str):
    # Looks for <ref_name>.conmgr next to the package
    candidate = os.path.join(dtsx_dir, f'{ref_name}.conmgr')
    if os.path.isfile(candidate):
        try:
            return ET.parse(candidate).getroot()
        except Exception:
            return None
    return None


def collect_pipeline_connections(executable_df: ET.Element, pkg_dir: str, pkg_conn_by_id, pkg_conn_by_name):
    used = []
    seen_keys = set()
    for conn in executable_df.findall('.//connections/connection'):
        ref_id = conn.attrib.get('connectionManagerRefId')  # e.g., Project.ConnectionManagers[CCEM_SOURCE]
        cm_id = conn.attrib.get('connectionManagerID')       # e.g., {GUID}:external or {GUID}
        key = (ref_id or '') + '|' + (cm_id or '')
        if key in seen_keys:
            continue
        seen_keys.add(key)

        entry = {'refId': ref_id, 'id': cm_id, 'scope': None, 'packageDef': None, 'projectDef': None, 'name': None}

        # Try to parse name from refId
        name = None
        if ref_id:
            # Formats: Project.ConnectionManagers[NAME] or Package.ConnectionManagers[NAME]
            if '[' in ref_id and ref_id.endswith(']'):
                name = ref_id.split('[')[-1][:-1]
            entry['scope'] = 'project' if ref_id.startswith('Project.') else 'package' if ref_id.startswith('Package.') else None
        entry['name'] = name

        # Attempt to resolve package definition by ID
        if cm_id and ':' in cm_id:
            cm_id_clean = cm_id.split(':', 1)[0]
        else:
            cm_id_clean = cm_id
        if cm_id_clean and cm_id_clean in pkg_conn_by_id:
            entry['packageDef'] = deepcopy(pkg_conn_by_id[cm_id_clean])

        # Attempt to resolve package definition by name
        if not entry['packageDef'] and name and name in pkg_conn_by_name:
            entry['packageDef'] = deepcopy(pkg_conn_by_name[name])

        # Attempt to resolve project-level conmgr file
        if (entry['scope'] == 'project' or (ref_id and ref_id.startswith('Project.'))) and name:
            proj_root = find_project_conmgr_xml(pkg_dir, name)
            if proj_root is not None:
                entry['projectDef'] = deepcopy(proj_root)

        used.append(entry)
    return used


def write_dataflow_file(out_path: str, df_exe: ET.Element, used_connections: list):
    df_name = get_attr(df_exe, 'ObjectName') or 'DataFlow'
    df_ref = get_attr(df_exe, 'refId') or ''
    df_id = get_attr(df_exe, 'DTSID') or ''

    wrapper = ET.Element('SSISDataFlow', attrib={
        'name': df_name,
        'refId': df_ref,
        'dtsid': df_id,
    })

    # Extract pipeline node
    obj_data = df_exe.find('DTS:ObjectData', NS)
    if obj_data is not None:
        pipeline = obj_data.find('pipeline')
        if pipeline is not None:
            wrapper.append(deepcopy(pipeline))

    # Add used connections
    conns_elem = ET.SubElement(wrapper, 'UsedConnections')
    for uc in used_connections:
        cref = ET.SubElement(conns_elem, 'ConnectionReference', attrib={
            'refId': uc.get('refId') or '',
            'id': uc.get('id') or '',
            'scope': uc.get('scope') or '',
            'name': uc.get('name') or '',
        })
        if uc.get('packageDef') is not None:
            cref.append(uc['packageDef'])
        if uc.get('projectDef') is not None:
            # Wrap to avoid duplicate top-levels if needed
            proj = ET.SubElement(cref, 'ProjectConnection')
            proj.append(uc['projectDef'])

    pretty_write(wrapper, out_path)


def build_dataflow_dag(root: ET.Element):
    # Map executable ref paths to whether they are dataflow and to names
    df_nodes = {}
    for exe in root.findall('.//DTS:Executable', NS):
        ref = get_attr(exe, 'refId') or ''
        is_df = get_attr(exe, 'ExecutableType') == 'Microsoft.Pipeline'
        if is_df:
            df_nodes[ref] = {
                'name': get_attr(exe, 'ObjectName') or '',
                'dtsid': get_attr(exe, 'DTSID') or '',
            }

    edges = []
    for pc in extract_constraints(root):
        from_ref = get_attr(pc, 'From') or ''
        to_ref = get_attr(pc, 'To') or ''
        if from_ref in df_nodes and to_ref in df_nodes:
            edges.append({
                'from': from_ref,
                'to': to_ref,
                'evalOp': get_attr(pc, 'EvalOp') or '',
                'expression': get_attr(pc, 'Expression') or '',
                'value': get_attr(pc, 'Value') or '',
                'logicalAnd': get_attr(pc, 'LogicalAnd') or '',
            })

    # Build XML
    dag = ET.Element('DataFlowDAG')
    nodes_elem = ET.SubElement(dag, 'Nodes')
    for ref, meta in df_nodes.items():
        ET.SubElement(nodes_elem, 'Node', attrib={
            'refId': ref,
            'name': meta['name'],
            'dtsid': meta['dtsid'],
        })
    edges_elem = ET.SubElement(dag, 'Edges')
    for e in edges:
        ET.SubElement(edges_elem, 'Edge', attrib={
            'from': e['from'],
            'to': e['to'],
            'evalOp': e['evalOp'],
            'expression': e['expression'],
            'value': e['value'],
            'logicalAnd': e['logicalAnd'],
        })
    return dag


def pretty_write(root_elem: ET.Element, out_path: str):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    xml_bytes = ET.tostring(root_elem, encoding='utf-8')
    try:
        reparsed = minidom.parseString(xml_bytes)
        pretty_xml = reparsed.toprettyxml(indent='  ', encoding='utf-8')
    except Exception:
        pretty_xml = xml_bytes
    with open(out_path, 'wb') as f:
        f.write(pretty_xml)


def write_category(elems, out_file_path: str, category: str):
    wrapper = ET.Element('SSISFragments', attrib={'category': category})
    for el in elems:
        wrapper.append(el)
    pretty_write(wrapper, out_file_path)


# Control Flow Task Extraction Functions
def get_task_type_from_executable(executable_elem: ET.Element) -> str:
    """Determine the task type from ExecutableType attribute."""
    exec_type = get_attr(executable_elem, 'ExecutableType')
    if not exec_type:
        return 'Unknown'
    
    # Map common SSIS task types to readable names
    type_mapping = {
        'Microsoft.ExecuteSQLTask': 'ExecuteSQL',
        'Microsoft.ExpressionTask': 'Expression',
        'Microsoft.ScriptTask': 'Script',
        'Microsoft.FileSystemTask': 'FileSystem',
        'Microsoft.FtpTask': 'FTP',
        'Microsoft.WebServiceTask': 'WebService',
        'Microsoft.BulkInsertTask': 'BulkInsert',
        'Microsoft.SendMailTask': 'SendMail',
        'Microsoft.XMLTask': 'XML',
        'Microsoft.DataReaderTask': 'DataReader',
        'STOCK:SEQUENCE': 'SequenceContainer',
        'STOCK:FORLOOP': 'ForLoop',
        'STOCK:FOREACHLOOP': 'ForEachLoop'
    }
    
    return type_mapping.get(exec_type, exec_type.replace('Microsoft.', '').replace('STOCK:', ''))


def extract_container_path(ref_id: str) -> str:
    """Extract the container hierarchy path from a reference ID."""
    if not ref_id:
        return ''
    
    parts = ref_id.split('\\')
    if len(parts) <= 1:
        return ''
    
    # Remove the task name (last part) and the 'Package' prefix
    container_parts = parts[1:-1] if parts[0] == 'Package' else parts[:-1]
    return '\\'.join(container_parts) if container_parts else ''


def extract_control_flow_tasks(root: ET.Element, include_event_handlers: bool = False):
    """Extract individual control flow tasks, excluding data flows."""
    tasks = []
    
    # Extract main package executables
    for exe in root.findall('.//DTS:Executable', NS):
        exec_type = get_attr(exe, 'ExecutableType')
        ref_id = get_attr(exe, 'refId') or ''
        
        # Skip data flow tasks
        if exec_type == 'Microsoft.Pipeline':
            continue
            
        # Skip event handler tasks unless specifically requested
        if not include_event_handlers and '.EventHandlers[' in ref_id:
            continue
        
        # Create a clean copy of the task
        task_copy = deepcopy(exe)
        
        # Remove child data flow tasks from sequence containers
        prune_pipelines_from_executable(task_copy)
        
        tasks.append({
            'element': task_copy,
            'name': get_attr(exe, 'ObjectName') or 'Task',
            'ref_id': ref_id,
            'dts_id': get_attr(exe, 'DTSID') or '',
            'task_type': get_task_type_from_executable(exe),
            'is_event_handler': '.EventHandlers[' in ref_id,
            'container_path': extract_container_path(ref_id)
        })
    
    return tasks


def extract_task_connections(task_elem: ET.Element, pkg_conn_by_id, pkg_conn_by_name, pkg_dir: str):
    """Extract connection references used by a control flow task."""
    used_connections = []
    seen_connections = set()
    
    # Look for connection references in all elements and attributes
    for elem in task_elem.findall('.//*'):
        for attr_name, attr_value in elem.attrib.items():
            # Look for attributes that contain 'Connection' in the name
            if 'connection' in attr_name.lower() and attr_value and attr_value.strip():
                if attr_value in seen_connections:
                    continue
                seen_connections.add(attr_value)
                
                # Try to resolve connection
                conn_info = resolve_connection_reference(attr_value, pkg_conn_by_id, pkg_conn_by_name, pkg_dir)
                if conn_info:
                    used_connections.append(conn_info)
    
    return used_connections


def resolve_connection_reference(conn_ref: str, pkg_conn_by_id, pkg_conn_by_name, pkg_dir: str):
    """Resolve a connection reference to its definition."""
    # Clean connection ID (remove :external suffix if present)
    clean_id = conn_ref.split(':')[0] if ':' in conn_ref else conn_ref
    
    conn_info = {
        'id': conn_ref,
        'clean_id': clean_id,
        'definition': None,
        'type': 'unknown'
    }
    
    # Try to find by ID first
    if clean_id in pkg_conn_by_id:
        conn_info['definition'] = deepcopy(pkg_conn_by_id[clean_id])
        conn_info['type'] = 'package'
    
    return conn_info


def write_control_flow_task_file(out_path: str, task_info: dict, used_connections: list):
    """Write a control flow task to an XML file."""
    task_elem = task_info['element']
    
    wrapper = ET.Element('SSISControlFlowTask', attrib={
        'name': task_info['name'],
        'refId': task_info['ref_id'],
        'dtsid': task_info['dts_id'],
        'taskType': task_info['task_type'],
        'containerPath': task_info['container_path'],
        'isEventHandler': str(task_info['is_event_handler']).lower()
    })
    
    # Add the task element
    wrapper.append(deepcopy(task_elem))
    
    # Add used connections if any
    if used_connections:
        conns_elem = ET.SubElement(wrapper, 'UsedConnections')
        for uc in used_connections:
            cref = ET.SubElement(conns_elem, 'ConnectionReference', attrib={
                'id': uc.get('id', ''),
                'cleanId': uc.get('clean_id', ''),
                'type': uc.get('type', 'unknown')
            })
            if uc.get('definition') is not None:
                cref.append(uc['definition'])
    
    pretty_write(wrapper, out_path)


def generate_summary_report(tasks: list, out_dir: str):
    """Generate a summary report of extracted control flow tasks."""
    report_lines = [
        "# Control Flow Tasks Summary",
        f"Total tasks extracted: {len(tasks)}",
        "",
        "## Task Types Distribution"
    ]
    
    # Count by task type
    type_counts = {}
    container_counts = {}
    event_handler_count = 0
    
    for task in tasks:
        task_type = task['task_type']
        type_counts[task_type] = type_counts.get(task_type, 0) + 1
        
        if task['is_event_handler']:
            event_handler_count += 1
        
        container_path = task['container_path']
        if container_path:
            container_counts[container_path] = container_counts.get(container_path, 0) + 1
    
    for task_type, count in sorted(type_counts.items()):
        report_lines.append(f"- {task_type}: {count}")
    
    if event_handler_count > 0:
        report_lines.extend([
            "",
            f"## Event Handlers: {event_handler_count}"
        ])
    
    if container_counts:
        report_lines.extend([
            "",
            "## Tasks by Container"
        ])
        for container, count in sorted(container_counts.items()):
            report_lines.append(f"- {container}: {count}")
    
    # Write report
    report_path = os.path.join(out_dir, 'control_flow_tasks_summary.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(report_lines))
    
    print(f'Generated control flow summary report: {report_path}')


def main():
    args = parse_args()
    package_path = os.path.abspath(args.package_path)
    if not os.path.isfile(package_path):
        raise FileNotFoundError(f'Package not found: {package_path}')

    pkg_dir = os.path.dirname(package_path)
    pkg_base = os.path.splitext(os.path.basename(package_path))[0]
    
    # Default output directory is in Analysis_first_step
    if args.out_dir:
        out_dir = os.path.abspath(args.out_dir)
    else:
        # Find the project root (where Analysis_first_step is located)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(script_dir))  # Go up two levels from scripts/1_package_analysis
        out_dir = os.path.join(project_root, 'Analysis_first_step', f'{pkg_base}_split')

    root = read_package(package_path)

    data_flows = extract_data_flows(root)
    control_flow = extract_control_flow(root)
    connections = extract_connections(root)
    constraints = extract_constraints(root)
    variables = extract_variables(root)

    outputs = [
        (data_flows, os.path.join(out_dir, f'{pkg_base}.data_flows.xml'), 'data_flows'),
        (control_flow, os.path.join(out_dir, f'{pkg_base}.control_flow.xml'), 'control_flow'),
        (connections, os.path.join(out_dir, f'{pkg_base}.connections.xml'), 'connections'),
        (constraints, os.path.join(out_dir, f'{pkg_base}.constraints.xml'), 'constraints'),
        (variables, os.path.join(out_dir, f'{pkg_base}.variables.xml'), 'variables'),
    ]

    for elems, path, category in outputs:
        write_category(elems, path, category)
        print(f'Wrote {category}: {path} ({len(elems)} items)')

    # Write per-data-flow internals and DAG
    df_dir = os.path.join(out_dir, 'dataflows')
    os.makedirs(df_dir, exist_ok=True)
    pkg_conn_by_id, pkg_conn_by_name = map_package_connections_by_id_and_name(root)

    name_counts = {}
    for df_exe in data_flows:
        name = get_attr(df_exe, 'ObjectName') or 'DataFlow'
        dtsid = strip_braces_guid(get_attr(df_exe, 'DTSID') or '')
        base = f"{sanitize_name(name)}_{dtsid}" if dtsid else sanitize_name(name)
        # Ensure unique filename
        count = name_counts.get(base, 0)
        name_counts[base] = count + 1
        if count > 0:
            file_base = f"{base}_{count+1}"
        else:
            file_base = base
        out_path = os.path.join(df_dir, f'{file_base}.xml')

        used_conns = collect_pipeline_connections(df_exe, pkg_dir, pkg_conn_by_id, pkg_conn_by_name)
        write_dataflow_file(out_path, df_exe, used_conns)
        print(f'Wrote dataflow: {out_path}')

    dag_xml = build_dataflow_dag(root)
    dag_path = os.path.join(out_dir, f'{pkg_base}.dataflow_dag.xml')
    pretty_write(dag_xml, dag_path)
    print(f'Wrote dataflow DAG: {dag_path}')

    # Extract and write individual control flow tasks
    print('\n--- Extracting Control Flow Tasks ---')
    control_flow_tasks = extract_control_flow_tasks(root, include_event_handlers=True)
    
    # Create controlflows directory
    cf_dir = os.path.join(out_dir, 'controlflows')
    os.makedirs(cf_dir, exist_ok=True)
    
    # Write individual control flow task files
    cf_name_counts = {}
    for task_info in control_flow_tasks:
        name = task_info['name']
        task_type = task_info['task_type']
        dtsid = strip_braces_guid(task_info['dts_id'])
        
        # Create filename with type prefix and GUID
        base_name = f"{task_type}_{sanitize_name(name)}"
        if dtsid:
            base_name = f"{base_name}_{dtsid}"
        
        # Ensure unique filename
        count = cf_name_counts.get(base_name, 0)
        cf_name_counts[base_name] = count + 1
        if count > 0:
            file_name = f"{base_name}_{count+1}.xml"
        else:
            file_name = f"{base_name}.xml"
        
        out_path = os.path.join(cf_dir, file_name)
        
        # Extract connections used by this task
        used_connections = extract_task_connections(task_info['element'], 
                                                  pkg_conn_by_id, 
                                                  pkg_conn_by_name, 
                                                  pkg_dir)
        
        write_control_flow_task_file(out_path, task_info, used_connections)
    
    # Generate summary report
    generate_summary_report(control_flow_tasks, out_dir)
    
    print(f'Extracted {len(control_flow_tasks)} control flow tasks to: {cf_dir}')


if __name__ == '__main__':
    main()
