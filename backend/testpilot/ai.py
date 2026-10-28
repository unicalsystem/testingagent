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


SYSTEM_GENERATE = """You are TestAgent, a senior QA engineer who writes precise, deterministic
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


SYSTEM_ANALYZE = """You are TestAgent, a senior QA engineer. Analyse a failed automated browser test
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


# --------------------------------------------------------------------------- manual -> automated
SYSTEM_AUTOMATE = """You are TestAgent, a senior test-automation engineer. You convert ONE existing
manual test case into deterministic browser steps, by calling the submit_automation tool exactly once.

Rules:
- Use only the step actions in the tool schema. There is no JavaScript, file upload, e-mail or OS access.
- Use the application map (pages, fields, buttons and links that were actually found in the app) to pick
  selectors. Prefer selectors listed there: #id, [name=...], [data-testid=...], role=button[name="..."],
  text=... . Never invent element ids that are not in the map or notes unless there is no alternative.
- Prefer relative goto paths; only use hosts that are listed as allowed.
- If the case needs a signed-in user and project setup steps exist, set use_setup=true and do NOT repeat
  the sign-in in your steps; start with a goto to the page under test. If the case is about the sign-in
  page itself or needs a signed-out user, set use_setup=false.
- Credentials only as {{secret:NAME}} placeholders from the list provided, and only in fill values of
  password fields. Otherwise use obvious dummy test data.
- End with expect_* assertions that check the case's expected result as closely as the step language
  allows, and take a screenshot at the key checkpoint.
- Set automatable=false (with a short reason, and no steps) when the case cannot be verified in a browser
  with these actions: it needs a mailbox, SMS, CAPTCHA, file upload/download, another device, visual or
  performance judgement, inspection of source code/documents, or data you do not have.
- Keep 'reason' short: assumptions you made, or why the case stays manual."""


def _automate_tool() -> dict:
    items_schema, defs = _steps_schema()
    return {
        "name": "submit_automation",
        "description": "Submit the automated version of the manual test case.",
        "input_schema": {
            "type": "object",
            "$defs": defs,
            "properties": {
                "automatable": {"type": "boolean"},
                "reason": {"type": "string", "maxLength": 1000},
                "use_setup": {"type": "boolean"},
                "steps": items_schema,
            },
            "required": ["automatable", "reason", "steps"],
        },
    }


def automate_case(*, case: dict, base_url: str, hosts: list[str], secret_names: list[str], setup_steps: list[dict],
                  app_map: str = "", app_notes: str = "", previous: dict | None = None,
                  screenshot: bytes | None = None, screenshot_mime: str = "image/jpeg") -> dict:
    """Returns {automatable, reason, use_setup, steps} with validated steps. `previous` (for a repair)
    holds the steps that failed and the error of the trial run."""
    s = get_settings()
    fields = [("Test case ID", "external_id"), ("Title", "name"), ("Module", "module"), ("Sub-module", "sub_module"),
              ("Scenario type", "scenario_type"), ("Description", "description"), ("Pre-conditions", "preconditions"),
              ("Test data", "test_data"), ("Procedure", "procedure"), ("Expected result", "expected_result")]
    case_txt = "\n".join(f"{label}: {case.get(k)}" for label, k in fields if case.get(k))
    setup_txt = "\n".join(f"  {i + 1}. {steps_mod.describe(st)}" for i, st in enumerate(setup_steps)) or "  (none)"
    text = (
        f"Project base URL: {base_url}\nAllowed hosts: {', '.join(hosts)}\n"
        f"Secret placeholders: {', '.join('{{secret:%s}}' % n for n in secret_names) or '(none)'}\n"
        f"Project setup steps (run before the case when use_setup=true):\n{setup_txt}\n\n"
        f"<manual_test_case>\n{case_txt}\n</manual_test_case>\n"
    )
    if app_notes:
        text += f"\n<app_notes>\n{app_notes[:8000]}\n</app_notes>\n"
    if app_map:
        text += f"\n<application_map>\n{app_map[:40000]}\n</application_map>\n"
    content: list[dict] = []
    if previous:
        import json as _json

        text += ("\nYour previous automation of this case FAILED when it was executed. Fix it.\n"
                 f"<previous_steps>\n{_json.dumps(previous.get('steps', []), indent=1)[:12000]}\n</previous_steps>\n"
                 f"<failure>\n{str(previous.get('error', ''))[:4000]}\n</failure>\n"
                 "If the failure shows the application itself does not meet the expected result (a real defect), keep the "
                 "assertion and say so in 'reason'. If it cannot work with the available actions, set automatable=false.\n")
        if screenshot and len(screenshot) < 3_500_000:
            content.append({"type": "image", "source": {"type": "base64", "media_type": screenshot_mime,
                                                        "data": base64.b64encode(screenshot).decode()}})
            text += "The image is the screenshot taken at the failing step.\n"
    content.append({"type": "text", "text": text + "\nCall the submit_automation tool exactly once."})
    resp = _call(max_tokens=s.claude_max_tokens, system=SYSTEM_AUTOMATE, tools=[_automate_tool()],
                 tool_choice={"type": "auto"}, messages=[{"role": "user", "content": content}])
    payload = None
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "submit_automation":
            payload = block.input
            break
    if payload is None:
        import json as _json
        import re as _re

        raw = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        m = _re.search(r"\{.*\}", raw, _re.S)
        try:
            payload = _json.loads(m.group(0)) if m else None
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        raise AIUnavailable("Claude did not return an automation")
    automatable = bool(payload.get("automatable", True))
    reason = str(payload.get("reason", ""))[:1000]
    steps: list[dict] = []
    if automatable:
        try:
            steps = steps_mod.validate_steps(payload.get("steps", []), s.max_steps_per_case)
        except (ValidationError, ValueError, TypeError) as e:
            raise ValueError(f"Claude returned invalid steps: {str(e)[:500]}")
        if not steps:
            automatable, reason = False, reason or "No steps were produced"
    return {"automatable": automatable, "reason": reason, "use_setup": bool(payload.get("use_setup", False)) and bool(setup_steps),
            "steps": steps}
