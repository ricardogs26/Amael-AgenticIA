"""
WebSkill — búsqueda web y fetch de URLs.

Capacidades:
  search(query)    — DuckDuckGo + fast-path tipo de cambio
  fetch_url(url)   — descarga el contenido de una URL (texto plano, sin JS)

Wrapper sobre agents/researcher/web_searcher + httpx.
"""
from __future__ import annotations

import logging

from core.skill_base import BaseSkill, SkillInput, SkillOutput

logger = logging.getLogger("skill.web")

# Persistent httpx client — reutilizado en toda la vida del proceso
_http_client = None


def _get_http():
    global _http_client
    if _http_client is None:
        import httpx
        _http_client = httpx.Client(timeout=15.0, follow_redirects=True)
    return _http_client


# ── Inputs ────────────────────────────────────────────────────────────────────

class SearchInput(SkillInput):
    query: str
    max_results: int = 5

class FetchUrlInput(SkillInput):
    url: str
    max_chars: int = 5000


# ── Skill ─────────────────────────────────────────────────────────────────────

class WebSkill(BaseSkill):
    """
    Capacidad de búsqueda web y descarga de URLs.
    Usada por ResearchAgent para el step WEB_SEARCH del Planner.
    """

    name        = "web"
    description = "Búsqueda web (DuckDuckGo + tipo de cambio) y fetch de URLs"
    version     = "1.0.0"

    async def execute(self, input: SkillInput) -> SkillOutput:
        if isinstance(input, SearchInput):
            return await self.search(input)
        if isinstance(input, FetchUrlInput):
            return await self.fetch_url(input)
        return SkillOutput.fail(f"Input tipo '{type(input).__name__}' no soportado por WebSkill")

    async def search(self, input: SearchInput) -> SkillOutput:
        """
        Busca en la web usando DuckDuckGo.
        Fast-path para queries de tipo de cambio/divisas vía open.er-api.com.
        """
        try:
            from agents.researcher.web_searcher import web_search
            result = web_search(input.query)
            if not result or "Error en búsqueda web" in result:
                return SkillOutput.fail(result or "Sin resultados", query=input.query)
            return SkillOutput.ok(
                data=result,
                query=input.query,
                source="duckduckgo",
            )
        except Exception as exc:
            logger.error(f"[web_skill] search error: {exc}")
            return SkillOutput.fail(str(exc))

    async def fetch_url(self, input: FetchUrlInput) -> SkillOutput:
        """
        Descarga el contenido de una URL y retorna texto plano (sin HTML).
        Trunca a max_chars para evitar saturar el contexto del LLM.
        """
        try:
            resp = _get_http().get(input.url, timeout=10.0)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "html" in content_type:
                # Extracción básica de texto sin dependencia de beautifulsoup
                import re
                text = re.sub(r"<[^>]+>", " ", resp.text)
                text = re.sub(r"\s+", " ", text).strip()
            else:
                text = resp.text.strip()

            truncated = len(text) > input.max_chars
            text      = text[: input.max_chars]
            return SkillOutput.ok(
                data=text,
                url=input.url,
                truncated=truncated,
                status_code=resp.status_code,
            )
        except Exception as exc:
            logger.error(f"[web_skill] fetch_url error: {exc}")
            return SkillOutput.fail(str(exc), url=input.url)

    async def health_check(self) -> bool:
        """Verifica conectividad básica a internet (non-blocking)."""
        import asyncio

        def _check() -> bool:
            try:
                resp = _get_http().get("https://duckduckgo.com", timeout=5.0)
                return resp.status_code < 500
            except Exception as exc:
                logger.warning(f"[web_skill] health_check falló: {exc}")
                return False

        return await asyncio.to_thread(_check)
