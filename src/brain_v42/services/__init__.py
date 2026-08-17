"""brain_v42 services package.

Exports:
    ADRService: Service layer for ADR lifecycle management.
    BrainService: Global semantic search orchestrator.
    ConsolidationJob: Detect near-duplicate entities via embedding similarity.
    DecisionService: Orchestration layer for Decision entities.
    GPUEmbeddingService: GPU-based text embedding via httpx async client.
    GraphService: Neo4j async client for lightweight nodes and explicit relations.
    LearningService: Service layer for Learning entities.
    ProjectContextService: PG-only service for project context management.
    RerankerClient: Async HTTP client for the reranker service.
    RunbookService: Service layer for runbook operations.
    SnippetService: Business logic layer for snippet management.
"""

from __future__ import annotations

from brain_v42.services.adr_service import ADRService
from brain_v42.services.brain_service import BrainService
from brain_v42.services.brain_session_service import BrainSessionService
from brain_v42.services.consolidation import ConsolidationJob
from brain_v42.services.decision_service import DecisionService
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.graph_service import GraphService
from brain_v42.services.learning_service import LearningService
from brain_v42.services.project_context_service import ProjectContextService
from brain_v42.services.reranker_client import RerankerClient
from brain_v42.services.runbook_service import RunbookService
from brain_v42.services.snippet_service import SnippetService

__all__ = [
    "ADRService",
    "BrainService",
    "BrainSessionService",
    "ConsolidationJob",
    "DecisionService",
    "GPUEmbeddingService",
    "GraphService",
    "LearningService",
    "ProjectContextService",
    "RerankerClient",
    "RunbookService",
    "SnippetService",
]
