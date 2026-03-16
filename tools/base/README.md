# BaseTool — Nota de Arquitectura

**Estado**: El contrato real está en `core/tool_base.py`

## Por qué está vacío

El `BaseTool` y su contrato están definidos en:

```
core/tool_base.py   ← definición canónica
```

Este directorio `tools/base/` existe como placeholder para posibles
utilidades compartidas entre tools (helpers HTTP, parsers de respuesta, etc.)
que no sean el contrato base en sí.

## Referencia

```python
# core/tool_base.py
class BaseTool(ABC):
    name: str
    description: str

    @abstractmethod
    def execute(self, query: str, **kwargs) -> str:
        """
        Ejecuta la herramienta con el query dado.
        Retorna siempre un string (resultado legible por el LLM).
        """
        ...

    def __call__(self, query: str, **kwargs) -> str:
        return self.execute(query, **kwargs)
```

## Tools implementadas

| Tool | Archivo | Estado |
|------|---------|--------|
| `PrometheusTool` | `tools/prometheus/` | ✅ Producción |
| `GrafanaTool` | `tools/grafana/` | ✅ Producción |
| `WhatsAppTool` | `tools/whatsapp/` | ✅ Producción |
| `GitHubTool` | `tools/github/` | ✅ Producción |
