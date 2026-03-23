from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator

from .json_extract import extract_json_object


class LoopTurnOutput(BaseModel):
    action: Literal["sql", "clarify", "answer"]
    sql: str | None = None
    clarification: str | None = None
    answer: str | None = None
    rationale: str | None = None

    @model_validator(mode="after")
    def clear_non_sql_fields_on_sql_action(self) -> LoopTurnOutput:
        if self.action == "sql":
            return self.model_copy(update={"answer": None, "clarification": None})
        return self


def parse_loop_turn(text: str) -> LoopTurnOutput:
    data = extract_json_object(text)
    return LoopTurnOutput.model_validate(data)


def parse_planner_output(text: str) -> LoopTurnOutput:
    return parse_loop_turn(text)
