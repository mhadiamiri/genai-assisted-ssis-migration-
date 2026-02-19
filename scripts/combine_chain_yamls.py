#!/usr/bin/env python3
"""
SSIS Migration: Combine Chain YAML Files

This script combines all the individual YAML files from the Analysis_third_step directory
chains while preserving the original container and package information from the 
planned YAML files in Analysis_second_step.

The script will:
1. Read all planned YAML files from Analysis_second_step
2. Find corresponding chain YAML files in Analysis_third_step
3. Combine them into a single consolidated YAML structure
4. Preserve package name, container name, and chain information
"""

import os
import yaml
import argparse
from pathlib import Path
from typing import Dict, List, Any
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ChainYAMLCombiner:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.analysis_second_step = self.project_root / "Analysis_second_step"
        self.analysis_third_step = self.project_root / "Analysis_third_step"
        self.combined_data = {}
        
    def load_planned_yaml(self, file_path: Path) -> Dict[str, Any]:
        """Load a planned YAML file and return its contents."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Error loading planned YAML {file_path}: {e}")
            return {}
    
    def load_chain_yaml(self, file_path: Path) -> Dict[str, Any]:
        """Load a chain YAML file and return its contents."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Error loading chain YAML {file_path}: {e}")
            return {}
    
    def _names_match(self, step_name: str, yaml_key: str) -> bool:
        """Check if a step name matches a YAML file key using fuzzy matching."""
        # Normalize both names for comparison
        step_normalized = step_name.lower().replace(" ", "").replace("-", "").replace("_", "")
        yaml_normalized = yaml_key.lower().replace(" ", "").replace("-", "").replace("_", "")
        
        # Check if one contains the other or if they're very similar
        return (step_normalized in yaml_normalized or 
                yaml_normalized in step_normalized or
                step_normalized == yaml_normalized)
    
    def find_chain_yamls(self, container_name: str) -> Dict[str, List[Path]]:
        """Find all chain YAML files for a given container."""
        chain_files = {}
        
        # Look for container directory in Analysis_third_step
        # Try exact match first
        container_dir = self.analysis_third_step / container_name
        
        # If exact match not found, try some common naming variations
        if not container_dir.exists():
            # Try removing spaces and special characters
            normalized_name = container_name.replace(" ", "_").replace("-", "_")
            container_dir = self.analysis_third_step / normalized_name
            
            # If still not found, try fuzzy matching
            if not container_dir.exists():
                # Look for similar directory names
                for potential_dir in self.analysis_third_step.iterdir():
                    if potential_dir.is_dir():
                        # Compare normalized names
                        potential_normalized = potential_dir.name.replace(" ", "_").replace("-", "_")
                        container_normalized = container_name.replace(" ", "_").replace("-", "_")
                        
                        if potential_normalized.lower() == container_normalized.lower():
                            container_dir = potential_dir
                            logger.info(f"Found matching directory: {container_name} -> {potential_dir.name}")
                            break
        
        if not container_dir.exists():
            logger.warning(f"Container directory not found: {container_name}")
            return chain_files
        
        # Find all chain directories
        for chain_dir in container_dir.iterdir():
            if chain_dir.is_dir() and chain_dir.name.startswith("Chain_"):
                chain_name = chain_dir.name
                yaml_files = list(chain_dir.glob("*.yml"))
                if yaml_files:
                    chain_files[chain_name] = yaml_files
                    logger.info(f"Found {len(yaml_files)} YAML files in {container_name}/{chain_name}")
        
        return chain_files
    
    def combine_container_data(self, package_name: str, container_name: str, 
                             container_data: Dict[str, Any], chain_files: Dict[str, List[Path]]) -> Dict[str, Any]:
        """Combine container data with chain YAML files."""
        combined_container = {
            "package_name": package_name,
            "container_name": container_name,
            "flow_count": container_data.get("flow_count", 0),
            "execution_chains": []
        }
        
        # Process each execution chain
        for chain_info in container_data.get("execution_chains", []):
            chain_name = chain_info.get("chain_name", "")
            chain_key = chain_name.replace(" ", "_")  # Convert "Chain 1" to "Chain_1"
            
            # Build tasks directly from YAML definitions in execution order
            tasks_list = []
            
            if chain_key in chain_files:
                # Create a mapping of YAML files by their base names for quick lookup
                yaml_files_by_name = {}
                for yaml_file in chain_files[chain_key]:
                    yaml_files_by_name[yaml_file.stem] = yaml_file
                
                # Process steps in the order they appear in the planned YAML
                for step in chain_info.get("steps", []):
                    if step.get("type") == "control_flow":
                        # Add control flow tasks as simple entries
                        control_task = {
                            step.get("name", "unknown_control_task"): {
                                "type": "control_flow",
                                "task_type": step.get("task_type", "ExecuteSQL")
                            }
                        }
                        tasks_list.append(control_task)
                        logger.debug(f"Added control flow task: {step.get('name')}")
                        
                    elif step.get("type") == "data_flow":
                        step_name = step.get("name", "")
                        # Try to find matching YAML file
                        for yaml_key, yaml_file in yaml_files_by_name.items():
                            # Match by checking if the step name is contained in the YAML filename
                            if (step_name.replace(" ", "_").replace("-", "_") in yaml_key or
                                yaml_key.replace("_", " ").replace("-", " ") in step_name or
                                self._names_match(step_name, yaml_key)):
                                yaml_content = self.load_chain_yaml(yaml_file)
                                if yaml_content:
                                    tasks_list.append(yaml_content)
                                    logger.debug(f"Added YAML task definition for {yaml_key} (matched to task: {step_name})")
                                # Remove from remaining files to avoid duplicates
                                del yaml_files_by_name[yaml_key]
                                break
                
                # Add any remaining YAML files that didn't match tasks
                for yaml_key, yaml_file in yaml_files_by_name.items():
                    yaml_content = self.load_chain_yaml(yaml_file)
                    if yaml_content:
                        tasks_list.append(yaml_content)
                        logger.debug(f"Added remaining YAML task definition for {yaml_key}")
            else:
                logger.warning(f"No chain files found for {container_name}/{chain_name}")
                # If no YAML files, just add control flow tasks from planned YAML
                for step in chain_info.get("steps", []):
                    if step.get("type") == "control_flow":
                        control_task = {
                            step.get("name", "unknown_control_task"): {
                                "type": "control_flow", 
                                "task_type": step.get("task_type", "ExecuteSQL")
                            }
                        }
                        tasks_list.append(control_task)
            
            combined_chain = {
                "chain_name": chain_name,
                "tasks": tasks_list
            }
            
            combined_container["execution_chains"].append(combined_chain)
        
        return combined_container
    
    def process_planned_yaml(self, planned_file: Path):
        """Process a single planned YAML file."""
        logger.info(f"Processing planned YAML: {planned_file.name}")
        
        planned_data = self.load_planned_yaml(planned_file)
        if not planned_data:
            return
        
        package_name = planned_data.get("package_name", planned_file.stem)
        
        # Initialize package in combined data
        if package_name not in self.combined_data:
            self.combined_data[package_name] = {
                "package_name": package_name,
                "total_containers": planned_data.get("total_containers", 0),
                "total_flows": planned_data.get("total_flows", 0),
                "containers": {}
            }
        
        # Process each container
        for container_name, container_data in planned_data.get("containers", {}).items():
            logger.info(f"Processing container: {container_name}")
            
            # Find corresponding chain YAML files
            chain_files = self.find_chain_yamls(container_name)
            
            # Combine container data with chain files
            combined_container = self.combine_container_data(
                package_name, container_name, container_data, chain_files
            )
            
            self.combined_data[package_name]["containers"][container_name] = combined_container
    
    def run(self) -> Dict[str, Any]:
        """Main method to combine all YAML files."""
        logger.info("Starting YAML combination process...")
        
        # Find all planned YAML files
        planned_files = list(self.analysis_second_step.glob("*.yml"))
        if not planned_files:
            logger.error(f"No planned YAML files found in {self.analysis_second_step}")
            return {}
        
        logger.info(f"Found {len(planned_files)} planned YAML files")
        
        # Process each planned YAML file
        for planned_file in planned_files:
            self.process_planned_yaml(planned_file)
        
        logger.info("YAML combination process completed")
        return self.combined_data
    
    def save_combined_yaml(self, output_file: Path):
        """Save the combined data to a YAML file."""
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                yaml.dump(self.combined_data, f, default_flow_style=False, 
                         allow_unicode=True, sort_keys=False, indent=2)
            logger.info(f"Combined YAML saved to: {output_file}")
        except Exception as e:
            logger.error(f"Error saving combined YAML: {e}")
    
    def generate_summary(self) -> Dict[str, Any]:
        """Generate a summary of the combination process."""
        summary = {
            "total_packages": len(self.combined_data),
            "packages": {}
        }
        
        for package_name, package_data in self.combined_data.items():
            package_summary = {
                "total_containers": len(package_data["containers"]),
                "total_flows": package_data.get("total_flows", 0),
                "containers": {}
            }
            
            for container_name, container_data in package_data["containers"].items():
                container_summary = {
                    "execution_chains": len(container_data["execution_chains"]),
                    "yaml_files_found": 0
                }
                
                for chain in container_data["execution_chains"]:
                    # Count YAML tasks (exclude control flow tasks)
                    yaml_tasks = [task for task in chain["tasks"] if not (isinstance(task, dict) and any("type" in v and v.get("type") == "control_flow" for v in task.values() if isinstance(v, dict)))]
                    container_summary["yaml_files_found"] += len(yaml_tasks)
                
                package_summary["containers"][container_name] = container_summary
            
            summary["packages"][package_name] = package_summary
        
        return summary


