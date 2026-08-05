"""
LLMFactory — único punto de construcción de instancias LLM y Embeddings.

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
from typing import Any

logger = logging.getLogger("agents.base.llm_factory")

# Cache de instancias: (provider, model, temperature, timeout) → BaseChatModel
_chat_cache: dict[tuple, Any] = {}
_chat_lock = threading.Lock()

# Singleton de embeddings
_embed_instance: Any = None
_embed_lock = threading.Lock()


# Timeout mínimo del tier profundo. El modelo corre en CPU (~16.7 tok/s) y su
# carga en frío desde disco toma ~28.6 s, así que los 90 s de la ruta normal se
# quedan cortos para cualquier generación mediana.
_DEEP_TIMEOUT_S = 300


def get_chat_llm(
    temperature: float | None = None,
    timeout: int = 90,
    tier: str = "default",
) -> Any:
    """
    Retorna una instancia de BaseChatModel cacheada según la configuración activa.

    Args:
        temperature: Override de temperatura. None usa el default del provider (0.7).
        timeout:     Timeout en segundos para llamadas LLM (default 90s).
        tier:        "default" → instancia GPU (ruta interactiva).
                     "deep"    → instancia ollama-cpu, para trabajo asíncrono que
                                 no debe desalojar el modelo interactivo de la
                                 VRAM. Si OLLAMA_DEEP_URL / LLM_DEEP_MODEL no
                                 están configuradas, cae de vuelta a "default".

    Returns:
        Instancia de BaseChatModel compatible con LangChain.
    """
    from config.settings import settings

    provider = settings.llm_provider.lower()
    model = settings.llm_model
    base_url = settings.ollama_base_url

    if tier == "deep":
        if settings.ollama_deep_url and settings.llm_model_deep:
            # El tier profundo es siempre Ollama local, con independencia del
            # provider configurado para la ruta normal.
            provider = "ollama"
            model = settings.llm_model_deep
            base_url = settings.ollama_deep_url
            timeout = max(timeout, _DEEP_TIMEOUT_S)
        else:
            logger.debug(
                "[llm_factory] tier='deep' pedido pero OLLAMA_DEEP_URL/"
                "LLM_DEEP_MODEL no configuradas — usando la instancia normal."
            )

    temp = temperature if temperature is not None else 0.7

    # base_url va en la clave: dos tiers pueden compartir nombre de modelo.
    key = (provider, model, base_url, temp, timeout)
    if key not in _chat_cache:
        with _chat_lock:
            if key not in _chat_cache:
                instance = _build_chat_llm(
                    provider, model, temp, timeout, settings, base_url
                )
                _chat_cache[key] = instance
                logger.info(
                    f"[llm_factory] Chat LLM inicializado: provider={provider} "
                    f"model={model} base_url={base_url} temp={temp} "
                    f"timeout={timeout}s tier={tier}"
                )
    return _chat_cache[key]


def _build_chat_llm(
    provider: str,
    model: str,
    temperature: float,
    timeout: int,
    settings: Any,
    base_url: str | None = None,
) -> Any:
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = dict(
            model=model,
            temperature=temperature,
            timeout=timeout,
            api_key=settings.llm_api_key,
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
        )

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
            google_api_key=settings.llm_api_key,
        )

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model,
            temperature=temperature,
            timeout=timeout,
            api_key=settings.llm_api_key,
        )

    else:  # ollama (default)
        from langchain_ollama import ChatOllama

        # El timeout va en client_kwargs: ChatOllama no tiene request_timeout y
        # langchain_ollama descarta los kwargs desconocidos sin avisar, así que
        # hasta ahora estas llamadas no tenían límite de tiempo.
        return ChatOllama(
            model=model,
            base_url=base_url or settings.ollama_base_url,
            temperature=temperature,
            client_kwargs={"timeout": timeout},
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

        # num_gpu=0 → el modelo de embeddings corre en CPU y deja la VRAM
        # íntegra para el modelo generativo. Ver settings.embed_num_gpu.
        return OllamaEmbeddings(
            model=model,
            base_url=settings.ollama_base_url,
            num_gpu=settings.embed_num_gpu,
        )
