"""
LLMSkill — abstracción para invocar el LLM desde cualquier agente.

Delega la construcción del cliente a LLMFactory, que selecciona el provider
configurado (ollama | openai | groq | gemini | anthropic).

Capacidades:
  invoke(prompt)                     — texto plano → texto
  chat(messages)                     — lista de mensajes → respuesta
  invoke_with_timeout(prompt, secs)  — invoke con timeout via ThreadPoolExecutor
"""
from __future__ import annotations

import logging

from core.skill_base import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("skill.llm")


def _get_llm():
    from agents.base.llm_factory import get_chat_llm
    return get_chat_llm()


def _get_chat_llm():
    from agents.base.llm_factory import get_chat_llm
    return get_chat_llm()


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
    Capacidad de inferencia LLM. El provider se configura vía LLM_PROVIDER.
    """

    name        = "llm"
    description = "Inferencia LLM configurable: invoke (texto) y chat (mensajes)"
    version     = "2.0.0"

    async def execute(self, input: SkillInput) -> SkillOutput:
        if isinstance(input, InvokeInput):
            return await self.invoke(input)
        if isinstance(input, ChatInput):
            return await self.chat(input)
        return SkillOutput.fail(f"Input tipo '{type(input).__name__}' no soportado por LLMSkill")

    async def invoke(self, input: InvokeInput) -> SkillOutput:
        """Invoca el LLM con un prompt de texto plano."""
        import concurrent.futures

        try:
            llm = _get_llm()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(llm.invoke, input.prompt)
                raw    = future.result(timeout=input.timeout_seconds)

            content = raw.content if hasattr(raw, "content") else str(raw)
            response = content.strip()
            return SkillOutput.ok(
                data=response,
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
        """Invoca el LLM en modo conversacional con lista de mensajes."""
        import concurrent.futures

        try:
            from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
            chat_llm = _get_chat_llm()

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
            return SkillOutput.ok(data=content.strip())
        except concurrent.futures.TimeoutError:
            return SkillOutput.fail("LLM chat timeout (90s)", timeout=True)
        except Exception as exc:
            logger.error(f"[llm_skill] chat error: {exc}")
            return SkillOutput.fail(str(exc))

    async def health_check(self) -> bool:
        """Verifica el LLM haciendo una llamada mínima (non-blocking)."""
        import asyncio

        def _check() -> bool:
            try:
                from config.settings import settings
                if settings.llm_provider == "ollama":
                    import requests as _req
                    resp = _req.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
                    if resp.status_code == 200:
                        models = [m.get("name", "") for m in resp.json().get("models", [])]
                        base = settings.llm_model.split(":")[0]
                        return any(base in m for m in models)
                    return False
                else:
                    llm = _get_llm()
                    result = llm.invoke("ping")
                    return bool(result)
            except Exception as exc:
                logger.warning(f"[llm_skill] health_check falló: {exc}")
                return False

        return await asyncio.to_thread(_check)
