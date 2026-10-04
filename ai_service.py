from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from context_builder import compact_ai_context
from database import backend_name, connect, json_dumps


class AIProvider(ABC):
    name = "abstract"
    model = "unknown"

    @abstractmethod
    def generate(self, task_type: str, context: dict[str, Any], instruction: str) -> str:
        raise NotImplementedError


class MockAIProvider(AIProvider):
    """Development/fallback provider. Proves the architecture without token cost."""
    name = "mock"
    model = "development-mock"

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


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

    def generate(self, task_type: str, context: dict[str, Any], instruction: str) -> str:
        system = (
            "You are SeedIQ's agronomic explanation layer. Use only the structured farm context supplied. "
            "Do not invent agronomic facts, yields, soil values, seed ratings, or insurance values. "
            "Call out missing data explicitly. The deterministic SeedIQ engines remain the source of truth; "
            "your job is explanation, summarization, comparison, and sales-ready language."
        )
        response = self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"Task: {task_type}\nInstruction: {instruction}\nStructured context:\n{json_dumps(context)}",
                },
            ],
        )
        return response.output_text


def default_provider() -> AIProvider:
    if os.getenv("OPENAI_API_KEY"):
        return OpenAIProvider()
    return MockAIProvider()


def _insert_ai_event(
    farm_id: int,
    field_id: int | None,
    task_type: str,
    provider: AIProvider,
    context: dict[str, Any],
    output: str,
) -> int:
    sql = (
        "INSERT INTO ai_events(farm_id,field_id,task_type,provider,model,input_summary_json,output_text) "
        "VALUES(?,?,?,?,?,?,?)"
    )
    params = (farm_id, field_id, task_type, provider.name, provider.model, json_dumps(context), output)
    with connect() as conn:
        if backend_name() == "supabase-postgres":
            row = conn.execute(sql + " RETURNING id", params).fetchone()
            return int(row["id"])
        cur = conn.execute(sql, params)
        return int(cur.lastrowid)


def run_ai_task(
    farm_id: int,
    task_type: str,
    instruction: str,
    field_id: int | None = None,
    provider: AIProvider | None = None,
) -> dict[str, Any]:
    provider = provider or default_provider()
    context = compact_ai_context(farm_id, field_id)
    output = provider.generate(task_type, context, instruction)
    event_id = _insert_ai_event(farm_id, field_id, task_type, provider, context, output)
    return {
        "event_id": event_id,
        "provider": provider.name,
        "model": provider.model,
        "context": context,
        "output": output,
    }
