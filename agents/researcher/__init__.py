"""
agents.researcher — Research Agent package.

Módulos:
  rag_retriever — RAG sobre colecciones Qdrant per-user (similarity_search)
  web_searcher  — DuckDuckGo + fast-path tipo de cambio
  agent         — ResearchAgent(BaseAgent) registrado en AgentRegistry
"""
from agents.researcher.agent import ResearchAgent
from agents.researcher.rag_retriever import (
    retrieve_documents,
    ingest_document,
    get_user_vectorstore,
    list_user_documents,
    delete_user_collection,
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
