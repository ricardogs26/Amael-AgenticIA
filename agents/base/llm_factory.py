"""
LLMFactory — único punto de construcción de instancias LLM y Embeddings.
Incluye TokenTrackingCallback que registra tokens y costo estimado en Prometheus.

Providers chat soportados:
  ollama     → ChatOllama    (default — requiere OLLAMA_BASE_URL)
  openai     → ChatOpenAI    (requiere LLM_API_KEY; compatible con llm-adapter)
  groq       → ChatGroq      (requiere LLM_API_KEY)
  gemini     → ChatGoogleGenerativeAI (requiere LLM_API_KEY)
  anthropic  → ChatAnthropic (requiere LLM_API_KEY)

Providers embeddings soportados:
  ollama     → OllamaEmbeddings            (default — 768 dims con nomic-embed-text)
  openai     → OpenAIEmbeddings            (requiere LLM_API_KEY o EMBED_API_KEY)
  google     → GoogleGenerativeAIEmbeddings (requiere EMBED_API_KEY — 768 dims, sin re-indexar)

Variables de entorno:
  LLM_PROVIDER   — proveedor de chat       (default: "ollama")
  LLM_MODEL      — modelo de chat          (default: "qwen2.5:14b")
  LLM_API_KEY    — API key cloud           (requerido si provider != ollama)
  LLM_BASE_URL   — URL base opcional       (para OpenAI-compatible / llm-adapter)
  EMBED_PROVIDER — proveedor de embeddings (default: igual a LLM_PROVIDER)
  EMBED_MODEL    — modelo de embeddings    (default: LLM_EMBED_MODEL)
  EMBED_API_KEY  — API key embeddings      (default: LLM_API_KEY)

Uso:
    from agents.base.llm_factory import get_chat_llm, get_embeddings

    llm      = get_chat_llm()             # temperatura por defecto
    llm_zero = get_chat_llm(temperature=0) # determinístico (supervisor)
    emb      = get_embeddings()
"""
from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("agents.base.llm_factory")

# ── Contexto de agente activo ─────────────────────────────────────────────────
# Setear antes de llamar al LLM para etiquetar correctamente los tokens.
# Uso: llm_agent_context.set("planner") antes de llm.invoke(...)
llm_agent_context: ContextVar[str] = ContextVar("llm_agent_context", default="other")

# ── Precios por millón de tokens (input, output) ──────────────────────────────
_COST_PER_MILLION: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-sonnet-4-5":          (3.00,  15.00),
    "claude-haiku-4-5-20251001":  (0.80,   4.00),
    "claude-haiku-4-5":           (0.80,   4.00),
    "claude-opus-4-6":            (15.00, 75.00),
    # OpenAI
    "gpt-4o":                     (2.50,  10.00),
    "gpt-4o-mini":                (0.15,   0.60),
    "gpt-4.1":                    (2.00,   8.00),
    "gpt-4.1-mini":               (0.40,   1.60),
    # Google
    "gemini-2.0-flash":           (0.10,   0.40),
    "gemini-1.5-pro":             (3.50,  10.50),
    # Groq (inferencia gratuita / sin costo directo de tokens)
    "llama-3.3-70b-versatile":    (0.0,    0.0),
    # Ollama local — siempre 0
    "qwen2.5:14b":                (0.0,    0.0),
    "qwen2.5:7b":                 (0.0,    0.0),
    "llama3.2":                   (0.0,    0.0),
}

def _get_cost(model: str, input_tok: int, output_tok: int) -> float:
    """Costo estimado en USD para esta llamada."""
    # Normalizar: buscar primero coincidencia exacta, luego por prefijo
    costs = _COST_PER_MILLION.get(model)
    if costs is None:
        for key in _COST_PER_MILLION:
            if model.startswith(key) or key.startswith(model.split(":")[0]):
                costs = _COST_PER_MILLION[key]
                break
    if costs is None:
        return 0.0
    input_cost, output_cost = costs
    return (input_tok * input_cost + output_tok * output_cost) / 1_000_000


# ── Token Tracking Callback ───────────────────────────────────────────────────

from langchain_core.callbacks.base import BaseCallbackHandler


