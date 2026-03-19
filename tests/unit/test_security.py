"""Tests unitarios para security/validator.py y security/sanitizer.py."""
from security.sanitizer import sanitize_output
from security.validator import validate_prompt

# ── validator ─────────────────────────────────────────────────────────────────

class TestValidatePrompt:
    def test_valid_question(self):
        ok, result = validate_prompt("¿Cuál es el estado del clúster?")
        assert ok is True
        assert "clúster" in result

    def test_prompt_too_long_rejected(self):
        ok, _ = validate_prompt("a" * 5000)
        assert ok is False

    def test_control_chars_stripped(self):
        ok, result = validate_prompt("pregunta\x00válida")
        assert ok is True
        assert "\x00" not in result

    def test_normal_text_accepted(self):
        ok, _ = validate_prompt("despliega el backend v1.8.1")
        assert ok is True

    def test_returns_tuple(self):
        result = validate_prompt("test")
        assert isinstance(result, tuple)
        assert len(result) == 2


# ── sanitizer ─────────────────────────────────────────────────────────────────

class TestSanitizeOutput:
    def test_normal_text_passes_through(self):
        text = "El clúster tiene 3 pods corriendo."
        assert sanitize_output(text) == text

    def test_vault_token_redacted(self):
        text = "Token: hvs.abcdefghijklmnopqrstuvwxyz123456"
        result = sanitize_output(text)
        assert "hvs." not in result
        assert "REDACTED" in result.upper() or "[" in result

    def test_jwt_token_redacted(self):
        fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature123"
        result = sanitize_output(fake_jwt)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result

    def test_empty_string(self):
        assert sanitize_output("") == ""

    def test_plain_text_unchanged(self):
        text = "Estado del cluster: todos los pods healthy"
        assert sanitize_output(text) == text
