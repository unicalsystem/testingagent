"""Claude integration. Every AI feature is optional: when the API key is missing or the API is
unreachable, callers get AIUnavailable and the rest of the application keeps working."""
from __future__ import annotations

import base64
import copy
import logging
import threading
import time
from typing import Any

from pydantic import ValidationError

from . import steps as steps_mod
from .config import get_settings

log = logging.getLogger(__name__)


class AIUnavailable(RuntimeError):
    pass


_state = {"last_error": "", "last_error_at": 0.0, "last_ok_at": 0.0}
_lock = threading.Lock()
_client_override: Any = None  # tests inject a fake client here
COOLDOWN_SECONDS = 60


def status() -> dict:
    s = get_settings()
    configured = s.ai_configured or _client_override is not None
    cooling = time.time() - _state["last_error_at"] < COOLDOWN_SECONDS
    return {
        "configured": configured,
        "available": configured and not cooling,
        "model": s.claude_model,
        "last_error": _state["last_error"] if cooling else "",
    }


def _client():
    if _client_override is not None:
        return _client_override
    s = get_settings()
    if not s.ai_configured:
        raise AIUnavailable("Claude API key is not configured")
    import anthropic

    headers = {}
    if s.anthropic_workspace_id.strip():
        headers["anthropic-workspace-id"] = s.anthropic_workspace_id.strip()
    return anthropic.Anthropic(
        api_key=s.anthropic_api_key.get_secret_value(),
        timeout=s.claude_timeout_seconds,
        max_retries=2,
        default_headers=headers or None,
    )


def _call(**kwargs):
    st = status()
    if not st["configured"]:
        raise AIUnavailable("Claude API key is not configured")
    if not st["available"]:
        raise AIUnavailable(f"Claude API temporarily unavailable: {st['last_error']}")
    try:
        resp = _client().messages.create(model=get_settings().claude_model, **kwargs)
    except AIUnavailable:
        raise
    except Exception as e:  # network, auth, rate limit, overload
        msg = type(e).__name__
        code = getattr(e, "status_code", None)
        if code:
            msg += f" (HTTP {code})"
        # Surface Anthropic's own explanation (it never contains the key).
        body = getattr(e, "body", None)
        detail = body.get("error", {}).get("message") if isinstance(body, dict) else None
        if detail:
            msg += f": {str(detail)[:300]}"
        with _lock:
            # Client errors caused by the request itself should not disable AI for everyone.
            if code is None or code in (401, 403, 429) or code >= 500:
                _state.update(last_error=msg, last_error_at=time.time())
        log.warning("Claude API call failed: %s", msg)
        raise AIUnavailable(f"Claude API call failed: {msg}") from e
    with _lock:
        _state.update(last_ok_at=time.time(), last_error="", last_error_at=0.0)
    return resp


def _steps_schema() -> tuple[dict, dict]:
    sch = copy.deepcopy(steps_mod.json_schema())
    defs = sch.pop("$defs", {})

    def clean(node):
        if isinstance(node, dict):
            node.pop("discriminator", None)
            node.pop("title", None)
            for v in node.values():
                clean(v)
        elif isinstance(node, list):
            for v in node:
                clean(v)

    clean(sch)
    clean(defs)
    return sch, defs


SYSTEM_GENERATE = """You are TestPilot AI, a senior QA engineer who writes precise, deterministic
end-to-end browser test cases. You output test cases ONLY by calling the submit_test_cases tool.

Rules:
- Use only the step actions in the tool schema. There is no JavaScript execution step; never try to run scripts.
- Prefer relative paths for goto (e.g. "/login"); they are resolved against the project base URL.
- Only reference hosts listed as allowed. Never invent external URLs.
- Prefer robust Playwright selectors: data-testid (e.g. [data-testid=save]), role selectors
  (e.g. role=button[name="Sign in"]), label/text selectors (e.g. text=Welcome), then CSS ids.
- Never put real passwords or personal data in steps. For credentials use the provided secret
  placeholders in the form {{secret:NAME}}; if none fit, use obvious dummy test data.
- Every test case must end with at least one expect_* assertion and should take a screenshot at key checkpoints.
- Each test case must be independent (start with a goto).
- Give each step a short human-readable description."""


