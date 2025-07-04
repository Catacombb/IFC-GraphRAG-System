"""
Topology module for analyzing spatial relationships in IFC models.

This module provides functionality to analyze topological relationships
between building elements using TopologicPy, including adjacency, 
containment, and connectivity.
"""

from .topologic_analyzer import TopologicAnalyzer

__all__ = ["TopologicAnalyzer"]