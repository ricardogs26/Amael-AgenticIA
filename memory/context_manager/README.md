# ContextManager — Roadmap Phase 8

**Estado**: Placeholder — no implementado

## Responsabilidad

Gestión del contexto activo de una conversación: ventana de mensajes, compresión
de historial largo y recuperación de contexto relevante de memoria episódica.

## Implementación planeada

```python
class ContextManager:
    """
    Gestiona el contexto de conversación dentro de los límites del context window del LLM.
    """
    MAX_CONTEXT_TOKENS = 8_000

    def __init__(self, user_id: str, conversation_id: str):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self._messages: List[dict] = []

    def add_message(self, role: str, content: str) -> None: ...

    def get_context(self, max_tokens: int = None) -> List[dict]:
        """
        Retorna los mensajes más recientes que caben en max_tokens.
        Si el historial es muy largo, comprime mensajes anteriores con LLM.
        """
        ...

    def compress(self) -> str:
        """Resume mensajes antiguos para liberar espacio en la ventana."""
        ...

    def inject_memory(self, query: str) -> List[dict]:
        """
        Recupera episodios relevantes de memoria a largo plazo
        y los inyecta como contexto adicional.
        """
        ...
```

## Relación con AgentState actual

El `AgentState.context` (string, máx 12k chars) es la implementación actual
de working memory. `ContextManager` lo reemplazará con gestión más sofisticada.
