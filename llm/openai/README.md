# OpenAIAdapter — Roadmap Phase 6

**Estado**: Placeholder — no implementado

## Responsabilidad

Implementación de `BaseLLMAdapter` para la API de OpenAI (GPT-4o, GPT-4-turbo, text-embedding-3-*).

## Activación

```bash
# Variables de entorno requeridas
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o              # default
OPENAI_EMBED_MODEL=text-embedding-3-small
```

## Modelos objetivo

| Modelo | Context | Uso |
|--------|---------|-----|
| `gpt-4o` | 128k | Chat principal |
| `gpt-4o-mini` | 128k | Tareas simples (menor costo) |
| `text-embedding-3-small` | — | Embeddings RAG (1536 dims) |
| `text-embedding-3-large` | — | Embeddings alta precisión (3072 dims) |

## Consideraciones

- Cambiar a OpenAI implica cambiar dimensión de embeddings (1536 vs 768 de nomic-embed-text)
- Las colecciones Qdrant deberán recrearse con el nuevo tamaño vectorial
- Costo por token: considerar rate limiting más agresivo