class TokenTrackingCallback(BaseCallbackHandler):
    """
    Callback LangChain que registra tokens y costo estimado en Prometheus.
    Compatible con ChatOllama, ChatAnthropic, ChatOpenAI, ChatGoogleGenerativeAI, ChatGroq.

    Registra:
      amael_llm_tokens_total{model, token_type, agent}
      amael_llm_cost_usd_total{model, agent}
      amael_llm_calls_total{model, agent}
    """

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            from observability.metrics import LLM_CALLS_TOTAL, LLM_COST_USD_TOTAL, LLM_TOKENS_TOTAL

            model   = self._extract_model(response, kwargs)
            agent   = llm_agent_context.get()
            in_tok, out_tok = self._extract_tokens(response)

            if in_tok > 0 or out_tok > 0:
                LLM_TOKENS_TOTAL.labels(model=model, token_type="input",  agent=agent).inc(in_tok)
                LLM_TOKENS_TOTAL.labels(model=model, token_type="output", agent=agent).inc(out_tok)
                cost = _get_cost(model, in_tok, out_tok)
                if cost > 0:
                    LLM_COST_USD_TOTAL.labels(model=model, agent=agent).inc(cost)

            LLM_CALLS_TOTAL.labels(model=model, agent=agent).inc()

        except Exception as exc:
            logger.debug(f"[token_tracking] Error registrando tokens: {exc}")

    def _extract_model(self, response: Any, kwargs: Any) -> str:
        # Intentar extraer el modelo del llm_output o invocation_params
        llm_out = getattr(response, "llm_output", {}) or {}
        model = (
            llm_out.get("model_name")
            or llm_out.get("model")
            or kwargs.get("invocation_params", {}).get("model")
            or kwargs.get("invocation_params", {}).get("model_name")
        )
        if not model:
            # Fallback: leer directamente de settings (siempre disponible)
            try:
                from config.settings import settings
                model = settings.llm_model
            except Exception:
                model = "unknown"
        return str(model)

    def _extract_tokens(self, response: Any) -> tuple[int, int]:
        """Extrae (input_tokens, output_tokens) de LLMResult — soporta múltiples formatos."""
        in_tok = out_tok = 0

        # Formato 1: llm_output["token_usage"] — OpenAI, Groq
        llm_out = getattr(response, "llm_output", {}) or {}
        usage = llm_out.get("token_usage") or llm_out.get("usage") or {}
        if usage:
            in_tok  = usage.get("input_tokens") or usage.get("prompt_tokens", 0)
            out_tok = usage.get("output_tokens") or usage.get("completion_tokens", 0)
            if in_tok or out_tok:
                return int(in_tok), int(out_tok)

        # Formato 2: generations[0][0].message.usage_metadata — LangChain 0.2+ (Ollama, Anthropic)
        try:
            gen = response.generations[0][0]
            msg = getattr(gen, "message", None)
            meta = getattr(msg, "usage_metadata", None) or {}
            if meta:
                in_tok  = meta.get("input_tokens", 0)
                out_tok = meta.get("output_tokens", 0)
                if in_tok or out_tok:
                    return int(in_tok), int(out_tok)
        except (IndexError, AttributeError):
            pass

        # Formato 3: generations[0][0].generation_info — Ollama directo
        try:
            gen = response.generations[0][0]
            info = getattr(gen, "generation_info", {}) or {}
            in_tok  = info.get("prompt_eval_count", 0)
            out_tok = info.get("eval_count", 0)
        except (IndexError, AttributeError):
            pass

        return int(in_tok), int(out_tok)


_token_callback = TokenTrackingCallback()

# Cache de instancias: (provider, model, temperature, timeout) → BaseChatModel
_chat_cache: dict[tuple, Any] = {}
_chat_lock = threading.Lock()

# Singleton de embeddings
_embed_instance: Any = None
_embed_lock = threading.Lock()


def get_chat_llm(temperature: float | None = None, timeout: int = 90) -> Any:
    """
    Retorna una instancia de BaseChatModel cacheada según la configuración activa.

    Args:
        temperature: Override de temperatura. None usa el default del provider (0.7).
        timeout:     Timeout en segundos para llamadas LLM (default 90s).

    Returns:
        Instancia de BaseChatModel compatible con LangChain.
    """
    from config.settings import settings

    provider = settings.llm_provider.lower()
    model = settings.llm_model
    temp = temperature if temperature is not None else 0.7

    key = (provider, model, temp, timeout)
    if key not in _chat_cache:
        with _chat_lock:
            if key not in _chat_cache:
                instance = _build_chat_llm(provider, model, temp, timeout, settings)
                _chat_cache[key] = instance
                logger.info(
                    f"[llm_factory] Chat LLM inicializado: "
                    f"provider={provider} model={model} temp={temp} timeout={timeout}s"
                )
    return _chat_cache[key]


def _build_chat_llm(
    provider: str,
    model: str,
    temperature: float,
    timeout: int,
    settings: Any,
) -> Any:
    callbacks = [_token_callback]

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = dict(
            model=model,
            temperature=temperature,
            timeout=timeout,
            api_key=settings.llm_api_key,
            callbacks=callbacks,
        )
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return ChatOpenAI(**kwargs)

    elif provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=model,
            temperature=temperature,
            groq_api_key=settings.llm_api_key,
            callbacks=callbacks,
        )

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            google_api_key=settings.llm_api_key,
            callbacks=callbacks,
        )

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model,
            temperature=temperature,
            timeout=timeout,
            api_key=settings.llm_api_key,
            callbacks=callbacks,
        )

    else:  # ollama (default)
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=model,
            base_url=settings.ollama_base_url,
            temperature=temperature,
            request_timeout=timeout,
            callbacks=callbacks,
        )


def get_embeddings() -> Any:
    """
    Retorna el singleton de Embeddings según la configuración activa.

    Returns:
        Instancia compatible con langchain_core.embeddings.Embeddings.
    """
    global _embed_instance
    if _embed_instance is None:
        with _embed_lock:
            if _embed_instance is None:
                from config.settings import settings

                _embed_instance = _build_embeddings(settings)
                logger.info(
                    f"[llm_factory] Embeddings inicializado: "
                    f"provider={settings.embed_provider} model={settings.llm_embed_model}"
                )
    return _embed_instance


def _build_embeddings(settings: Any) -> Any:
    provider = settings.embed_provider.lower()
    model = settings.llm_embed_model
    api_key = settings.embed_api_key or settings.llm_api_key

    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings

        kwargs: dict[str, Any] = dict(model=model, api_key=api_key)
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return OpenAIEmbeddings(**kwargs)

    elif provider == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        # text-embedding-004 produce 768 dims — compatible con colecciones Qdrant actuales
        return GoogleGenerativeAIEmbeddings(model=model, google_api_key=api_key)

    else:  # ollama (default)
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(model=model, base_url=settings.ollama_base_url)
