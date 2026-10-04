from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from context_builder import compact_ai_context
from database import connect, json_dumps


class AIProvider(ABC):
    name = "abstract"

    @abstractmethod
    def generate(self, task_type: str, context: dict[str, Any], instruction: str) -> str:
        raise NotImplementedError


class MockAIProvider(AIProvider):
    """Development provider. Proves the architecture without any token cost."""
    name = "mock"

    def generate(self, task_type: str, context: dict[str, Any], instruction: str) -> str:
        farm = context.get("farm", {})
        fields = context.get("fields", [])
        acres = sum((f.get("acres") or 0) for f in fields)
        return (
            f"{farm.get('name','This operation')} currently has {len(fields)} field(s) and "
            f"{acres:,.0f} acres in the selected structured context. "
            "This response is generated from normalized database values rather than rereading source documents. "
            f"Requested task: {task_type}."
        )


def run_ai_task(farm_id: int, task_type: str, instruction: str, field_id: int | None = None, provider: AIProvider | None = None) -> dict[str, Any]:
    provider = provider or MockAIProvider()
    context = compact_ai_context(farm_id, field_id)
    output = provider.generate(task_type, context, instruction)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_events(farm_id,field_id,task_type,provider,model,input_summary_json,output_text) VALUES(?,?,?,?,?,?,?)",
            (farm_id, field_id, task_type, provider.name, "development-mock", json_dumps(context), output),
        )
    return {"event_id": cur.lastrowid, "provider": provider.name, "context": context, "output": output}
