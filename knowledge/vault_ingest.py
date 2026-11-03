#!/usr/bin/env python3
"""Indexa el vault de Obsidian en Qdrant (F2 del RAG gobernado).

El vault es la fuente de verdad y Qdrant solo un índice: todo se puede regenerar.
Reglas de gobierno, en este orden:

- Una nota sin `audiencia` válida es privada. `personal/`, `trader/` y `qwen/` son
  privadas aunque el frontmatter diga otra cosa.
- Si una nota cambia de audiencia se borra de la colección vieja ANTES de subirla a la
  nueva: un fallo a medias puede dejarla sin indexar, nunca expuesta de más.
- Un fragmento con un secreto no se sube. El informe dice dónde, jamás el valor.

Uso:
    python3 knowledge/vault_ingest.py --dry-run     # no toca Qdrant ni embeddings
    python3 knowledge/vault_ingest.py               # incremental e idempotente
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml

CONFIG = Path(__file__).with_name("exclusiones.yaml")
VAULT = Path(os.environ.get("VAULT_DIR", "~/obsidian-vault")).expanduser()
QDRANT_URL = os.environ.get("QDRANT_URL", "https://10.152.183.251:6333")
# CA interna de Qdrant (TLS desde nov-2026). En el cluster la monta el pod; en el host
# vive fuera de git en ~/.config/amael/qdrant-ca.crt.
QDRANT_CA_FILE = os.environ.get(
    "QDRANT_CA_FILE", str(Path.home() / ".config/amael/qdrant-ca.crt"))


def _contexto_tls():
    if not QDRANT_CA_FILE or not Path(QDRANT_CA_FILE).is_file():
        return None
    import ssl
    return ssl.create_default_context(cafile=QDRANT_CA_FILE)
OLLAMA_URL = (os.environ.get("OLLAMA_EMBED_URL") or os.environ.get("OLLAMA_URL")
              or "http://10.152.183.25:11434")
LLAVE_QDRANT = Path(os.environ.get("QDRANT_KEY_FILE", "~/.secrets/qdrant-api-key")).expanduser()
INFORME = "inbox/ingesta-vault.md"
DIMENSIONES = 768
CHARS_POR_TOKEN = 4          # aproximación suficiente para español técnico
LOTE_EMBEDDINGS = 16
VERSION_FRAGMENTADOR = 2     # subirla obliga a reindexar todo el vault
ESPACIO_IDS = uuid.UUID("6f2c1a54-7e0b-4a3e-9d55-0a4f8a1c2b77")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
ENCABEZADO = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass
class Fragmento:
    texto: str
    encabezado: str
    linea: int


@dataclass
class Nota:
    ruta: str
    audiencia: str
    coleccion: str
    titulo: str
    tags: list[str]
    hash_nota: str
    fragmentos: list[Fragmento]
    avisos: list[str] = field(default_factory=list)
    bloqueados: list[tuple[str, int]] = field(default_factory=list)
    reportados: list[tuple[str, int]] = field(default_factory=list)


def cargar_config(ruta: Path = CONFIG) -> dict:
    cfg = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    for grupo in ("bloqueantes", "solo_reportar"):
        for patron in cfg["secretos"][grupo]:
            patron["re"] = re.compile(patron["regex"])
    return cfg


def separar_frontmatter(contenido: str) -> tuple[dict, str, bool]:
    """Devuelve (frontmatter, cuerpo, valido). Un YAML roto cuenta como sin frontmatter."""
    m = FRONTMATTER.match(contenido)
    if not m:
        return {}, contenido, True
    try:
        datos = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, contenido[m.end():], False
    if not isinstance(datos, dict):
        return {}, contenido[m.end():], False
    return datos, contenido[m.end():], True


def debe_indexarse(ruta: PurePosixPath, cfg: dict) -> bool:
    if any(p in cfg["carpetas"]["no_indexar"] or p.startswith(".") for p in ruta.parts[:-1]):
        return False
    return not any(fnmatch.fnmatch(ruta.name, v) for v in cfg["secretos"]["archivos_vetados"])


def resolver_audiencia(ruta: PurePosixPath, frontmatter: dict, cfg: dict) -> tuple[str, str | None]:
    """Audiencia efectiva y, si se ignoró lo que decía la nota, el motivo."""
    defecto = cfg["audiencias"]["por_defecto"]
    declarada = frontmatter.get("audiencia")
    raiz = ruta.parts[0] if len(ruta.parts) > 1 else ""
    forzadas = [*cfg["carpetas"]["privado_forzado"], cfg["carpetas"]["qwen_code"]]
    if raiz in forzadas:
        if declarada not in (None, "privado"):
            return "privado", f"{raiz}/ siempre es privado; se ignoró `{declarada}`"
        return "privado", None
    if declarada is None:
        return defecto, None
    if declarada not in cfg["audiencias"]["validas"]:
        return defecto, f"audiencia `{declarada}` no válida; se trató como `{defecto}`"
    return declarada, None


def _bloques(cuerpo: str) -> list[tuple[str, str, int]]:
    """Parte en párrafos: (texto, ruta de encabezados, línea)."""
    bloques, actual, pila, inicio, en_codigo = [], [], [], 1, False

    def cerrar():
        if any(x.strip() for x in actual):
            bloques.append(("\n".join(actual).strip(), " > ".join(t for _, t in pila), inicio))
        actual.clear()

    for n, linea in enumerate(cuerpo.splitlines(), 1):
        if linea.lstrip().startswith("```"):
            en_codigo = not en_codigo
        m = None if en_codigo else ENCABEZADO.match(linea)
        if m or (not en_codigo and not linea.strip()):
            cerrar()
            inicio = n + 1
            if m:
                nivel = len(m.group(1))
                while pila and pila[-1][0] >= nivel:
                    pila.pop()
                pila.append((nivel, m.group(2)))
            continue
        if not actual:
            inicio = n
        actual.append(linea)
    cerrar()
    return bloques


def _cortar_largo(texto: str, maximo: int) -> list[str]:
    """Corta por líneas y, si una línea sola no cabe, en seco."""
    partes, actual = [], ""
    for linea in texto.splitlines():
        while len(linea) > maximo:
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:maximo])
            linea = linea[maximo:]
        if actual and len(actual) + 1 + len(linea) > maximo:
            partes.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        partes.append(actual)
    return partes


def fragmentar(cuerpo: str, tokens: int, solape: int) -> list[Fragmento]:
    maximo, cola = tokens * CHARS_POR_TOKEN, solape * CHARS_POR_TOKEN
    salida: list[Fragmento] = []
    actual, encabezado, seccion, linea = "", "", "", 1

    def emitir():
        nonlocal actual
        if actual.strip():
            salida.append(Fragmento(actual.strip(), encabezado, linea))
        # El solape arranca en un límite de palabra para no partir términos.
        resto = actual[-cola:] if cola else ""
        corte = resto.find(" ")
        actual = resto[corte + 1:] if corte >= 0 else resto

    for texto, ruta_enc, num in _bloques(cuerpo):
        for parte in _cortar_largo(texto, maximo):
            seccion_nueva = ruta_enc != seccion and actual.strip()
            if seccion_nueva and len(actual) >= maximo / 2:
                emitir()
                actual = ""     # el solape no cruza encabezados
                seccion_nueva = False
            # Una sección chica se junta con la anterior; su título viaja en el texto
            # para que el fragmento no pierda de qué trata.
            bloque = f"## {ruta_enc}\n{parte}" if seccion_nueva and ruta_enc else parte
            if len(actual) + len(bloque) + 2 > maximo and actual.strip():
                emitir()
            if not actual.strip():
                encabezado, linea = ruta_enc, num
            actual = f"{actual}\n\n{bloque}" if actual.strip() else bloque
            seccion = ruta_enc
    if actual.strip():
        salida.append(Fragmento(actual.strip(), encabezado, linea))
    return salida


def filtrar_secretos(fragmentos: list[Fragmento], cfg: dict):
    limpios, bloqueados, reportados = [], [], []
    for f in fragmentos:
        hallados = [p["nombre"] for p in cfg["secretos"]["bloqueantes"] if p["re"].search(f.texto)]
        reportados += [
            (p["nombre"], f.linea) for p in cfg["secretos"]["solo_reportar"] if p["re"].search(f.texto)
        ]
        if hallados:
            bloqueados += [(n, f.linea) for n in hallados]
        else:
            limpios.append(f)
    return limpios, bloqueados, reportados


def leer_nota(archivo: Path, ruta: PurePosixPath, cfg: dict) -> Nota:
    contenido = archivo.read_text(encoding="utf-8", errors="replace")
    frontmatter, cuerpo, valido = separar_frontmatter(contenido)
    audiencia, aviso = resolver_audiencia(ruta, frontmatter, cfg)
    avisos = [] if valido else ["frontmatter inválido; se trató como privada"]
    if aviso:
        avisos.append(aviso)
    if not valido:
        audiencia = "privado"
    h1 = re.search(r"^#\s+(.+?)\s*$", cuerpo, re.MULTILINE)
    tags = frontmatter.get("tags") or []
    fragmentos = fragmentar(cuerpo, cfg["fragmentos"]["tokens_objetivo"], cfg["fragmentos"]["solape_tokens"])
    limpios, bloqueados, reportados = filtrar_secretos(fragmentos, cfg)
    huella = json.dumps(
        [contenido, audiencia, cfg["fragmentos"]["tokens_objetivo"], cfg["fragmentos"]["solape_tokens"],
         cfg["fragmentos"]["embedder"], cfg["fragmentos"].get("prefijo_documento", ""),
         VERSION_FRAGMENTADOR],
        ensure_ascii=False,
    )
    return Nota(
        ruta=str(ruta),
        audiencia=audiencia,
        coleccion=cfg["audiencias"]["colecciones"][audiencia],
        titulo=h1.group(1) if h1 else ruta.stem,
        tags=[str(t) for t in tags] if isinstance(tags, list) else [str(tags)],
        hash_nota=hashlib.sha256(huella.encode()).hexdigest(),
        fragmentos=limpios,
        avisos=avisos,
        bloqueados=bloqueados,
        reportados=reportados,
    )


def recorrer_vault(vault: Path, cfg: dict) -> list[Nota]:
    notas = []
    for archivo in sorted(vault.rglob("*.md")):
        if archivo.is_symlink() or not archivo.is_file():
            continue
        ruta = PurePosixPath(archivo.relative_to(vault).as_posix())
        if debe_indexarse(ruta, cfg):
            notas.append(leer_nota(archivo, ruta, cfg))
    return notas


class Qdrant:
    def __init__(self, url: str, llave: str, espera: int = 120):
        self.url, self.llave, self.espera = url.rstrip("/"), llave, espera
        self.tls = _contexto_tls() if self.url.startswith("https") else None

    def _pedir(self, metodo: str, ruta: str, cuerpo: dict | None = None) -> dict:
        peticion = urllib.request.Request(
            self.url + ruta,
            method=metodo,
            data=None if cuerpo is None else json.dumps(cuerpo).encode(),
            headers={"api-key": self.llave, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(peticion, timeout=self.espera, context=self.tls) as r:
            return json.loads(r.read())

    def asegurar_coleccion(self, nombre: str) -> None:
        if self._pedir("GET", f"/collections/{nombre}/exists")["result"]["exists"]:
            return
        self._pedir("PUT", f"/collections/{nombre}",
                    {"vectors": {"size": DIMENSIONES, "distance": "Cosine"}})
        self._pedir("PUT", f"/collections/{nombre}/index?wait=true",
                    {"field_name": "ruta", "field_schema": "keyword"})

    def indice(self, nombre: str) -> dict[str, str]:
        """ruta -> hash_nota de lo que hay en la colección."""
        rutas, cursor = {}, None
        while True:
            cuerpo = {"limit": 1000, "with_vector": False,
                      "with_payload": ["ruta", "hash_nota"]}
            if cursor is not None:
                cuerpo["offset"] = cursor
            r = self._pedir("POST", f"/collections/{nombre}/points/scroll", cuerpo)["result"]
            for p in r["points"]:
                rutas[p["payload"]["ruta"]] = p["payload"]["hash_nota"]
            cursor = r.get("next_page_offset")
            if cursor is None:
                return rutas

    def borrar_nota(self, nombre: str, ruta: str) -> None:
        self._pedir("POST", f"/collections/{nombre}/points/delete?wait=true",
                    {"filter": {"must": [{"key": "ruta", "match": {"value": ruta}}]}})

    def subir(self, nombre: str, puntos: list[dict]) -> None:
        self._pedir("PUT", f"/collections/{nombre}/points?wait=true", {"points": puntos})


def embeber(textos: list[str], modelo: str, espera: int = 600) -> list[list[float]]:
    vectores = []
    for i in range(0, len(textos), LOTE_EMBEDDINGS):
        peticion = urllib.request.Request(
            f"{OLLAMA_URL}/api/embed",
            data=json.dumps({"model": modelo, "input": textos[i:i + LOTE_EMBEDDINGS]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(peticion, timeout=espera) as r:
            vectores += json.loads(r.read())["embeddings"]
    return vectores


def puntos_de(nota: Nota, cfg: dict) -> list[dict]:
    prefijo = cfg["fragmentos"].get("prefijo_documento", "")
    textos = [
        f"{prefijo}{nota.titulo}" + (f" > {f.encabezado}" if f.encabezado else "") + f"\n{f.texto}"
        for f in nota.fragmentos
    ]
    vectores = embeber(textos, cfg["fragmentos"]["embedder"])
    raiz = nota.ruta.split("/")[0] if "/" in nota.ruta else ""
    return [
        {
            "id": str(uuid.uuid5(ESPACIO_IDS, f"{nota.ruta}#{i}")),
            "vector": v,
            "payload": {
                "ruta": nota.ruta, "titulo": nota.titulo, "encabezado": f.encabezado,
                "linea": f.linea, "indice": i, "texto": f.texto, "tags": nota.tags,
                "proyecto": raiz, "audiencia": nota.audiencia, "hash_nota": nota.hash_nota,
            },
        }
        for i, (f, v) in enumerate(zip(nota.fragmentos, vectores, strict=True))
    ]


def sincronizar(notas: list[Nota], qdrant: Qdrant, cfg: dict, forzar: bool = False) -> dict:
    colecciones = list(cfg["audiencias"]["colecciones"].values())
    for c in colecciones:
        qdrant.asegurar_coleccion(c)
    indices = {c: qdrant.indice(c) for c in colecciones}
    en_qdrant = {r for i in indices.values() for r in i}
    # Notas sin fragmentos publicables tampoco deben quedar en Qdrant.
    vivas = {n.ruta: n for n in notas if n.fragmentos}
    if len(en_qdrant) > 10 and len(vivas) < len(en_qdrant) / 2 and not forzar:
        raise SystemExit(
            f"Abortado: {len(vivas)} notas en el vault contra {len(en_qdrant)} indexadas. "
            "Revisa el vault o usa --forzar."
        )

    conteo = {"sin_cambio": 0, "subidas": 0, "fragmentos": 0, "borradas": 0}
    # Primero lo que ya no debe estar: audiencia cambiada, nota borrada o vetada.
    for coleccion, indice in indices.items():
        for ruta in indice:
            nota = vivas.get(ruta)
            if nota is None or nota.coleccion != coleccion:
                qdrant.borrar_nota(coleccion, ruta)
                conteo["borradas"] += 1
    for nota in vivas.values():
        if indices[nota.coleccion].get(nota.ruta) == nota.hash_nota:
            conteo["sin_cambio"] += 1
            continue
        puntos = puntos_de(nota, cfg)     # si Ollama falla, la versión vieja sigue en pie
        qdrant.borrar_nota(nota.coleccion, nota.ruta)
        qdrant.subir(nota.coleccion, puntos)
        conteo["subidas"] += 1
        conteo["fragmentos"] += len(puntos)
    return conteo


def cobertura(pregunta: str, payload: dict) -> float:
    """Fracción de palabras de la pregunta que aparecen tal cual en el fragmento."""
    palabras = {p for p in re.findall(r"\w+", pregunta.lower()) if len(p) > 3}
    if not palabras:
        return 0.0
    texto = " ".join(str(payload.get(k, "")) for k in ("ruta", "titulo", "encabezado", "texto")).lower()
    return sum(p in texto for p in palabras) / len(palabras)


def buscar(pregunta: str, coleccion: str, qdrant: Qdrant, cfg: dict,
           umbral: float | None = None, limite: int = 5, espera: int = 600) -> list[dict]:
    """Fragmentos relevantes por coseno más coincidencia de palabras.

    El coseno solo no separa: con nomic en español, lo relevante da 0.62-0.79 y lo que no
    tiene relación 0.50-0.67. Por eso se piden candidatos de más y se reordenan.
    """
    frag = cfg["fragmentos"]
    umbral = frag["umbral_busqueda"] if umbral is None else umbral
    vector = embeber([frag.get("prefijo_consulta", "") + pregunta], frag["embedder"], espera)[0]
    r = qdrant._pedir("POST", f"/collections/{coleccion}/points/search",
                      {"vector": vector, "limit": limite * 4, "with_payload": True})["result"]
    puntuados = [
        {**p["payload"], "coseno": round(p["score"], 3),
         "puntaje": round(p["score"] + frag["peso_palabras"] * cobertura(pregunta, p["payload"]), 3)}
        for p in r
    ]
    puntuados.sort(key=lambda x: -x["puntaje"])
    return [x for x in puntuados if x["puntaje"] >= umbral][:limite]


def escribir_informe(vault: Path, notas: list[Nota]) -> Path:
    """Solo toca el archivo si cambió el fondo, para no ensuciar git cada hora."""
    def tabla(titulo: str, filas: list[str]) -> list[str]:
        return [f"## {titulo}", "", *(filas or ["Ninguno."]), ""]

    por_audiencia = {}
    for n in notas:
        por_audiencia[n.audiencia] = por_audiencia.get(n.audiencia, 0) + 1
    cuerpo = [
        "# Informe de ingesta del vault", "",
        f"Notas leídas: {len(notas)} · "
        + " · ".join(f"{a}: {c}" for a, c in sorted(por_audiencia.items())), "",
        *tabla("Fragmentos bloqueados por secreto (no se subieron)",
               [f"- `{n.ruta}` línea ~{ln}: {tipo}" for n in notas for tipo, ln in n.bloqueados]),
        *tabla("Avisos de audiencia",
               [f"- `{n.ruta}`: {a}" for n in notas for a in n.avisos]),
        *tabla("Menciones sensibles (sí se subieron)",
               [f"- `{n.ruta}` línea ~{ln}: {tipo}" for n in notas for tipo, ln in n.reportados]),
    ]
    texto = "\n".join(cuerpo)
    destino = vault / INFORME
    previo = destino.read_text(encoding="utf-8") if destino.exists() else ""
    if separar_frontmatter(previo)[1] != texto:
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
        destino.parent.mkdir(exist_ok=True)
        destino.write_text(f"---\ntags: [ingesta]\nfecha: {fecha}\n---\n{texto}", encoding="utf-8")
    return destino


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--dry-run", action="store_true", help="solo lee el vault e informa")
    args.add_argument("--forzar", action="store_true", help="salta el freno de borrado masivo")
    opciones = args.parse_args()

    cfg = cargar_config()
    notas = recorrer_vault(VAULT, cfg)
    if not notas:
        print(f"No hay notas en {VAULT}; no toco Qdrant.", file=sys.stderr)
        return 1
    destino = escribir_informe(VAULT, notas)
    bloqueados = sum(len(n.bloqueados) for n in notas)
    fragmentos = sum(len(n.fragmentos) for n in notas)
    print(f"notas={len(notas)} fragmentos={fragmentos} bloqueados={bloqueados} informe={destino}")
    if opciones.dry_run:
        return 0
    qdrant = Qdrant(QDRANT_URL, LLAVE_QDRANT.read_text().strip())
    try:
        print(sincronizar(notas, qdrant, cfg, opciones.forzar))
    except urllib.error.URLError as e:
        print(f"Error de red: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
