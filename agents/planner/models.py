"""Modelos Pydantic del PlannerAgent."""
from typing import Literal

from pydantic import BaseModel, field_validator

StepType = Literal[
    "K8S_TOOL", "RAG_RETRIEVAL", "PRODUCTIVITY_TOOL",
    "REASONING", "WEB_SEARCH", "DOCUMENT_TOOL",
]


class PlanStep(BaseModel):
    """Un paso validado del plan de ejecución."""
    step_type: StepType
    description: str

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description no puede estar vacía")
        return v

    def to_string(self) -> str:
        return f"{self.step_type}: {self.description}"

    @classmethod
    def from_string(cls, raw: str) -> "PlanStep":
        """Parsea 'STEP_TYPE: descripción' en un PlanStep."""
        parts = raw.split(":", 1)
        return cls(
            step_type=parts[0].strip().upper(),
            description=parts[1].strip() if len(parts) > 1 else raw,
        )
