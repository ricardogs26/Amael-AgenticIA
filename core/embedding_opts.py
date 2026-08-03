"""
Opciones de runtime para las llamadas de embeddings a Ollama.

Un único lugar donde se decide si el modelo de embeddings usa GPU o CPU, para
que los cuatro servicios (backend, raphael, camael, trader) se comporten igual.
Vive en `core/` porque es lo único que copian todos los Dockerfiles.

Contexto: la RTX 5070 tiene 12 GB y qwen3:14b ocupa 9.6 GB, así que el modelo
generativo y el de embeddings no caben a la vez. Cada llamada de embeddings
expulsaba el 14b de VRAM y obligaba a recargarlo entero — medido el 03-ago-2026:
91 cargas de modelo en 6 h, 40 de ellas de nomic-embed-text (274 MB desalojando
9.6 GB). Con los embeddings en CPU la VRAM queda íntegra para el generativo.

EMBED_NUM_GPU=0 (default) → CPU. Poner -1 para devolverlos a GPU.
"""
from __future__ import annotations

import os


def embed_num_gpu() -> int:
    """Capas del modelo de embeddings a descargar a GPU (0 = CPU puro)."""
    try:
        return int(os.environ.get("EMBED_NUM_GPU", "0"))
    except ValueError:
        return 0


def embed_options() -> dict:
    """
    Bloque `options` para POST /api/embeddings de Ollama.

    Ollama acepta las mismas opciones de runtime que en /api/generate; num_gpu
    controla cuántas capas se descargan a la GPU.
    """
    return {"num_gpu": embed_num_gpu()}


def embed_payload(model: str, prompt: str) -> dict:
    """Cuerpo completo de la petición de embeddings, con las opciones aplicadas."""
    return {"model": model, "prompt": prompt, "options": embed_options()}
