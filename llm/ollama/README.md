# OllamaAdapter — Roadmap Phase 6

**Estado**: Placeholder — funcionalidad actual vía `langchain_ollama` directamente

## Responsabilidad

Implementación de `BaseLLMAdapter` para Ollama. Envuelve `langchain_ollama.ChatOllama`
y `langchain_ollama.OllamaEmbeddings` con el contrato estándar de la plataforma.

## Implementación planeada

```python
from langchain_ollama import ChatOllama, OllamaEmbeddings
from llm.base import BaseLLMAdapter, LLMMessage, LLMResponse


class OllamaAdapter(BaseLLMAdapter):
    provider = "ollama"

    def __init__(self, model: str, base_url: str, embed_model: str = "nomic-embed-text"):
        self.model = model
        self._chat = ChatOllama(model=model, base_url=base_url)
        self._embeddings = OllamaEmbeddings(model=embed_model, base_url=base_url)

    @property
    def context_window(self) -> int:
        return 32_768  # qwen2.5:14b

    def chat(self, messages, temperature=0.7, **kwargs) -> LLMResponse:
        lc_messages = [
            SystemMessage(m.content) if m.role == "system" else HumanMessage(m.content)
            for m in messages
        ]
        response = self._chat.invoke(lc_messages)
        return LLMResponse(
            content=response.content,
            model=self.model,
            prompt_tokens=0,   # Ollama no expone tokens en todas las versiones
            completion_tokens=0,
            finish_reason="stop",
        )

    def embed(self, text: str) -> List[float]:
        return self._embeddings.embed_query(text)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return self._embeddings.embed_documents(texts)
```

## Modelos soportados actualmente

| Modelo | Uso |
|--------|-----|
| `qwen2.5:14b` | Chat: planner, supervisor, reasoning |
| `qwen2.5-vl:7b` | Visión: análisis de screenshots Grafana |
| `nomic-embed-text` | Embeddings RAG (768 dims) |
