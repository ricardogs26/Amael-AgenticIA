# VectorStore — Roadmap Phase 8

**Estado**: Placeholder — no implementado

## Responsabilidad

Abstracción sobre Qdrant que provee una interfaz limpia para búsqueda semántica
independiente del cliente concreto de vector DB.

## Implementación planeada

```python
class VectorStore(ABC):
    @abstractmethod
    def upsert(self, collection: str, points: List[VectorPoint]) -> int: ...

    @abstractmethod
    def search(self, collection: str, query_vector: List[float],
               limit: int = 5, filter: dict = None) -> List[SearchResult]: ...

    @abstractmethod
    def scroll(self, collection: str, limit: int = 100,
               filter: dict = None) -> List[VectorPoint]: ...

    @abstractmethod
    def delete(self, collection: str, ids: List[str]) -> bool: ...

    @abstractmethod
    def collection_exists(self, collection: str) -> bool: ...

    @abstractmethod
    def create_collection(self, collection: str, vector_size: int,
                          distance: str = "Cosine") -> bool: ...


class QdrantVectorStore(VectorStore):
    """Implementación concreta sobre qdrant-client."""
    ...
```

## Migración

Cuando se implemente, refactorizar `agents/researcher/rag_retriever.py`
para usar `VectorStore` en lugar de `qdrant_client` directamente.