def generate_test_cases(*, base_url: str, hosts: list[str], secret_names: list[str], requirement: str,
                        page_notes: str = "", count: int = 3) -> dict:
    s = get_settings()
    items_schema, defs = _steps_schema()
    tool = {
        "name": "submit_test_cases",
        "description": "Submit the generated test cases.",
        "input_schema": {
            "type": "object",
            "$defs": defs,
            "properties": {
                "test_cases": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "maxLength": 200},
                            "description": {"type": "string", "maxLength": 2000},
                            "tags": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 8},
                            "steps": items_schema,
                        },
                        "required": ["name", "steps"],
                    },
                }
            },
            "required": ["test_cases"],
        },
    }
    user = (
        f"Project base URL: {base_url}\n"
        f"Allowed hosts: {', '.join(hosts)}\n"
        f"Available secret placeholders: {', '.join('{{secret:%s}}' % n for n in secret_names) or '(none)'}\n"
        f"Number of test cases wanted: {count}\n\n"
        f"Requirement / feature to test:\n<requirement>\n{requirement}\n</requirement>\n"
    )
    if page_notes:
        user += f"\nNotes about the application's pages and elements:\n<notes>\n{page_notes}\n</notes>\n"
    resp = _call(
        max_tokens=s.claude_max_tokens,
        system=SYSTEM_GENERATE,
        tools=[tool],
        # Some current models reject forced tool_choice ("tool"/"any"); "auto" plus a clear
        # instruction works everywhere, and plain-JSON replies are accepted as a fallback.
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": user + "\nCall the submit_test_cases tool exactly once with your answer."}],
    )
    payload = None
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "submit_test_cases":
            payload = block.input
            break
    if payload is None:
        payload = _json_from_text("".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"))
    if not isinstance(payload, dict):
        raise AIUnavailable("Claude did not return test cases")
    cases, rejected = [], []
    for i, c in enumerate(payload.get("test_cases", [])[:10]):
        try:
            name = str(c.get("name", "")).strip()[:200] or f"Generated test {i + 1}"
            st = steps_mod.validate_steps(c.get("steps", []), s.max_steps_per_case)
            if not st:
                raise ValueError("no steps")
            cases.append({
                "name": name,
                "description": str(c.get("description", ""))[:2000],
                "tags": [str(t)[:40] for t in c.get("tags", [])][:8],
                "steps": st,
            })
        except (ValidationError, ValueError, TypeError, AttributeError) as e:
            rejected.append({"index": i, "error": str(e)[:500]})
    return {"test_cases": cases, "rejected": rejected, "model": s.claude_model}


def _json_from_text(text: str):
    import json
    import re

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) and "test_cases" in data else None


SYSTEM_ANALYZE = """You are TestPilot AI, a senior QA engineer. Analyse a failed automated browser test
run and explain it to a tester. Be concise and concrete. Structure your answer as:
1. What failed (one sentence)
2. Most likely cause (application defect vs. test defect vs. environment), with reasoning
3. Suggested fix to the test steps or to the application
Do not invent facts that are not supported by the log or screenshot."""


def analyze_run(*, case_name: str, step_rows: list[dict], run_error: str, screenshot: bytes | None,
                screenshot_mime: str = "image/jpeg") -> str:
    lines = []
    for r in step_rows:
        lines.append(f"#{r['idx'] + 1} [{r['status']}] {r['action']}: {r['description']}"
                     + (f"\n    error: {r['error'][:1500]}" if r.get("error") else ""))
    text = (f"Test case: {case_name}\nRun error: {run_error or '(none)'}\n\nSteps:\n" + "\n".join(lines))
    content: list[dict] = [{"type": "text", "text": text}]
    if screenshot and len(screenshot) < 3_500_000:
        content.insert(0, {
            "type": "image",
            "source": {"type": "base64", "media_type": screenshot_mime, "data": base64.b64encode(screenshot).decode()},
        })
        content.append({"type": "text", "text": "The image is the screenshot taken at the failing step."})
    resp = _call(max_tokens=1500, system=SYSTEM_ANALYZE, messages=[{"role": "user", "content": content}])
    out = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
    if not out:
        raise AIUnavailable("Claude returned an empty analysis")
    return out
