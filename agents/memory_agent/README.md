# MemoryAgent — Roadmap Phase 8

**Estado**: Placeholder — no implementado

## Responsabilidad

Agente especializado en gestión de memoria a largo plazo: almacena, indexa y recupera conocimiento de conversaciones pasadas, preferencias del usuario y contexto episódico.

## Diferencia con RAG del ResearchAgent

| | MemoryAgent | ResearchAgent RAG |
|-|-------------|-------------------|
| Fuente | Conversaciones históricas | Documentos subidos por el usuario |
| Granularidad | Episodios, preferencias, hechos | Chunks de documentos |
| Actualización | Continua (cada conversación) | Manual (POST /api/ingest) |
| Colección Qdrant | `memory_{user_id}` | `{email_sanitized}` |

## Capacidades planeadas

- Indexar automáticamente conversaciones relevantes al finalizar cada sesión
- Recuperar contexto de conversaciones pasadas relevantes al query actual
- Almacenar preferencias explícitas del usuario ("prefiero respuestas cortas")
- Detectar y consolidar hechos repetidos (deduplicación de memoria)
- Olvido selectivo (GDPR — eliminar memorias por solicitud)

## Implementación de referencia

```python
@AgentRegistry.register
class MemoryAgent(BaseAgent):
    name = "memory_agent"
    role = "Gestión de memoria episódica y contexto a largo plazo"
    version = "1.0.0"
    capabilities = ["memory_store", "memory_retrieve", "memory_consolidate"]
    required_skills = ["rag", "llm"]

    async def execute(self, task: Dict[str, Any]) -> AgentResult:
        action = task.get("action")  # "store" | "retrieve" | "forget"
        ...
```

## Colección Qdrant

```python
# Nombre de colección: memory_{sanitize_email(user_id)}
# Payload structure:
{
    "episode_type": "conversation" | "preference" | "fact",
    "content": "resumen del episodio",
    "participants": ["user", "assistant"],
    "timestamp": "2026-03-15T10:00:00Z",
    "importance": 0.85   # score de relevancia para retención
}
```

## Fase de implementación

**Phase 8** — requiere `memory/` módulo implementado.
