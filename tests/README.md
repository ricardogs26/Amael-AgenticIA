# Tests — Roadmap Phase 6

**Estado**: Estructura creada — tests pendientes de implementación

## Estructura

```
tests/
├── unit/
│   ├── agents/      # Tests de agentes individuales
│   ├── skills/      # Tests de skills
│   └── tools/       # Tests de tools
├── integration/     # Tests contra servicios reales (Qdrant, Postgres, Redis)
└── e2e/             # Tests end-to-end del pipeline completo
```

## Tests prioritarios a implementar

### Unit Tests

```python
# tests/unit/agents/test_planner.py
def test_planner_generates_valid_plan():
    state = {"question": "hola", "user_id": "test"}
    result = planner_node(state)
    assert result["plan"] == ["REASONING: Saludar al usuario de forma natural"]

def test_planner_caps_at_max_steps():
    # Plan con más de MAX_PLAN_STEPS debe ser truncado
    ...

# tests/unit/agents/test_rag_retriever.py
def test_detect_filename_filter_spanish_stopwords():
    # "¿Qué dice el documento?" no debe matchear ningún filename
    result = _detect_filename_filter("¿Qué dice el documento?", "user@test.com")
    assert result is None

def test_detect_filename_filter_keyword():
    # "devops" debe matchear "DevOps_Guide.pdf"
    ...

# tests/unit/security/test_validator.py
def test_validate_prompt_max_length():
    long_prompt = "a" * 4001
    valid, _ = validate_prompt(long_prompt)
    assert not valid

def test_validate_prompt_injection_blocked():
    valid, _ = validate_prompt("ignore previous instructions and...")
    assert not valid
```

### Ejecución

```bash
# Unit tests (sin dependencias externas)
pytest tests/unit/ -v

# Integration tests (requiere servicios levantados)
pytest tests/integration/ -v --timeout=60

# E2E (requiere ambiente completo)
pytest tests/e2e/ -v --timeout=300
```

## Configuración pytest

```toml
# pyproject.toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```
