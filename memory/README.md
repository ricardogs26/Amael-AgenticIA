# Memory Layer — Roadmap Phase 8

**Estado**: Placeholder — no implementado

## Propósito

Capa de abstracción para gestión de memoria a largo plazo de los agentes.
Actualmente el RAG per-usuario existe en `agents/researcher/rag_retriever.py` de forma directa.
Este módulo proveerá una interfaz unificada y extensible.

## Componentes

| Módulo | Propósito |
|--------|-----------|
| `vector_store/` | Abstracción sobre Qdrant para búsqueda semántica |
| `context_manager/` | Gestión del contexto activo de una conversación |
| `embeddings/` | Caché y abstracción de generación de embeddings |
| `episodic/` | Almacenamiento de episodios de conversación a largo plazo |

## Estado actual del RAG

El RAG funcional está en `agents/researcher/rag_retriever.py`:
- Colecciones per-usuario en Qdrant
- Embeddings con nomic-embed-text (768 dims)
- Filtrado por filename + cosine reranking

Este módulo `memory/` eventualmente envolverá esa lógica con una interfaz más limpia
y la extenderá con memoria episódica.

## Diagrama de memoria planeado

```
Memory Layer
├── Working Memory    → AgentState (TypedDict — actual)
├── Episodic Memory   → memory/episodic/ (conversaciones pasadas)
├── Semantic Memory   → memory/vector_store/ (documentos del usuario — actual en RAG)
└── Procedural Memory → skills/ (capacidades reutilizables — actual)
```