def main():
    parser = argparse.ArgumentParser(description="Combine SSIS chain YAML files")
    parser.add_argument("--project-root", 
                       default="/Users/sam/CodeBase/CL_SSIS",
                       help="Root directory of the SSIS project")
    parser.add_argument("--output", 
                       default="combined_chain_yamls.yml",
                       help="Output file name for combined YAML")
    parser.add_argument("--summary", 
                       default="combination_summary.yml",
                       help="Output file name for summary")
    parser.add_argument("--output-dir",
                       default="Analysis_fourth_step", 
                       help="Output directory for generated files")
    parser.add_argument("--verbose", "-v", 
                       action="store_true",
                       help="Enable verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Initialize combiner
    combiner = ChainYAMLCombiner(args.project_root)
    
    # Run combination process
    combined_data = combiner.run()
    
    if not combined_data:
        logger.error("No data was combined. Exiting.")
        return 1
    
    # Create output directory if it doesn't exist
    output_dir = Path(args.project_root) / args.output_dir
    output_dir.mkdir(exist_ok=True)
    
    # Save combined YAML
    output_path = output_dir / args.output
    combiner.save_combined_yaml(output_path)
    
    # Generate and save summary
    summary = combiner.generate_summary()
    summary_path = output_dir / args.summary
    
    try:
        with open(summary_path, 'w', encoding='utf-8') as f:
            yaml.dump(summary, f, default_flow_style=False, 
                     allow_unicode=True, sort_keys=False, indent=2)
        logger.info(f"Summary saved to: {summary_path}")
    except Exception as e:
        logger.error(f"Error saving summary: {e}")
    
    # Print summary to console
    print("\n" + "="*60)
    print("COMBINATION SUMMARY")
    print("="*60)
    print(f"Total packages processed: {summary['total_packages']}")
    
    for package_name, package_info in summary["packages"].items():
        print(f"\nPackage: {package_name}")
        print(f"  Containers: {package_info['total_containers']}")
        print(f"  Total flows: {package_info['total_flows']}")
        
        for container_name, container_info in package_info["containers"].items():
            print(f"    {container_name}:")
            print(f"      Chains: {container_info['execution_chains']}")
            print(f"      YAML files: {container_info['yaml_files_found']}")
    
    print(f"\nOutput files:")
    print(f"  Combined YAML: {output_path}")
    print(f"  Summary: {summary_path}")
    
    return 0


if __name__ == "__main__":
    exit(main())
