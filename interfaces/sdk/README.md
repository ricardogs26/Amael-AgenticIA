# Python SDK — Roadmap Phase 6

**Estado**: Placeholder — no implementado

## Propósito

Cliente Python para consumir la API de Amael-AgenticIA desde otras aplicaciones,
scripts o servicios externos.

## Uso planeado

```python
from amael_sdk import AmaelClient

client = AmaelClient(
    base_url="https://amael-ia.richardx.dev",
    api_key="your-jwt-token",
)

# Chat
response = client.chat("¿cuántos pods están corriendo?")
print(response.answer)

# Streaming
for token in client.chat_stream("explica el estado del clúster"):
    print(token, end="", flush=True)

# Ingesta de documentos
result = client.ingest("./mi_documento.pdf")
print(f"Indexados {result.chunks} chunks")

# Listar documentos
docs = client.documents.list()

# SRE
incidents = client.sre.incidents(last=5)
status = client.sre.status()
```

## Implementación planeada

```python
# interfaces/sdk/client.py
class AmaelClient:
    def __init__(self, base_url: str, api_key: str):
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )
        self.sre = SREResource(self._http)
        self.documents = DocumentsResource(self._http)

    def chat(self, question: str, conversation_id: str = None) -> ChatResponse: ...
    def chat_stream(self, question: str) -> Iterator[str]: ...
    def ingest(self, file_path: str) -> IngestResponse: ...
```

## Instalación planeada

```bash
pip install amael-sdk
```
