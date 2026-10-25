"""The declarative step language executed by the worker.

Steps are data, never code: there is no step that evaluates JavaScript or runs a shell
command, so neither users nor Claude can make the worker execute arbitrary scripts.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

SECRET_RE = re.compile(r"\{\{\s*secret:([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
_SELECTOR_BLOCK = re.compile(r"^\s*(js|javascript|eval)\s*=", re.I)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=500)
    timeout_ms: int | None = Field(default=None, ge=100, le=120_000)
    retries: int | None = Field(default=None, ge=0, le=5)


class _Sel(_Base):
    selector: str = Field(min_length=1, max_length=1000)

    @field_validator("selector")
    @classmethod
    def _no_js(cls, v: str) -> str:
        if _SELECTOR_BLOCK.match(v):
            raise ValueError("script selectors are not allowed")
        return v


class Goto(_Base):
    action: Literal["goto"]
    url: str = Field(min_length=1, max_length=2048, description="Absolute URL or path relative to the project base URL")


class Click(_Sel):
    action: Literal["click"]


class Fill(_Sel):
    action: Literal["fill"]
    value: str = Field(max_length=5000, description="May reference project secrets as {{secret:NAME}}")


class Press(_Sel):
    action: Literal["press"]
    key: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9+_ -]+$")


class Select(_Sel):
    action: Literal["select"]
    value: str = Field(max_length=500)


class Check(_Sel):
    action: Literal["check"]


class Uncheck(_Sel):
    action: Literal["uncheck"]


class Hover(_Sel):
    action: Literal["hover"]


class WaitFor(_Sel):
    action: Literal["wait_for"]
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"


class ExpectVisible(_Sel):
    action: Literal["expect_visible"]


class ExpectHidden(_Sel):
    action: Literal["expect_hidden"]


class ExpectText(_Sel):
    action: Literal["expect_text"]
    text: str = Field(max_length=2000)
    match: Literal["contains", "equals"] = "contains"


class ExpectUrl(_Base):
    action: Literal["expect_url"]
    contains: str = Field(min_length=1, max_length=2048)


class ExpectTitle(_Base):
    action: Literal["expect_title"]
    contains: str = Field(min_length=1, max_length=500)


class Screenshot(_Base):
    action: Literal["screenshot"]
    name: str = Field(default="checkpoint", max_length=80, pattern=r"^[A-Za-z0-9 _.-]*$")
    full_page: bool = False


class Wait(_Base):
    action: Literal["wait"]
    ms: int = Field(ge=0, le=10_000)


Step = Annotated[
    Union[
        Goto, Click, Fill, Press, Select, Check, Uncheck, Hover, WaitFor,
        ExpectVisible, ExpectHidden, ExpectText, ExpectUrl, ExpectTitle, Screenshot, Wait,
    ],
    Field(discriminator="action"),
]
StepList = TypeAdapter(list[Step])
ACTIONS = [
    "goto", "click", "fill", "press", "select", "check", "uncheck", "hover", "wait_for",
    "expect_visible", "expect_hidden", "expect_text", "expect_url", "expect_title", "screenshot", "wait",
]


def validate_steps(raw: list[dict], max_steps: int) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("steps must be a list")
    if len(raw) > max_steps:
        raise ValueError(f"too many steps (max {max_steps})")
    parsed = StepList.validate_python(raw)
    out = [s.model_dump(exclude_none=True) for s in parsed]
    for i, s in enumerate(out):
        for k, v in s.items():
            if isinstance(v, str) and SECRET_RE.search(v) and not (s["action"] == "fill" and k == "value"):
                raise ValueError(f"step {i + 1}: secrets ({{{{secret:NAME}}}}) are only allowed in the value of a fill step")
    return out


def describe(step: dict) -> str:
    if step.get("description"):
        return step["description"]
    a = step["action"]
    if a == "goto":
        return f"Open {step['url']}"
    if a == "fill":
        return f"Fill {step['selector']}"
    if a == "expect_text":
        return f"Expect {step['selector']} {step.get('match', 'contains')} \"{step['text']}\""
    if a in ("expect_url", "expect_title"):
        return f"Expect {a[7:]} contains \"{step['contains']}\""
    if a == "wait":
        return f"Wait {step['ms']} ms"
    if a == "screenshot":
        return f"Screenshot: {step.get('name', '')}"
    if "selector" in step:
        return f"{a.replace('_', ' ').capitalize()} {step['selector']}"
    return a


def secret_names(steps: list[dict]) -> set[str]:
    names: set[str] = set()
    for s in steps:
        for v in s.values():
            if isinstance(v, str):
                names.update(SECRET_RE.findall(v))
    return names


# JSON schema handed to Claude so its output is constrained to this language.
def json_schema() -> dict:
    return StepList.json_schema()
