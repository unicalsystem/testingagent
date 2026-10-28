"""The declarative step language for mobile tests (release 1.8).

Like the web step language, steps are data: there is no step that executes scripts, shell commands or arbitrary
Appium "mobile:" commands. Targets are written as a strategy prefix plus a value:

    ~Login                       accessibility id (content-desc on Android, accessibilityIdentifier on iOS)
    id=com.acme:id/username      resource id / element id
    text=Sign in                 visible text / label / name (exact)
    contains=Sign                visible text containing the value
    xpath=//android.widget.Button[@text='OK']
    class=android.widget.EditText
    css=#email                   (mobile web only)
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

SECRET_RE = re.compile(r"\{\{\s*secret:([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
TARGET_RE = re.compile(r"^(~.+|(id|text|contains|xpath|class|css)=.+)$", re.S)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=500)
    timeout_ms: int | None = Field(default=None, ge=100, le=120_000)


class _T(_Base):
    target: str = Field(min_length=2, max_length=1000)

    @field_validator("target")
    @classmethod
    def _valid(cls, v: str) -> str:
        v = v.strip()
        if not TARGET_RE.match(v):
            raise ValueError("target must look like ~accessibilityId, id=..., text=..., contains=..., xpath=..., class=... or css=...")
        return v


class LaunchApp(_Base):
    action: Literal["launch_app"]


class OpenUrl(_Base):
    action: Literal["open_url"]
    url: str = Field(min_length=1, max_length=2048)


class Tap(_T):
    action: Literal["tap"]


class TapAt(_Base):
    action: Literal["tap_at"]
    x: float = Field(ge=0, le=1, description="fraction of screen width")
    y: float = Field(ge=0, le=1, description="fraction of screen height")


class TypeText(_T):
    action: Literal["type"]
    text: str = Field(max_length=5000, description="May reference project secrets as {{secret:NAME}}")
    clear_first: bool = True


class Clear(_T):
    action: Literal["clear"]


class Swipe(_Base):
    action: Literal["swipe"]
    direction: Literal["up", "down", "left", "right"]
    distance: float = Field(default=0.6, ge=0.1, le=0.9)


class ScrollTo(_T):
    action: Literal["scroll_to"]
    direction: Literal["up", "down"] = "down"
    max_swipes: int = Field(default=8, ge=1, le=20)


class Back(_Base):
    action: Literal["back"]


class HideKeyboard(_Base):
    action: Literal["hide_keyboard"]


class WaitFor(_T):
    action: Literal["wait_for"]
    state: Literal["visible", "hidden"] = "visible"


class ExpectVisible(_T):
    action: Literal["expect_visible"]


class ExpectHidden(_T):
    action: Literal["expect_hidden"]


class ExpectText(_T):
    action: Literal["expect_text"]
    text: str = Field(max_length=2000)
    match: Literal["contains", "equals"] = "contains"


class Screenshot(_Base):
    action: Literal["screenshot"]
    name: str = Field(default="checkpoint", max_length=80, pattern=r"^[A-Za-z0-9 _.-]*$")


class Wait(_Base):
    action: Literal["wait"]
    ms: int = Field(ge=0, le=15_000)


Step = Annotated[Union[LaunchApp, OpenUrl, Tap, TapAt, TypeText, Clear, Swipe, ScrollTo, Back, HideKeyboard, WaitFor,
                       ExpectVisible, ExpectHidden, ExpectText, Screenshot, Wait], Field(discriminator="action")]
StepList = TypeAdapter(list[Step])
ACTIONS = ["launch_app", "open_url", "tap", "tap_at", "type", "clear", "swipe", "scroll_to", "back", "hide_keyboard",
           "wait_for", "expect_visible", "expect_hidden", "expect_text", "screenshot", "wait"]
ASSERTIONS = {"expect_visible", "expect_hidden", "expect_text"}


def validate_steps(raw: list, max_steps: int = 200) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("steps must be a list")
    if len(raw) > max_steps:
        raise ValueError(f"too many steps (max {max_steps})")
    out = [s.model_dump(exclude_none=True) for s in StepList.validate_python(raw)]
    for i, s in enumerate(out):
        for k, v in s.items():
            if isinstance(v, str) and SECRET_RE.search(v) and not (s["action"] == "type" and k == "text"):
                raise ValueError(f"step {i + 1}: secrets ({{{{secret:NAME}}}}) are only allowed in the text of a type step")
    return out


def json_schema() -> dict:
    return StepList.json_schema()


def describe(s: dict) -> str:
    if s.get("description"):
        return s["description"]
    a = s["action"]
    if a == "launch_app":
        return "Launch the app"
    if a == "open_url":
        return f"Open {s['url']}"
    if a == "tap":
        return f"Tap {s['target']}"
    if a == "tap_at":
        return f"Tap at {round(s['x'] * 100)}% / {round(s['y'] * 100)}%"
    if a == "type":
        return f"Type into {s['target']}"
    if a == "swipe":
        return f"Swipe {s['direction']}"
    if a == "scroll_to":
        return f"Scroll {s.get('direction', 'down')} to {s['target']}"
    if a == "expect_text":
        return f"Expect {s['target']} {s.get('match', 'contains')} \"{s['text']}\""
    if a == "wait":
        return f"Wait {s['ms']} ms"
    if a == "screenshot":
        return f"Screenshot: {s.get('name', '')}"
    if "target" in s:
        return f"{a.replace('_', ' ').capitalize()} {s['target']}"
    return a.replace("_", " ").capitalize()


def secret_names(steps: list[dict]) -> set[str]:
    out: set[str] = set()
    for s in steps:
        for v in s.values():
            if isinstance(v, str):
                out.update(SECRET_RE.findall(v))
    return out


def _xq(v: str) -> str:
    """XPath string literal that survives both quote kinds."""
    if "'" not in v:
        return f"'{v}'"
    if '"' not in v:
        return f'"{v}"'
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in v.split("'")) + ")"


def locator(target: str, *, web: bool = False) -> tuple[str, str]:
    """Maps a target to a W3C/Appium (strategy, value) pair."""
    t = target.strip()
    if t.startswith("~"):
        return ("css selector", f"[aria-label={_cssq(t[1:])}]") if web else ("accessibility id", t[1:])
    k, _, v = t.partition("=")
    if k == "css":
        return "css selector", v
    if k == "xpath":
        return "xpath", v
    if k == "class":
        return ("css selector", v) if web else ("class name", v)
    if k == "id":
        return ("css selector", f"[id={_cssq(v)}]") if web else ("id", v)
    q = _xq(v)
    if k == "text":
        if web:
            return "xpath", f"//*[normalize-space(text())={q} or @value={q} or @aria-label={q}]"
        return "xpath", f"//*[@text={q} or @label={q} or @name={q} or @content-desc={q} or @value={q}]"
    if k == "contains":
        if web:
            return "xpath", f"//*[contains(normalize-space(text()), {q})]"
        return "xpath", f"//*[contains(@text, {q}) or contains(@label, {q}) or contains(@name, {q}) or contains(@content-desc, {q})]"
    raise ValueError(f"unsupported target {target}")


def _cssq(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
