# BaseLLMAdapter — Roadmap Phase 6

**Estado**: Placeholder — no implementado

## Responsabilidad

Interfaz abstracta que todos los adaptadores LLM deben implementar.

## Implementación planeada

```python
from abc import ABC, abstractmethod
from typing import Iterator, List, Optional
from dataclasses import dataclass


@dataclass
class LLMMessage:
    role: str        # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str   # "stop" | "length" | "tool_calls"


class BaseLLMAdapter(ABC):
    """
    Contrato base para todos los adaptadores LLM de la plataforma.
    Implementar este contrato permite cambiar de proveedor LLM
    sin modificar ningún agente.
    """

    model: str
    provider: str

    @abstractmethod
    def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> LLMResponse: ...

    @abstractmethod
    def stream(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.7,
    ) -> Iterator[str]: ...

    @abstractmethod
    def embed(self, text: str) -> List[float]: ...

    @abstractmethod
    def embed_batch(self, texts: List[str]) -> List[List[float]]: ...

    @property
    @abstractmethod
    def context_window(self) -> int:
        """Tamaño máximo de contexto en tokens."""
        ...
```
