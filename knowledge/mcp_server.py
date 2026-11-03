#!/usr/bin/env python3
"""MCP `amael-knowledge`: consulta el vault indexado y guarda notas de trabajo.

Corre por stdio, lanzado por Qwen Code. Usa un JWT de solo lectura: la única forma de
escribir es una nota en el vault, que luego indexa el hook post-commit. Nunca hace push.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from knowledge.vault_ingest import (
    QDRANT_URL,
    VAULT,
    Fragmento,
    Qdrant,
    buscar,
    cargar_config,
    filtrar_secretos,
)

TOKEN = Path(os.environ.get("QDRANT_TOKEN_FILE", "~/.secrets/qdrant-qwen-jwt")).expanduser()
MAX_NOTA = 8_000
MAX_RESPUESTA = 6_000      # ~1.5k tokens: el contexto del modelo local es lo que más escasea
LIMITE_POR_COLECCION = 4

cfg = cargar_config()
mcp = MCPServer("amael-knowledge")


def _slug(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")[:60]


def colecciones_de(ambito: str) -> list[str]:
    todas = cfg["audiencias"]["colecciones"]
    if ambito == "todo":
        return list(todas.values())
    if ambito not in todas:
        raise ValueError(f"ambito debe ser todo, {', '.join(todas)}")
    return [todas[ambito]]


def formatear(resultados: list[dict]) -> str:
    salida, usado = [], 0
    for r in sorted(resultados, key=lambda x: -x["puntaje"]):
        bloque = f"[{r['puntaje']}] {r['ruta']}:{r.get('linea', 1)}\n{r['texto']}"
        if salida and usado + len(bloque) > MAX_RESPUESTA:
            break
        salida.append(bloque[:MAX_RESPUESTA])
        usado += len(bloque)
    return "\n\n---\n\n".join(salida)


def ruta_nota(proyecto: str, tema: str, vault: Path = VAULT, hoy: date | None = None) -> Path:
    p, t = _slug(proyecto), _slug(tema)
    if not p or not t:
        raise ValueError("proyecto y tema deben tener letras o números")
    carpeta = vault / cfg["carpetas"]["qwen_code"] / p
    base = f"{(hoy or date.today()).isoformat()}-{t}"
    destino, n = carpeta / f"{base}.md", 2
    while destino.exists():
        destino, n = carpeta / f"{base}-{n}.md", n + 1
    return destino


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(VAULT), *args], capture_output=True, text=True,
                          timeout=60)


@mcp.tool()
def buscar_conocimiento(pregunta: str, ambito: str = "todo") -> str:
    """Busca en las notas del vault de Ricardo: decisiones, análisis y runbooks. NO contiene
    código. Devuelve ruta:línea y fragmento; para más detalle lee la nota con read_file.
    pregunta: la duda en una frase completa con sus términos clave, no una palabra suelta
    (bien: «cómo está construido el RAG gobernado y qué decidimos»; mal: «RAG»).
    ambito: todo, privado, sre o publico."""
    qdrant = Qdrant(QDRANT_URL, TOKEN.read_text().strip())
    resultados = [
        r for c in colecciones_de(ambito)
        for r in buscar(pregunta, c, qdrant, cfg, limite=LIMITE_POR_COLECCION)
    ]
    return formatear(resultados) or "Sin resultados relevantes en el vault."


@mcp.tool()
def guardar_nota_trabajo(texto: str, proyecto: str, tema: str, tags: list[str] | None = None) -> str:
    """Guarda un resumen de la sesión como nota privada en qwen/<proyecto>/ del vault.
    Hace commit local; Ricardo decide el push. Úsala al terminar una tarea."""
    if len(texto) > MAX_NOTA:
        return f"Rechazada: la nota supera {MAX_NOTA} caracteres; resúmela."
    if filtrar_secretos([Fragmento(texto, "", 1)], cfg)[1]:
        return "Rechazada: el texto parece contener un secreto. Quítalo y vuelve a intentar."
    etiquetas = ", ".join(_slug(t) for t in (tags or []) if _slug(t))
    with open(VAULT / ".git" / "amael-knowledge.lock", "w") as candado:
        fcntl.flock(candado, fcntl.LOCK_EX)
        pull = _git("pull", "--ff-only", "-q")
        destino = ruta_nota(proyecto, tema)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(
            f"---\ntags: [qwen, {etiquetas}]\nfecha: {date.today().isoformat()}\n"
            f"audiencia: privado\n---\n\n# {tema}\n\n{texto}\n",
            encoding="utf-8",
        )
        relativa = str(destino.relative_to(VAULT))
        _git("add", "--", relativa)
        commit = _git("commit", "-q", "-m", f"qwen: {tema}", "--", relativa)
    if commit.returncode:
        return f"Nota escrita en {relativa}, pero el commit falló: {commit.stderr[-200:]}"
    aviso = "" if pull.returncode == 0 else " (git pull falló: revisa el vault)"
    return f"Guardada en {relativa}, con commit local.{aviso}"


if __name__ == "__main__":
    mcp.run()
