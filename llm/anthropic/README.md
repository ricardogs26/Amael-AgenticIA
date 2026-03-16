# AnthropicAdapter — Roadmap Phase 6

**Estado**: Placeholder — no implementado

## Responsabilidad

Implementación de `BaseLLMAdapter` para la API de Anthropic (Claude 4.x).

## Activación

```bash
# Variables de entorno requeridas
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6   # default
```

## Modelos objetivo

| Modelo ID | Alias | Context | Uso recomendado |
|-----------|-------|---------|-----------------|
| `claude-opus-4-6` | Opus 4.6 | 200k | Tareas complejas de razonamiento |
| `claude-sonnet-4-6` | Sonnet 4.6 | 200k | Balance calidad/velocidad — default |
| `claude-haiku-4-5-20251001` | Haiku 4.5 | 200k | Tareas simples, alta velocidad |

## Nota sobre embeddings

Anthropic no ofrece modelo de embeddings propio. Si se usa AnthropicAdapter como LLM,
los embeddings deben seguir generándose con Ollama (`nomic-embed-text`) o
cambiarse a un proveedor de embeddings externo (OpenAI, Cohere).

Configuración mixta:
```bash
LLM_PROVIDER=anthropic    # Claude para chat
EMBED_PROVIDER=ollama     # Ollama para embeddings (mantiene Qdrant sin cambios)
```
