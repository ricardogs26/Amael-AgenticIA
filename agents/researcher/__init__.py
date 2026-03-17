"""
agents.researcher — Research Agent package.

Módulos:
  rag_retriever — RAG sobre colecciones Qdrant per-user (similarity_search)
  web_searcher  — DuckDuckGo + fast-path tipo de cambio
  agent         — ResearchAgent(BaseAgent) registrado en AgentRegistry
"""
from agents.researcher.agent import ResearchAgent
from agents.researcher.rag_retriever import (
    delete_user_collection,
    get_user_vectorstore,
    ingest_document,
    list_user_documents,
    retrieve_documents,
)
from agents.researcher.web_searcher import web_search

__all__ = [
    "ResearchAgent",
    "retrieve_documents",
    "ingest_document",
    "get_user_vectorstore",
    "list_user_documents",
    "delete_user_collection",
    "web_search",
]
