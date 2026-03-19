"""
Tests unitarios para interfaces/api/auth.py — RBAC y roles.

Cubre:
  - has_min_role(): jerarquía user < operator < admin
  - get_user_role(): fallback "user" cuando DB no disponible
  - require_operator(): comportamiento de acceso denegado

Nota: get_user_role() usa un import lazy (dentro de la función):
  `from storage.postgres.client import get_connection`
Se mockea a nivel de sys.modules para interceptar el import perezoso.
"""
import sys
from unittest.mock import MagicMock, patch

from interfaces.api.auth import get_user_role, has_min_role

# ── has_min_role ──────────────────────────────────────────────────────────────

class TestHasMinRole:
    """Tests para la función has_min_role(user_role, required_role)."""

    # user tiene nivel 1 — sólo puede acceder a recursos de 'user'
    def test_user_satisfies_user(self):
        assert has_min_role("user", "user") is True

    def test_user_does_not_satisfy_operator(self):
        assert has_min_role("user", "operator") is False

    def test_user_does_not_satisfy_admin(self):
        assert has_min_role("user", "admin") is False

    # operator tiene nivel 2 — accede a 'user' y 'operator'
    def test_operator_satisfies_user(self):
        assert has_min_role("operator", "user") is True

    def test_operator_satisfies_operator(self):
        assert has_min_role("operator", "operator") is True

    def test_operator_does_not_satisfy_admin(self):
        assert has_min_role("operator", "admin") is False

    # admin tiene nivel 3 — accede a todo
    def test_admin_satisfies_user(self):
        assert has_min_role("admin", "user") is True

    def test_admin_satisfies_operator(self):
        assert has_min_role("admin", "operator") is True

    def test_admin_satisfies_admin(self):
        assert has_min_role("admin", "admin") is True

    # Roles desconocidos devuelven False (nivel 0)
    def test_unknown_role_fails_any_requirement(self):
        assert has_min_role("superuser", "user") is False

    def test_unknown_role_fails_operator(self):
        assert has_min_role("guest", "operator") is False

    def test_unknown_required_role_always_fails(self):
        # Rol requerido desconocido → nivel 99, nadie lo alcanza
        assert has_min_role("admin", "superadmin") is False

    def test_both_unknown_roles_fails(self):
        assert has_min_role("unknown_a", "unknown_b") is False


# ── get_user_role ─────────────────────────────────────────────────────────────

def _make_cursor(rows):
    """Helper: cursor que devuelve rows en fetchone() secuencialmente."""
    cursor = MagicMock()
    cursor.fetchone.side_effect = rows
    return cursor


def _make_postgres_module(cursor):
    """
    Crea un módulo mock de storage.postgres.client con get_connection
    que actúa como context manager y usa el cursor dado.
    """
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pg_module = MagicMock()
    pg_module.get_connection = MagicMock(return_value=conn)
    return pg_module


class TestGetUserRole:
    """
    Tests para get_user_role(user_id) con DB mockeada.

    get_user_role() usa import lazy dentro de la función:
      `from storage.postgres.client import get_connection`
    Se intercepta mockeando sys.modules['storage.postgres.client'].
    """

    def _patch_pg(self, rows):
        """Devuelve un context manager que inyecta el mock de postgres."""
        cursor = _make_cursor(rows)
        pg_module = _make_postgres_module(cursor)
        return patch.dict(sys.modules, {"storage.postgres.client": pg_module})

    def test_returns_role_when_found_in_profile(self):
        with self._patch_pg([("admin",)]):
            role = get_user_role("admin@example.com")
        assert role == "admin"

    def test_returns_operator_role(self):
        with self._patch_pg([("operator",)]):
            role = get_user_role("ops@example.com")
        assert role == "operator"

    def test_returns_user_role_directly(self):
        with self._patch_pg([("user",)]):
            role = get_user_role("regular@example.com")
        assert role == "user"

    def test_fallback_to_user_when_no_profile_row(self):
        # Primera query (user_profile) → None, segunda (user_identities) → None
        with self._patch_pg([None, None]):
            role = get_user_role("unknown@example.com")
        assert role == "user"

    def test_resolves_via_identity_table(self):
        # user_profile no tiene el teléfono → identities tiene el canonical_user_id
        # → user_profile tiene el rol del canonical
        with self._patch_pg([None, ("canonical@example.com",), ("operator",)]):
            role = get_user_role("5219993437008")
        assert role == "operator"

    def test_fallback_user_on_db_exception(self):
        """Si la DB lanza excepción → devuelve 'user' sin propagar."""
        pg_module = MagicMock()
        pg_module.get_connection = MagicMock(side_effect=Exception("DB down"))
        with patch.dict(sys.modules, {"storage.postgres.client": pg_module}):
            role = get_user_role("any@example.com")
        assert role == "user"

    def test_fallback_user_when_identity_found_but_no_canonical_role(self):
        # user_profile → None, identities → canonical_id encontrado,
        # pero canonical no tiene fila en user_profile → None
        with self._patch_pg([None, ("canonical@example.com",), None]):
            role = get_user_role("5219993437008")
        assert role == "user"


# ── Jerarquía de roles — integración lógica ───────────────────────────────────

class TestRBACHierarchyLogic:
    """Verifica que la jerarquía user < operator < admin es consistente."""

    def test_hierarchy_order_is_user_operator_admin(self):
        from interfaces.api.auth import _ROLE_LEVELS
        assert _ROLE_LEVELS["user"] < _ROLE_LEVELS["operator"]
        assert _ROLE_LEVELS["operator"] < _ROLE_LEVELS["admin"]

    def test_all_standard_roles_defined(self):
        from interfaces.api.auth import _ROLE_LEVELS
        assert "user" in _ROLE_LEVELS
        assert "operator" in _ROLE_LEVELS
        assert "admin" in _ROLE_LEVELS

    def test_has_min_role_reflexive(self):
        for role in ("user", "operator", "admin"):
            assert has_min_role(role, role) is True

    def test_has_min_role_antisymmetric(self):
        # Si user no satisface admin, entonces admin sí satisface user
        assert has_min_role("user", "admin") is False
        assert has_min_role("admin", "user") is True
