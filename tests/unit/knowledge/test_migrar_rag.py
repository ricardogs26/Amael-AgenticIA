"""La migración copia, compara y nunca borra."""
from knowledge import migrar_rag as m


class Falso:
    def __init__(self, colecciones):
        self.c = colecciones
        self.borrados = []

    def nombres(self):
        return list(self.c)

    def contar(self, n):
        return len(self.c.get(n, []))

    def existe(self, n):
        return n in self.c

    def crear_como(self, origen, destino):
        self.c.setdefault(destino, [])

    def paginas(self, n):
        yield self.c[n]

    def subir(self, n, puntos):
        ids = {p["id"] for p in self.c[n]}
        self.c[n] += [p for p in puntos if p["id"] not in ids]


def test_elige_solo_colecciones_de_usuario():
    nombres = ["a_at_x_dot_com", "memory_a_at_x_dot_com", "kb_publico", "sre_runbooks",
               "rag_a_x_com_abc", "agent_skills"]
    assert m.pendientes(nombres) == {"a_at_x_dot_com": "a@x.com"}


def test_copia_y_repetir_no_duplica():
    q = Falso({"a_at_x_dot_com": [{"id": 1}, {"id": 2}]})
    destino = m.rag_collection("a@x.com")
    assert m.copiar(q, "a_at_x_dot_com", destino) == (2, 2)
    q.c["a_at_x_dot_com"].append({"id": 3})
    assert m.copiar(q, "a_at_x_dot_com", destino) == (3, 3)
    assert "a_at_x_dot_com" in q.c


def test_el_modulo_no_sabe_borrar():
    assert not hasattr(Falso, "borrar")
    assert "DELETE" not in open(m.__file__).read()
