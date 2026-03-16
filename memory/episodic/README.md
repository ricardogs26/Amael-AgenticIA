# Episodic Memory — Roadmap Phase 8

**Estado**: Placeholder — no implementado

## Responsabilidad

Almacenamiento y recuperación de episodios de conversación a largo plazo.
Permite al sistema "recordar" interacciones pasadas relevantes al contexto actual.

## Implementación planeada

```python
@dataclass
class Episode:
    id: str
    user_id: str
    conversation_id: str
    summary: str              # LLM-generated summary del episodio
    key_facts: List[str]      # Hechos extraídos del episodio
    embedding: List[float]    # Vector del summary para búsqueda semántica
    timestamp: datetime
    importance: float         # 0-1, score de relevancia para retención


class EpisodicMemory:
    """
    Almacena episodios en Qdrant (colección memory_{user_id})
    y los recupera por similitud semántica con el query actual.
    """
    COLLECTION_PREFIX = "memory_"

    def store_episode(self, user_id: str, conversation_id: str,
                      messages: List[dict]) -> Episode:
        # 1. LLM genera summary del episodio
        # 2. Extrae key facts
        # 3. Genera embedding del summary
        # 4. Upsert en Qdrant
        ...

    def recall(self, user_id: str, query: str, k: int = 3) -> List[Episode]:
        # Búsqueda semántica en colección memory_{user_id}
        ...

    def forget(self, user_id: str, episode_id: str) -> bool:
        # GDPR compliance — eliminar episodio específico
        ...

    def forget_all(self, user_id: str) -> int:
        # GDPR — eliminar toda la memoria del usuario
        ...
```

## Colección Qdrant

- **Nombre**: `memory_{sanitize_email(user_id)}`
- **Dims**: 768 (nomic-embed-text, igual que RAG)
- **Distancia**: Cosine
