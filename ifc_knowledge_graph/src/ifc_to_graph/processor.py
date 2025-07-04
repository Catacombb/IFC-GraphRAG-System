"""
IFC to Neo4j Processor

This module coordinates between the IFC parser and Neo4j database components
to process IFC files and populate the graph database.
"""

import logging
import time
import os
from typing import Dict, List, Any, Optional, Tuple, Callable
from datetime import datetime

from .parser import IfcParser
from .database import Neo4jConnector, SchemaManager, IfcToGraphMapper
from .database.performance_monitor import timing_decorator
from .utils.parallel_processor import ParallelProcessor, TaskBatch, parallel_batch_process
from .topology.topologic_analyzer import TopologicAnalyzer
from .database.topologic_to_graph_mapper import TopologicToGraphMapper

# Configure logging
logger = logging.getLogger(__name__)


class IfcProcessor:
    """
    Main processor class that coordinates the conversion of IFC data to Neo4j.
    """
    
    def __init__(
        self, 
        ifc_file_path: str, 
        neo4j_uri: str = "neo4j://localhost:7687", 
        neo4j_username: str = "neo4j",
        neo4j_password: str = "password",
        neo4j_database: Optional[str] = None,
        enable_monitoring: bool = False,
        monitoring_output_dir: Optional[str] = None,
        parallel_processing: bool = False,
        max_workers: Optional[int] = None,
        enable_domain_enrichment: bool = True,
        enable_topological_analysis: bool = False
    ):
        """
        Initialize the processor with file and database connection details.
        
        Args:
            ifc_file_path: Path to the IFC file to process
            neo4j_uri: URI for Neo4j connection
            neo4j_username: Neo4j username
            neo4j_password: Neo4j password
            neo4j_database: Optional Neo4j database name
            enable_monitoring: Whether to enable performance monitoring
            monitoring_output_dir: Directory to save performance reports
            parallel_processing: Whether to enable parallel processing
            max_workers: Maximum number of parallel workers (default: number of CPUs)
            enable_domain_enrichment: Whether to enable domain-specific enrichment
            enable_topological_analysis: Whether to enable topological analysis
        """
        self.ifc_file_path = ifc_file_path
        self.enable_monitoring = enable_monitoring
        self.monitoring_output_dir = monitoring_output_dir
        self.parallel_processing = parallel_processing
        self.max_workers = max_workers
        self.enable_domain_enrichment = enable_domain_enrichment
        self.enable_topological_analysis = enable_topological_analysis
        
        # Create monitoring directory if it doesn't exist
        if enable_monitoring and monitoring_output_dir:
            os.makedirs(monitoring_output_dir, exist_ok=True)
        
        # Initialize parser
        logger.info(f"Initializing IFC parser for {ifc_file_path}")
        self.parser = IfcParser(ifc_file_path)
        
        # Initialize database connection with monitoring
        logger.info(f"Connecting to Neo4j database at {neo4j_uri}")
        self.db_connector = Neo4jConnector(
            uri=neo4j_uri,
            username=neo4j_username,
            password=neo4j_password,
            database=neo4j_database,
            enable_monitoring=enable_monitoring
        )
        
        # Initialize schema manager
        self.schema_manager = SchemaManager(self.db_connector)
        
        # Initialize mapper
        self.mapper = IfcToGraphMapper(self.db_connector)
        
        # Initialize topological analyzer and mapper if enabled
        if self.enable_topological_analysis:
            logger.info("Initializing topological analyzer")
            self.topologic_analyzer = TopologicAnalyzer(self.parser)
            self.topologic_mapper = TopologicToGraphMapper(self.db_connector)
        
        # Statistics
        self.stats = {
            "element_count": 0,
            "relationship_count": 0,
            "property_set_count": 0,
            "material_count": 0,
            "topological_relationship_count": 0,
            "processing_time": 0,
            "start_time": time.time(),
            "end_time": 0,
            "parallel_workers": max_workers or (os.cpu_count() if parallel_processing else 1)
        }
        
        # Initialize parallel processor if enabled
        if self.parallel_processing:
            logger.info(f"Parallel processing enabled with {self.stats['parallel_workers']} workers")
    
    @timing_decorator
    def setup_database(self, clear_existing: bool = False) -> None:
        """
        Set up the Neo4j database schema.
        
        Args:
            clear_existing: Whether to clear existing data
        """
        logger.info("Setting up database schema")
        
        if clear_existing:
            logger.warning("Clearing existing graph data")
            self.mapper.clear_graph()
        
        # Set up schema constraints and indexes
        self.schema_manager.setup_schema()
        logger.info("Database schema setup complete")
    
    def process(
        self, 
        clear_existing: bool = False, 
        batch_size: int = 100,
        save_performance_report: bool = True,
        parallel_batch_size: int = 200
    ) -> Dict[str, Any]:
        """
        Process the IFC file and populate the Neo4j database.
        
        Args:
            clear_existing: Whether to clear existing data
            batch_size: Batch size for processing elements
            save_performance_report: Whether to save a performance report
            parallel_batch_size: Batch size for parallel processing (larger is faster)
            
        Returns:
            Dictionary with processing statistics
        """
        self.stats["start_time"] = time.time()
        start_time = time.time()
        
        # Set up database schema
        self.setup_database(clear_existing)
        
        # Track memory at start
        if self.enable_monitoring:
            self.db_connector.performance_monitor.measure_memory("process_start", {
                "file_path": self.ifc_file_path,
                "batch_size": batch_size,
                "parallel": self.parallel_processing,
                "workers": self.stats["parallel_workers"]
            })
        
        # Process project info
        self._process_project_info()
        
        # Process spatial structure
        self._process_spatial_structure()
        
        # Process elements with batch processing - use larger batch size for better performance
        self._process_elements(batch_size=parallel_batch_size)
        
        # Process relationships
        self._process_relationships(batch_size)
        
        # Process topological relationships if enabled
        if self.enable_topological_analysis:
            self._process_topological_relationships()
        
        # Track memory at end
        if self.enable_monitoring:
            self.db_connector.performance_monitor.measure_memory("process_end", {
                "file_path": self.ifc_file_path,
                "batch_size": batch_size,
                "element_count": self.stats["element_count"],
                "relationship_count": self.stats["relationship_count"],
                "parallel": self.parallel_processing,
                "workers": self.stats["parallel_workers"]
            })
        
        self.stats["processing_time"] = time.time() - start_time
        self.stats["end_time"] = time.time()
        
        logger.info(f"Processing completed in {self.stats['processing_time']:.2f} seconds")
        
        # Log statistics
        logger.info(f"Processed {self.stats['element_count']} elements")
        logger.info(f"Created {self.stats['relationship_count']} relationships")
        logger.info(f"Created {self.stats['property_set_count']} property sets")
        logger.info(f"Created {self.stats['material_count']} materials")
        
        # Log topological statistics if enabled
        if self.enable_topological_analysis:
            logger.info(f"Created {self.stats['topological_relationship_count']} topological relationships")
        
        # Get graph statistics
        node_count = self.mapper.get_node_count()
        rel_count = self.mapper.get_relationship_count()
        logger.info(f"Graph now contains {node_count} nodes and {rel_count} relationships")
        
        self.stats["node_count"] = node_count
        self.stats["relationship_count"] = rel_count
        
        # Save performance report if monitoring is enabled
        if self.enable_monitoring and save_performance_report:
            self._save_performance_report()
        
        return self.stats
    
    @timing_decorator
    def _process_project_info(self) -> None:
        """Process project information."""
        logger.info("Processing project information")
        
        # Get project info from IFC file
        project_info = self.parser.get_project_info()
        
        # Create project node
        if project_info:
            project_info["IFCType"] = "IfcProject"
            self.mapper.create_node_from_element(project_info)
    
    @timing_decorator
    def _process_spatial_structure(self) -> None:
        """Process the spatial structure hierarchy."""
        logger.info("Processing spatial structure")
        
        # Get spatial structure from IFC file
        spatial_structure = self.parser.get_spatial_structure()
        
        # Create nodes for each spatial element
        
        # Process sites
        for site in spatial_structure.get("sites", []):
            site["IFCType"] = "IfcSite"
            site_id = self.mapper.create_node_from_element(site)
            
            # Connect site to project
            if site_id and spatial_structure.get("project", {}).get("GlobalId"):
                self.mapper.create_relationship(
                    spatial_structure["project"]["GlobalId"],
                    site_id,
                    "IfcRelAggregates"
                )
                self.stats["relationship_count"] += 1
        
        # Process buildings
        for building in spatial_structure.get("buildings", []):
            building["IFCType"] = "IfcBuilding"
            building_id = self.mapper.create_node_from_element(building)
            
            # Connect building to site if available, or to project
            if building_id:
                if building.get("SiteGlobalId") and any(site["GlobalId"] == building["SiteGlobalId"] for site in spatial_structure.get("sites", [])):
                    # Connect to site
                    self.mapper.create_relationship(
                        building["SiteGlobalId"],
                        building_id,
                        "IfcRelAggregates"
                    )
                elif spatial_structure.get("project", {}).get("GlobalId"):
                    # Connect to project if no site
                    self.mapper.create_relationship(
                        spatial_structure["project"]["GlobalId"],
                        building_id,
                        "IfcRelAggregates"
                    )
                self.stats["relationship_count"] += 1
        
        # Process storeys
        for storey in spatial_structure.get("storeys", []):
            storey["IFCType"] = "IfcBuildingStorey"
            storey_id = self.mapper.create_node_from_element(storey)
            
            # Connect storey to building
            if storey_id and storey.get("BuildingGlobalId"):
                self.mapper.create_relationship(
                    storey["BuildingGlobalId"],
                    storey_id,
                    "IfcRelAggregates"
                )
                self.stats["relationship_count"] += 1
        
        # Process spaces
        for space in spatial_structure.get("spaces", []):
            space["IFCType"] = "IfcSpace"
            space_id = self.mapper.create_node_from_element(space)
            
            # Connect space to storey
            if space_id and space.get("StoreyGlobalId"):
                self.mapper.create_relationship(
                    space["StoreyGlobalId"],
                    space_id,
                    "IfcRelAggregates"
                )
                self.stats["relationship_count"] += 1
    
    def _process_element_batch(self, element_batch: List[Any]) -> Dict[str, int]:
        """
        Process a batch of IFC elements.
        
        Args:
            element_batch: List of IFC elements to process
            
        Returns:
            Dictionary with processing statistics
        """
        batch_stats = {
            "element_count": 0,
            "property_set_count": 0,
            "material_count": 0,
            "relationship_count": 0
        }
        
        for element in element_batch:
            # Skip if no GlobalId
            if not hasattr(element, "GlobalId"):
                continue
            
            # Extract element attributes
            attributes = self.parser.get_element_attributes(element)
            
            # Create node for element
            element_id = self.mapper.create_node_from_element(attributes)
            
            if element_id:
                batch_stats["element_count"] += 1
                
                # Process property sets
                property_sets = self.parser.get_element_property_sets(element)
                if property_sets:
                    for pset in property_sets:
                        pset_id = self.mapper.create_property_set(pset, element_id)
                        if pset_id:
                            batch_stats["property_set_count"] += 1
                
                # Process materials
                materials = self.parser.get_element_materials(element)
                if materials:
                    for material in materials:
                        material_id = self.mapper.create_material(material, element_id)
                        if material_id:
                            batch_stats["material_count"] += 1
                
                # Connect to spatial structure
                container = self.parser.get_element_container(element)
                if container and container.get("GlobalId"):
                    self.mapper.create_relationship(
                        container["GlobalId"],
                        element_id,
                        "IfcRelContainedInSpatialStructure"
                    )
                    batch_stats["relationship_count"] += 1
        
        return batch_stats
    
    @timing_decorator
    def _process_elements(self, batch_size: int = 100) -> None:
        """
        Process IFC elements, optionally in parallel.
        
        Args:
            batch_size: Number of elements to process in a batch
        """
        logger.info("Processing IFC elements")
        
        # Get all elements
        elements = self.parser.get_elements()
        total_elements = len(elements)
        logger.info(f"Found {total_elements} elements to process")
        
        # Record metric for element count if monitoring enabled
        if self.enable_monitoring:
            self.db_connector.performance_monitor.record_metric(
                name="total_ifc_elements",
                value=total_elements,
                unit="count",
                context={
                    "ifc_file": os.path.basename(self.ifc_file_path),
                    "parallel": self.parallel_processing
                }
            )
        
        if not self.parallel_processing:
            # Process elements in sequential batches
            self._process_elements_sequential(elements, batch_size)
        else:
            # Process elements in parallel batches
            self._process_elements_parallel(elements, batch_size)
    
    def _process_elements_sequential(self, elements: List[Any], batch_size: int) -> None:
        """
        Process elements sequentially in batches.
        
        Args:
            elements: List of IFC elements
            batch_size: Batch size
        """
        total_elements = len(elements)
        
        # Process elements in batches
        for i in range(0, total_elements, batch_size):
            batch = elements[i:i+batch_size]
            batch_start_time = time.time()
            
            logger.info(f"Processing batch of {len(batch)} elements ({i+1}-{min(i+batch_size, total_elements)} of {total_elements})")
            
            # Process the batch
            batch_stats = self._process_element_batch(batch)
            
            # Update statistics
            self.stats["element_count"] += batch_stats["element_count"]
            self.stats["property_set_count"] += batch_stats["property_set_count"]
            self.stats["material_count"] += batch_stats["material_count"]
            self.stats["relationship_count"] += batch_stats["relationship_count"]
            
            # Log batch processing time
            batch_processing_time = time.time() - batch_start_time
            logger.info(
                f"Batch processed in {batch_processing_time:.2f}s "
                f"({len(batch)/batch_processing_time:.2f} elements/s)"
            )
    
    def _process_elements_parallel(self, elements: List[Any], batch_size: int) -> None:
        """
        Process elements in parallel batches.
        
        Args:
            elements: List of IFC elements
            batch_size: Size of each batch
        """
        total_elements = len(elements)
        
        # Create batches for parallel processing
        task_batch = TaskBatch(elements, batch_size, "elements")
        batches = task_batch.get_batches()
        batch_count = len(batches)
        
        logger.info(
            f"Processing {total_elements} elements in {batch_count} batches "
            f"with {self.stats['parallel_workers']} parallel workers"
        )
        
        # Use thread pool for processing (processes would be too expensive for Neo4j connections)
        with ParallelProcessor(
            max_workers=self.max_workers, 
            use_processes=False,
            name="Element Processor"
        ) as processor:
            # Process each batch in parallel and collect results
            all_batch_results = processor.process_batches(
                self._process_element_batch,
                task_batch,
                show_progress=True
            )
            
            # Combine batch statistics
            for batch_stats in all_batch_results:
                self.stats["element_count"] += batch_stats["element_count"]
                self.stats["property_set_count"] += batch_stats["property_set_count"]
                self.stats["material_count"] += batch_stats["material_count"]
                self.stats["relationship_count"] += batch_stats["relationship_count"]
    
    def _process_relationship_batch(self, relationship_batch: List[Dict[str, Any]]) -> int:
        """
        Process a batch of relationships.
        
        Args:
            relationship_batch: List of relationship dictionaries
            
        Returns:
            Number of relationships created
        """
        created_count = 0
        
        for rel in relationship_batch:
            # Skip if missing source or target
            if not rel.get("SourceGlobalId") or not rel.get("TargetGlobalId"):
                continue
            
            # Create the relationship
            success = self.mapper.create_relationship(
                rel["SourceGlobalId"],
                rel["TargetGlobalId"],
                rel["RelationshipType"],
                rel.get("Properties")
            )
            
            if success:
                created_count += 1
        
        return created_count
    
    @timing_decorator
    def _process_relationships(self, batch_size: int = 100) -> None:
        """
        Process relationships between elements, optionally in parallel.
        
        Args:
            batch_size: Batch size for processing relationships
        """
        logger.info("Processing element relationships")
        
        # Get all relationships
        relationships = self.parser.get_relationships()
        total_relationships = len(relationships)
        
        logger.info(f"Found {total_relationships} relationships to process")
        
        # Record metric for relationship count if monitoring enabled
        if self.enable_monitoring:
            self.db_connector.performance_monitor.record_metric(
                name="total_ifc_relationships",
                value=total_relationships,
                unit="count",
                context={
                    "ifc_file": os.path.basename(self.ifc_file_path),
                    "parallel": self.parallel_processing
                }
            )
        
        if not self.parallel_processing:
            # Process relationships sequentially
            self._process_relationships_sequential(relationships, batch_size)
        else:
            # Process relationships in parallel
            self._process_relationships_parallel(relationships, batch_size)
    
    def _process_relationships_sequential(self, relationships: List[Dict[str, Any]], batch_size: int) -> None:
        """
        Process relationships sequentially in batches.
        
        Args:
            relationships: List of relationship dictionaries
            batch_size: Batch size
        """
        total_relationships = len(relationships)
        
        # Process relationships in batches
        for i in range(0, total_relationships, batch_size):
            batch = relationships[i:i+batch_size]
            batch_start_time = time.time()
            
            logger.info(
                f"Processing relationship batch {i//batch_size + 1}/{(total_relationships-1)//batch_size + 1} "
                f"({len(batch)} relationships)"
            )
            
            # Process the batch
            created_count = self._process_relationship_batch(batch)
            
            # Update statistics
            self.stats["relationship_count"] += created_count
            
            # Log batch processing time
            batch_processing_time = time.time() - batch_start_time
            logger.info(
                f"Relationship batch processed in {batch_processing_time:.2f}s "
                f"({len(batch)/batch_processing_time:.2f} relationships/s)"
            )
    
    def _process_relationships_parallel(self, relationships: List[Dict[str, Any]], batch_size: int) -> None:
        """
        Process relationships in parallel batches.
        
        Args:
            relationships: List of relationship dictionaries
            batch_size: Size of each batch
        """
        total_relationships = len(relationships)
        
        # Create batches for parallel processing
        task_batch = TaskBatch(relationships, batch_size, "relationships")
        batches = task_batch.get_batches()
        batch_count = len(batches)
        
        logger.info(
            f"Processing {total_relationships} relationships in {batch_count} batches "
            f"with {self.stats['parallel_workers']} parallel workers"
        )
        
        # Use thread pool for processing
        with ParallelProcessor(
            max_workers=self.max_workers, 
            use_processes=False,
            name="Relationship Processor"
        ) as processor:
            # Process batches in parallel and collect counts
            all_created_counts = processor.process_batches(
                self._process_relationship_batch,
                task_batch,
                show_progress=True
            )
            
            # Sum up created relationships
            self.stats["relationship_count"] += sum(all_created_counts)
    
    @timing_decorator
    def _process_topological_relationships(self) -> None:
        """
        Process topological relationships using the TopologicAnalyzer.
        This extracts implicit spatial relationships from the IFC model geometry.
        """
        if not self.enable_topological_analysis:
            logger.warning("Topological analysis is disabled. Skipping topological relationship processing.")
            return
            
        logger.info("Processing topological relationships")
        
        try:
            # Analyze building topology to get all relationship types
            logger.info("Analyzing building topology")
            topology_results = self.topologic_analyzer.analyze_building_topology()
            
            # Clear existing topological relationships to avoid duplicates
            logger.info("Clearing existing topological relationships")
            self.topologic_mapper.clear_topological_relationships()
            
            # Import the relationships into Neo4j
            logger.info("Importing topological relationships into Neo4j")
            import_stats = self.topologic_mapper.import_all_topological_relationships(topology_results)
            
            # Update statistics
            total_topological_rels = sum(import_stats.values())
            self.stats["topological_relationship_count"] = total_topological_rels
            
            # Log detailed statistics
            logger.info(f"Created {total_topological_rels} topological relationships:")
            for rel_type, count in import_stats.items():
                logger.info(f"  - {rel_type}: {count}")
                
        except Exception as e:
            logger.error(f"Error processing topological relationships: {str(e)}")
            logger.error("Continuing with standard processing")
            
        logger.info("Topological relationship processing complete")
    
    def _save_performance_report(self) -> None:
        """
        Save performance monitoring report and metrics to files.
        """
        if not self.enable_monitoring or not self.monitoring_output_dir:
            return
        
        try:
            # Generate timestamp for filenames
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_filename = os.path.basename(self.ifc_file_path).split('.')[0]
            
            # Save summary report
            report_path = os.path.join(
                self.monitoring_output_dir, 
                f"{base_filename}_perf_report_{timestamp}.txt"
            )
            
            with open(report_path, 'w') as report_file:
                report_file.write(self.db_connector.get_performance_report())
                
                # Add processing statistics
                report_file.write("\n\nProcessing Statistics\n")
                report_file.write("=" * 80 + "\n")
                report_file.write(f"Elements: {self.stats['element_count']}\n")
                report_file.write(f"Relationships: {self.stats['relationship_count']}\n")
                report_file.write(f"Property Sets: {self.stats['property_set_count']}\n")
                report_file.write(f"Materials: {self.stats['material_count']}\n")
                report_file.write(f"Processing Time: {self.stats['processing_time']:.2f} seconds\n")
                report_file.write(f"Nodes in Graph: {self.stats['node_count']}\n")
                report_file.write(f"Relationships in Graph: {self.stats.get('relationship_count', 0)}\n")
                report_file.write(f"Parallel Processing: {'Enabled' if self.parallel_processing else 'Disabled'}\n")
                if self.parallel_processing:
                    report_file.write(f"Parallel Workers: {self.stats['parallel_workers']}\n")
            
            logger.info(f"Performance report saved to {report_path}")
            
            # Export detailed metrics to JSON
            metrics_path = os.path.join(
                self.monitoring_output_dir, 
                f"{base_filename}_perf_metrics_{timestamp}.json"
            )
            
            self.db_connector.export_performance_metrics(metrics_path)
            logger.info(f"Performance metrics exported to {metrics_path}")
            
        except Exception as e:
            logger.error(f"Error saving performance report: {str(e)}")
    
    def close(self) -> None:
        """
        Close database connection and clean up resources.
        """
        if self.db_connector:
            self.db_connector.close() 