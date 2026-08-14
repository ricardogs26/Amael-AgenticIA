"""
Errores del dominio de productividad.

Existe uno solo, y por una razón concreta: distinguir «no hay nada» de «no pude
leer». El 14-ago-2026 el refresh token de Google del usuario quedó revocado; los
lectores capturaban la excepción, devolvían `[]`, y el brief de la mañana
concluía «✅ ¡Tu día está libre!» con el calendario lleno. Un sistema que no
puede leer y lo reporta como ausencia de datos es peor que uno que falla: el
usuario le cree.
"""
from __future__ import annotations


class GoogleAuthRevocada(Exception):
    """
    Las credenciales de Google del usuario ya no sirven — revocadas, caducadas,
    o con el refresh token invalidado. Solo se arregla reautorizando; ningún
    reintento la resuelve.
    """

    def __init__(self, user_email: str = "", detalle: str = ""):
        self.user_email = user_email
        self.detalle = detalle
        super().__init__(
            "Credenciales de Google revocadas o caducadas"
            + (f" para {user_email}" if user_email else "")
            + (f": {detalle}" if detalle else "")
        )
