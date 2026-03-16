# LLM Abstraction Layer — Roadmap Phase 6

**Estado**: Placeholder — no implementado

## Propósito

Capa de abstracción que desacopla el resto de la plataforma del proveedor LLM concreto.
Permite cambiar de Ollama a OpenAI, Anthropic o Bedrock sin modificar los agentes.

## Estado actual

Los agentes usan `langchain_ollama` directamente:

```python
# Uso actual (acoplado)
from langchain_ollama import ChatOllama
_llm = ChatOllama(model=settings.llm_model, base_url=settings.ollama_base_url)

# Uso futuro (desacoplado)
from llm import get_llm
_llm = get_llm()   # resuelve el adaptador según LLM_PROVIDER env var
```

## Estructura planeada

```
llm/
├── base/          # BaseLLMAdapter — interfaz común
├── ollama/        # OllamaAdapter (implementación actual)
├── openai/        # OpenAIAdapter
└── anthropic/     # AnthropicAdapter
```

## Interfaz planeada

```python
class BaseLLMAdapter(ABC):
    @abstractmethod
    def chat(self, messages: List[Message], **kwargs) -> str: ...

    @abstractmethod
    def embed(self, text: str) -> List[float]: ...

    @abstractmethod
    def stream(self, messages: List[Message]) -> Iterator[str]: ...
```

## Selección por env var

```bash
LLM_PROVIDER=ollama      # default — OllamaAdapter
LLM_PROVIDER=openai      # OpenAIAdapter (requiere OPENAI_API_KEY)
LLM_PROVIDER=anthropic   # AnthropicAdapter (requiere ANTHROPIC_API_KEY)
```
