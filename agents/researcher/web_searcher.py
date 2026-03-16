"""
Web Searcher — búsqueda web con fast-path para tipo de cambio.

Migrado desde backend-ia/main.py → _web_search():
  - Fast path: open.er-api.com para queries de divisas/tipo de cambio
  - General: DuckDuckGo con filtrado de resultados basura que causan alucinaciones

Módulo puro: sin estado global, sin singletons, importable desde cualquier agente.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("agents.researcher.web")

# Palabras clave que activan el fast-path de tipo de cambio
_CURRENCY_KEYWORDS = {
    "dolar", "dollar", "usd", "euro", "eur", "tipo de cambio",
    "precio dolar", "cotizacion", "cotización", "divisas", "forex",
}

# Resultados de búsqueda que causan alucinaciones (Google help pages, etc.)
_JUNK_KEYWORDS = [
    "google search help", "how to search on google", "chrome search",
    "búsquedas efectivas", "cómo buscar", "soporte de google",
    "search results help", "google support",
    "información sobre cómo realizar búsquedas",
    "search effectively", "mejores prácticas de búsqueda",
    "ayuda de búsqueda", "google search tips", "chrome tips",
    "browser search help",
]

# Persistent httpx client — reutilizado en toda la vida del proceso
_http_client: Optional[httpx.Client] = None


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(timeout=15.0)
    return _http_client


def _search_exchange_rates() -> Optional[str]:
    """
    Consulta open.er-api.com para tipo de cambio actual USD → MXN/EUR/CAD/GBP.
    Retorna None si la API falla.
    """
    try:
        resp = _get_http_client().get(
            "https://open.er-api.com/v6/latest/USD", timeout=10.0
        )
        if resp.status_code == 200:
            data    = resp.json()
            rates   = data.get("rates", {})
            updated = data.get("time_last_update_utc", "")
            lines   = ["**Tipo de cambio actual (base USD):**"]
            if rates.get("MXN"):
                lines.append(f"• 1 USD = **{rates['MXN']:.4f} MXN** (Peso Mexicano)")
            if rates.get("EUR"):
                lines.append(f"• 1 USD = **{rates['EUR']:.4f} EUR** (Euro)")
            if rates.get("CAD"):
                lines.append(f"• 1 USD = **{rates['CAD']:.4f} CAD** (Dólar Canadiense)")
            if rates.get("GBP"):
                lines.append(f"• 1 USD = **{rates['GBP']:.4f} GBP** (Libra Esterlina)")
            lines.append(f"\nActualizado: {updated}\nFuente: open.er-api.com")
            return "\n".join(lines)
    except Exception as exc:
        logger.warning(f"[web] Exchange rate API falló: {exc}")
    return None


def _search_duckduckgo(query: str, max_results: int = 8) -> str:
    """
    Busca en DuckDuckGo, filtra resultados basura y retorna los top 5.
    Migrado desde backend-ia/main.py → _web_search() (sección DuckDuckGo).
    """
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(
                ddgs.text(
                    query,
                    region="mx-es",
                    safesearch="off",
                    timelimit="m",
                    max_results=max_results,
                )
            )
    except Exception as exc:
        logger.error(f"[web] DuckDuckGo error: {exc}")
        return f"Error en búsqueda web: {exc}"

    if not results:
        logger.warning(f"[web] Sin resultados para: {query!r}")
        return "No se encontraron resultados para la búsqueda."

    # Filtrar resultados basura
    filtered = []
    for r in results:
        text_to_check = " ".join([
            r.get("title", ""),
            r.get("body", ""),
            r.get("href", ""),
        ]).lower()
        if not any(junk in text_to_check for junk in _JUNK_KEYWORDS):
            filtered.append(r)
        else:
            logger.debug(f"[web] Resultado basura filtrado: {r.get('title')!r}")

    top = filtered[:5]
    if not top:
        logger.warning(f"[web] Todos los resultados filtrados como basura para: {query!r}")
        return "No se encontró información técnica específica en la búsqueda web."

    logger.info(f"[web] {len(top)} resultados válidos para: {query!r}")
    lines = [
        f"**{r.get('title', '')}**\n{r.get('body', '')}\nFuente: {r.get('href', '')}"
        for r in top
    ]
    return "\n\n---\n\n".join(lines)


def web_search(query: str) -> str:
    """
    Punto de entrada unificado para búsqueda web.

    Fast path: tipo de cambio → open.er-api.com
    General:   DuckDuckGo con filtrado anti-alucinaciones

    Migrado desde backend-ia/main.py → _web_search()
    """
    q_lower = query.lower()

    if any(kw in q_lower for kw in _CURRENCY_KEYWORDS):
        result = _search_exchange_rates()
        if result:
            return result
        # Si la API falló, continúa con DuckDuckGo como fallback

    return _search_duckduckgo(query)
