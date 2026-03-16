"""
Utilidades compartidas para agentes LLM especializados (CTO, DEV, ARCH y similares).

Centraliza:
  - _invoke_llm()          — invoca ChatOllama singleton con separación system/question
  - _build_prompt()        — ensambla system prompt + contexto RAG + pregunta
  - _retrieve_rag_context() — recupera contexto RAG sin bloquear el event loop
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("agents.base.llm_utils")


async def invoke_llm(prompt: str, context: Any, agent_name: str = "agent") -> str:
    """
    Invoca el LLM con separación de system message y pregunta.

    Estrategia:
      1. ChatOllama singleton vía skills/llm/skill._get_chat_ollama()
      2. Fallback: OllamaLLM del contexto (context.llm)

    Args:
        prompt:      Prompt completo (system + secciones ## separadas).
        context:     AgentContext con context.llm como fallback.
        agent_name:  Nombre del agente para logging.

    Returns:
        Respuesta del LLM como string.
    """
    # Separar system message del resto: todo antes del primer "## " es sistema
    import re
    match = re.search(r'^##\s', prompt, re.MULTILINE)
    if match:
        system_text   = prompt[:match.start()].strip()
        question_text = prompt[match.start():].strip()
    else:
        system_text   = ""
        question_text = prompt.strip()

    # Intentar vía ChatOllama singleton
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from skills.llm.skill import _get_chat_ollama

        chat_llm = _get_chat_ollama()
        messages = [SystemMessage(content=system_text), HumanMessage(content=question_text)]
        result   = await asyncio.to_thread(chat_llm.invoke, messages)
        return result.content if hasattr(result, "content") else str(result)

    except Exception as exc:
        logger.debug(f"[{agent_name}] ChatOllama singleton falló, usando context.llm: {exc}")

    # Fallback: OllamaLLM del contexto
    if context.llm is not None:
        result = await asyncio.to_thread(context.llm.invoke, prompt)
        return str(result)

    raise RuntimeError(f"[{agent_name}] No hay LLM disponible en el contexto")


def build_prompt(
    system: str,
    question: str,
    rag_context: str = "",
    context_header: str = "## Contexto relevante",
    question_header: str = "## Pregunta",
) -> str:
    """
    Ensambla el prompt completo: system + (contexto RAG opcional) + pregunta.

    Args:
        system:          System prompt del agente.
        question:        Pregunta o tarea del usuario.
        rag_context:     Texto recuperado de Qdrant (vacío si no hay hits).
        context_header:  Encabezado de la sección de contexto RAG.
        question_header: Encabezado de la sección de pregunta.

    Returns:
        Prompt listo para enviar al LLM.
    """
    parts = [system, ""]
    if rag_context:
        parts += [context_header, rag_context, ""]
    parts += [question_header, question]
    return "\n".join(parts)


async def retrieve_rag_context(
    user_email: str,
    query: str,
    k: int = 4,
    agent_name: str = "agent",
) -> str:
    """
    Recupera contexto RAG del usuario sin bloquear el event loop.

    Args:
        user_email: Email del usuario (colección Qdrant).
        query:      Consulta para la búsqueda semántica.
        k:          Número de chunks a recuperar.
        agent_name: Nombre del agente para logging.

    Returns:
        Texto de contexto concatenado, o "" si no hay hits / falla.
    """
    if not user_email:
        return ""
    try:
        from agents.researcher.rag_retriever import retrieve_documents
        result = await asyncio.to_thread(retrieve_documents, user_email, query, k)
        return result or ""
    except Exception as exc:
        logger.debug(f"[{agent_name}] RAG no disponible: {exc}")
        return ""
