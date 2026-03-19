"""
LLMSkill — abstracción para invocar el LLM (Ollama) desde cualquier agente.

Capacidades:
  invoke(prompt)                     — texto plano → texto (OllamaLLM)
  chat(messages)                     — lista de mensajes → respuesta (ChatOllama)
  invoke_with_timeout(prompt, secs)  — invoke con timeout via ThreadPoolExecutor

Un único singleton por proceso para OllamaLLM y ChatOllama (mismo patrón
que planner.py y supervisor.py — evita reconexiones por request).
"""
from __future__ import annotations

import logging
import os

from core.skill_base import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("skill.llm")

_OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama-service:11434")
_MODEL_NAME = os.environ.get("MODEL_NAME", "qwen2.5:14b")

# Singletons — lazy init al primer uso
_ollama_llm  = None
_chat_ollama = None


def _get_ollama_llm():
    global _ollama_llm
    if _ollama_llm is None:
        from langchain_ollama import OllamaLLM
        _ollama_llm = OllamaLLM(model=_MODEL_NAME, base_url=_OLLAMA_URL)
    return _ollama_llm


def _get_chat_ollama():
    global _chat_ollama
    if _chat_ollama is None:
        from langchain_ollama import ChatOllama
        _chat_ollama = ChatOllama(model=_MODEL_NAME, base_url=_OLLAMA_URL)
    return _chat_ollama


# ── Inputs ────────────────────────────────────────────────────────────────────

class InvokeInput(SkillInput):
    prompt: str
    timeout_seconds: int = 60

class ChatInput(SkillInput):
    messages: list[dict[str, str]]
    """Lista de {"role": "user"|"system"|"assistant", "content": str}"""
    temperature: float = 0.7


# ── Skill ─────────────────────────────────────────────────────────────────────

class LLMSkill(BaseSkill):
    """
    Capacidad de inferencia LLM sobre Ollama.

    OllamaLLM  → invoke(prompt): texto plano, ideal para RAG, postmortems, análisis
    ChatOllama → chat(messages): conversacional con separación system/user/assistant
    """

    name        = "llm"
    description = "Inferencia LLM vía Ollama: invoke (texto) y chat (mensajes)"
    version     = "1.0.0"

    async def execute(self, input: SkillInput) -> SkillOutput:
        if isinstance(input, InvokeInput):
            return await self.invoke(input)
        if isinstance(input, ChatInput):
            return await self.chat(input)
        return SkillOutput.fail(f"Input tipo '{type(input).__name__}' no soportado por LLMSkill")

    async def invoke(self, input: InvokeInput) -> SkillOutput:
        """
        Invoca el LLM con un prompt de texto plano.
        Usa ThreadPoolExecutor para aplicar timeout sin bloquear el event loop.
        """
        import concurrent.futures

        try:
            llm = _get_ollama_llm()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(llm.invoke, input.prompt)
                raw    = future.result(timeout=input.timeout_seconds)

            response = raw.strip() if isinstance(raw, str) else str(raw)
            return SkillOutput.ok(
                data=response,
                model=_MODEL_NAME,
                tokens_approx=len(response.split()),
            )
        except concurrent.futures.TimeoutError:
            logger.warning(f"[llm_skill] invoke timeout ({input.timeout_seconds}s)")
            return SkillOutput.fail(
                f"LLM timeout tras {input.timeout_seconds}s",
                timeout=True,
            )
        except Exception as exc:
            logger.error(f"[llm_skill] invoke error: {exc}")
            return SkillOutput.fail(str(exc))

    async def chat(self, input: ChatInput) -> SkillOutput:
        """
        Invoca el LLM en modo conversacional con lista de mensajes.
        Mantiene separación semántica system/user/assistant (anti-prompt-injection).
        """
        import concurrent.futures

        try:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
            chat_llm = _get_chat_ollama()

            lc_messages = []
            for msg in input.messages:
                role    = msg.get("role", "user").lower()
                content = msg.get("content", "")
                if role == "system":
                    lc_messages.append(SystemMessage(content=content))
                elif role == "assistant":
                    lc_messages.append(AIMessage(content=content))
                else:
                    lc_messages.append(HumanMessage(content=content))

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future   = ex.submit(chat_llm.invoke, lc_messages)
                response = future.result(timeout=90)

            content = response.content if hasattr(response, "content") else str(response)
            return SkillOutput.ok(
                data=content.strip(),
                model=_MODEL_NAME,
            )
        except concurrent.futures.TimeoutError:
            return SkillOutput.fail("LLM chat timeout (90s)", timeout=True)
        except Exception as exc:
            logger.error(f"[llm_skill] chat error: {exc}")
            return SkillOutput.fail(str(exc))

    async def health_check(self) -> bool:
        """Verifica que Ollama tiene el modelo disponible via /api/tags."""
        try:
            import requests as _req
            resp = _req.get(f"{_OLLAMA_URL}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                # Acepta coincidencia parcial (qwen2.5:14b o qwen2.5)
                base = _MODEL_NAME.split(":")[0]
                return any(base in m for m in models)
        except Exception as exc:
            logger.warning(f"[llm_skill] health_check falló: {exc}")
        return False
