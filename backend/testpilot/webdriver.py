"""Minimal W3C WebDriver client for Appium 2 hubs (BrowserStack, LambdaTest, Sauce Labs, own Appium) — release 1.8.

Only the commands TestAgent's mobile step language needs. Arbitrary script execution is not exposed to steps; the
one `execute` call is used internally with fixed vendor commands (marking a cloud session passed/failed).
"""
from __future__ import annotations

import base64
import time
from typing import Any

import httpx

ELEMENT_KEY = "element-6066-11e4-a52e-4f735466cecf"


class WebDriverError(Exception):
    def __init__(self, message: str, error: str = "", status: int = 0):
        super().__init__(message)
        self.error, self.status = error, status


class NoSuchElement(WebDriverError):
    pass


class Session:
    def __init__(self, hub: str, session_id: str, client: httpx.Client, capabilities: dict | None = None):
        self.hub, self.id, self.c = hub.rstrip("/"), session_id, client
        self.capabilities = capabilities or {}
        self._size: tuple[int, int] | None = None

    # ---- lifecycle ----
    @classmethod
    def create(cls, hub: str, caps: dict, *, auth: tuple[str, str] | None = None, timeout_s: float = 360) -> "Session":
        c = httpx.Client(auth=auth, timeout=60, trust_env=False, follow_redirects=False,
                         headers={"User-Agent": "TestAgent-Mobile/1.8", "Content-Type": "application/json"})
        try:
            r = c.post(f"{hub.rstrip('/')}/session", json={"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}},
                       timeout=timeout_s)
        except httpx.HTTPError as e:
            c.close()
            raise WebDriverError(f"could not reach the device hub: {type(e).__name__}: {str(e)[:200]}")
        data = _json(r)
        val = data.get("value", {}) if isinstance(data, dict) else {}
        if r.status_code >= 300 or not isinstance(val, dict) or "sessionId" not in val and "sessionId" not in data:
            c.close()
            msg = val.get("message") if isinstance(val, dict) else None
            raise WebDriverError(f"device session was not created (HTTP {r.status_code}): {(msg or r.text)[:600]}",
                                 val.get("error", "") if isinstance(val, dict) else "", r.status_code)
        sid = val.get("sessionId") or data.get("sessionId")
        return cls(hub, sid, c, val.get("capabilities") or {})

    def quit(self) -> None:
        try:
            self.c.delete(f"{self.hub}/session/{self.id}", timeout=30)
        except httpx.HTTPError:
            pass
        finally:
            self.c.close()

    # ---- transport ----
    def cmd(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
        url = f"{self.hub}/session/{self.id}{path}"
        try:
            r = self.c.request(method, url, json=body if body is not None else ({} if method == "POST" else None), timeout=timeout)
        except httpx.HTTPError as e:
            raise WebDriverError(f"{method} {path}: {type(e).__name__}: {str(e)[:200]}")
        data = _json(r)
        val = data.get("value") if isinstance(data, dict) else None
        if r.status_code >= 300 or (isinstance(val, dict) and val.get("error")):
            err = val.get("error", "") if isinstance(val, dict) else ""
            msg = (val.get("message") if isinstance(val, dict) else None) or r.text[:300]
            cls = NoSuchElement if err in ("no such element", "stale element reference") else WebDriverError
            raise cls(f"{err or 'error'}: {str(msg)[:500]}", err, r.status_code)
        return val

    # ---- elements ----
    def find(self, strategy: str, value: str) -> str:
        v = self.cmd("POST", "/element", {"using": strategy, "value": value})
        return v.get(ELEMENT_KEY) or v.get("ELEMENT")

    def find_all(self, strategy: str, value: str) -> list[str]:
        return [e.get(ELEMENT_KEY) or e.get("ELEMENT") for e in (self.cmd("POST", "/elements", {"using": strategy, "value": value}) or [])]

    def wait_find(self, strategy: str, value: str, timeout_s: float, *, visible: bool = True) -> str:
        end = time.monotonic() + timeout_s
        last: Exception | None = None
        while True:
            try:
                el = self.find(strategy, value)
                if not visible or self.displayed(el):
                    return el
                last = WebDriverError("element found but not visible")
            except NoSuchElement as e:
                last = e
            if time.monotonic() >= end:
                raise NoSuchElement(f"not found within {timeout_s:.0f}s: {strategy}={value} ({last})")
            time.sleep(0.5)

    def click(self, el: str) -> None:
        self.cmd("POST", f"/element/{el}/click")

    def clear(self, el: str) -> None:
        self.cmd("POST", f"/element/{el}/clear")

    def send_keys(self, el: str, text: str) -> None:
        self.cmd("POST", f"/element/{el}/value", {"text": text, "value": list(text)})

    def text(self, el: str) -> str:
        return self.cmd("GET", f"/element/{el}/text") or ""

    def displayed(self, el: str) -> bool:
        try:
            return bool(self.cmd("GET", f"/element/{el}/displayed"))
        except WebDriverError:
            return True  # some drivers do not implement it; presence is enough

    # ---- screen ----
    def screenshot(self) -> bytes:
        return base64.b64decode(self.cmd("GET", "/screenshot", timeout=60) or "")

    def source(self) -> str:
        return self.cmd("GET", "/source", timeout=60) or ""

    def size(self) -> tuple[int, int]:
        if not self._size:
            try:
                v = self.cmd("GET", "/window/rect")
            except WebDriverError:
                v = self.cmd("GET", "/window/size")
            self._size = (int(v.get("width") or 0), int(v.get("height") or 0))
        return self._size

    def back(self) -> None:
        self.cmd("POST", "/back")

    def navigate(self, url: str) -> None:
        self.cmd("POST", "/url", {"url": url}, timeout=90)

    def current_url(self) -> str:
        return self.cmd("GET", "/url") or ""

    def hide_keyboard(self) -> None:
        try:
            self.cmd("POST", "/appium/device/hide_keyboard")
        except WebDriverError:
            pass

    def activate_app(self, app_id: str) -> None:
        self.cmd("POST", "/appium/device/activate_app", {"appId": app_id, "bundleId": app_id})

    def _pointer(self, steps: list[dict]) -> None:
        self.cmd("POST", "/actions", {"actions": [{"type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"},
                                                    "actions": steps}]})
        try:
            self.cmd("DELETE", "/actions")
        except WebDriverError:
            pass

    def tap_at(self, x: int, y: int) -> None:
        self._pointer([{"type": "pointerMove", "duration": 0, "x": x, "y": y}, {"type": "pointerDown", "button": 0},
                       {"type": "pause", "duration": 80}, {"type": "pointerUp", "button": 0}])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 400) -> None:
        self._pointer([{"type": "pointerMove", "duration": 0, "x": x1, "y": y1}, {"type": "pointerDown", "button": 0},
                       {"type": "pause", "duration": 100}, {"type": "pointerMove", "duration": ms, "x": x2, "y": y2},
                       {"type": "pointerUp", "button": 0}])

    def execute(self, script: str, args: list | None = None) -> Any:
        return self.cmd("POST", "/execute/sync", {"script": script, "args": args or []})


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {}
