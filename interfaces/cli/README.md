# CLI Interface — Roadmap Phase 6

**Estado**: Placeholder — no implementado

## Propósito

Interfaz de línea de comandos para interactuar con Amael-AgenticIA directamente
desde la terminal, sin necesidad del frontend web o WhatsApp.

## Uso planeado

```bash
# Chat interactivo
amael chat

# Pregunta única
amael ask "¿cuántos pods están corriendo en amael-ia?"

# Subir documento
amael ingest documento.pdf

# Estado del SRE
amael sre status
amael sre incidents --last 10

# Gestión de conversaciones
amael conversations list
amael conversations show <id>
amael conversations delete <id>
```

## Implementación planeada

```python
# interfaces/cli/main.py
import click
import httpx

BASE_URL = os.getenv("AMAEL_API_URL", "https://amael-ia.richardx.dev")

@click.group()
def cli(): ...

@cli.command()
@click.argument("question")
def ask(question: str):
    """Envía una pregunta a Amael y muestra la respuesta."""
    response = httpx.post(
        f"{BASE_URL}/api/chat",
        json={"question": question},
        headers={"Authorization": f"Bearer {get_token()}"},
    )
    click.echo(response.json()["answer"])
```

## Instalación planeada

```bash
pip install amael-cli
# o desde el repo:
pip install -e ".[cli]"
```
