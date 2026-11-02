"""Copia las colecciones RAG sin prefijo a `rag_<slug>_<hash>`.

Sin --ejecutar solo muestra el plan. Con --ejecutar toma un snapshot de cada origen, copia con
los mismos ids y compara conteos. Repetirlo copia lo nuevo sin duplicar. Nunca borra el origen.

Uso, desde la raíz del repo:
    python3 -m knowledge.migrar_rag
    python3 -m knowledge.migrar_rag --ejecutar
"""
from __future__ import annotations

import argparse
import re
import sys

from agents.researcher.rag_retriever import rag_collection
from knowledge.vault_ingest import LLAVE_QDRANT, QDRANT_URL, Qdrant

CORREO = re.compile(r"^(?!memory_|rag_|kb_)[a-z0-9_-]+_at_[a-z0-9_-]+$")
TAM_PAGINA = 256


class Cliente:
    def __init__(self, qdrant: Qdrant):
        self.q = qdrant

    def nombres(self) -> list[str]:
        return [c["name"] for c in self.q._pedir("GET", "/collections")["result"]["collections"]]

    def existe(self, nombre: str) -> bool:
        return self.q._pedir("GET", f"/collections/{nombre}/exists")["result"]["exists"]

    def contar(self, nombre: str) -> int:
        return self.q._pedir("POST", f"/collections/{nombre}/points/count",
                             {"exact": True})["result"]["count"]

    def crear_como(self, origen: str, destino: str) -> None:
        vectores = self.q._pedir("GET", f"/collections/{origen}")["result"]["config"]["params"]["vectors"]
        self.q._pedir("PUT", f"/collections/{destino}", {"vectors": vectores})

    def paginas(self, nombre: str):
        cursor = None
        while True:
            cuerpo = {"limit": TAM_PAGINA, "with_payload": True, "with_vector": True}
            if cursor is not None:
                cuerpo["offset"] = cursor
            r = self.q._pedir("POST", f"/collections/{nombre}/points/scroll", cuerpo)["result"]
            yield [{"id": p["id"], "vector": p["vector"], "payload": p.get("payload") or {}}
                   for p in r["points"]]
            cursor = r.get("next_page_offset")
            if cursor is None:
                return

    def subir(self, nombre: str, puntos: list[dict]) -> None:
        if puntos:
            self.q._pedir("PUT", f"/collections/{nombre}/points?wait=true", {"points": puntos})

    def snapshot(self, nombre: str) -> str:
        return self.q._pedir("POST", f"/collections/{nombre}/snapshots?wait=true")["result"]["name"]


def pendientes(nombres: list[str]) -> dict[str, str]:
    """Colección antigua -> id del usuario."""
    return {n: n.replace("_at_", "@").replace("_dot_", ".") for n in nombres if CORREO.match(n)}


def copiar(cliente, origen: str, destino: str) -> tuple[int, int]:
    if not cliente.existe(destino):
        cliente.crear_como(origen, destino)
    for pagina in cliente.paginas(origen):
        cliente.subir(destino, pagina)
    return cliente.contar(origen), cliente.contar(destino)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ejecutar", action="store_true")
    args = parser.parse_args()
    cliente = Cliente(Qdrant(QDRANT_URL, LLAVE_QDRANT.read_text().strip()))
    bien = True
    for origen, usuario in sorted(pendientes(cliente.nombres()).items()):
        destino = rag_collection(usuario)
        if not args.ejecutar:
            print(f"{origen} ({cliente.contar(origen)}) -> {destino}")
            continue
        print(f"snapshot {origen}: {cliente.snapshot(origen)}")
        en_origen, en_destino = copiar(cliente, origen, destino)
        print(f"{origen} {en_origen} -> {destino} {en_destino}")
        bien &= en_destino >= en_origen
    return 0 if bien else 1


if __name__ == "__main__":
    sys.exit(main())
