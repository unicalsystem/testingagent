"""Device drivers for mobile tests (release 1.8).

Two drivers share one interface, used by test runs, the recorder and the AI agent:

  AppiumDriver    a W3C / Appium session on a real device (BrowserStack, LambdaTest, Sauce Labs, own Appium,
                  Unical lab): native / hybrid apps and the device's own browser.
  EmulatorDriver  TestAgent's own Chromium with phone emulation (viewport, touch, user agent): mobile web only.
                  All traffic goes through the per-session egress proxy, like browser runs.

observe() returns what a person (or Claude) needs to pick the next action: a screenshot and a numbered list of
the visible, interesting elements with the best selector for each.
"""
from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import mobilesteps as ms
from .webdriver import NoSuchElement, Session, WebDriverError

try:  # optional: smaller screenshots
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None

try:
    from defusedxml import ElementTree as ET  # page sources come from the device
except Exception:  # pragma: no cover
    import xml.etree.ElementTree as ET  # type: ignore

MAX_ELEMENTS = 120
SECRET_RE = ms.SECRET_RE


class StepError(Exception):
    pass


@dataclass
class Observation:
    image: bytes
    mime: str
    width: int
    height: int
    elements: list[dict] = field(default_factory=list)
    url: str = ""

    def meta(self) -> dict:
        return {"width": self.width, "height": self.height, "elements": self.elements, "url": self.url}


def compress(png: bytes, max_side: int = 1400, quality: int = 70) -> tuple[bytes, str]:
    """PNG -> JPEG (smaller evidence, cheaper for Claude). Keeps PNG when Pillow is unavailable."""
    if not Image or not png:
        return png, "image/png"
    try:
        im = Image.open(io.BytesIO(png))
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return png, "image/png"


# --------------------------------------------------------------------------- element extraction
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_INTERACTIVE_ANDROID = ("Button", "EditText", "CheckBox", "Switch", "RadioButton", "Spinner", "ImageButton", "ToggleButton",
                        "SeekBar", "Tab")
_INTERACTIVE_IOS = ("Button", "TextField", "SecureTextField", "Switch", "Cell", "Link", "SearchField", "TextView",
                    "SegmentedControl", "Slider", "Picker", "Tab", "Key")


def _short(v: str, n: int = 80) -> str:
    v = " ".join((v or "").split())
    return v if len(v) <= n else v[: n - 1] + "…"


def native_elements(xml: str, width: int, height: int) -> list[dict]:
    """Parses an Appium page source (UiAutomator2 or XCUITest) into selectable elements."""
    if not xml or width <= 0 or height <= 0:
        return []
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except Exception:
        return []
    raw: list[dict] = []
    tag_count: dict[str, int] = {}
    for node in root.iter():
        tag = node.tag if isinstance(node.tag, str) else ""
        a = node.attrib
        cls = a.get("class") or tag
        tag_count[cls] = tag_count.get(cls, 0) + 1
        index = tag_count[cls]
        ios = tag.startswith("XCUIElementType")
        if ios:
            if a.get("visible", "true") == "false":
                continue
            try:
                x, y, w, h = (int(float(a.get(k, "0"))) for k in ("x", "y", "width", "height"))
            except ValueError:
                continue
            label, text_v, acc = a.get("label") or "", a.get("value") or "", a.get("name") or ""
            interactive = any(tag.endswith(s) for s in _INTERACTIVE_IOS) or a.get("accessible") == "true"
            rid = ""
        else:
            if a.get("displayed", "true") == "false":
                continue
            m = _BOUNDS.match(a.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            x, y, w, h = x1, y1, x2 - x1, y2 - y1
            label, text_v, acc = a.get("text") or "", "", a.get("content-desc") or ""
            rid = a.get("resource-id") or ""
            interactive = (a.get("clickable") == "true" or a.get("focusable") == "true" and "EditText" in cls
                           or any(cls.endswith(s) for s in _INTERACTIVE_ANDROID))
        if w <= 2 or h <= 2 or x >= width or y >= height or x + w <= 0 or y + h <= 0:
            continue
        if not (interactive or label or acc or text_v):
            continue
        raw.append({"cls": cls, "index": index, "label": label, "value": text_v, "acc": acc, "rid": rid,
                    "interactive": bool(interactive), "box": (x, y, w, h)})
    # uniqueness of candidate selectors
    cnt: dict[str, int] = {}
    for r in raw:
        for k in _candidates(r):
            cnt[k] = cnt.get(k, 0) + 1
    out = []
    for r in raw:
        target = next((c for c in _candidates(r) if cnt.get(c) == 1), None) or f"xpath=(//{r['cls']})[{r['index']}]"
        x, y, w, h = r["box"]
        out.append({"type": r["cls"].split(".")[-1].replace("XCUIElementType", ""),
                    "text": _short(r["label"] or r["value"]), "desc": _short(r["acc"]), "id": r["rid"],
                    "interactive": r["interactive"], "target": target,
                    "box": [round(max(0, x) / width, 4), round(max(0, y) / height, 4),
                            round(min(w, width) / width, 4), round(min(h, height) / height, 4)]})
    # interactive elements first, then text, both in screen order; bounded
    out.sort(key=lambda e: (not e["interactive"], round(e["box"][1], 2), e["box"][0]))
    out = out[:MAX_ELEMENTS]
    for i, e in enumerate(out, 1):
        e["ref"] = i
    return out


def _candidates(r: dict) -> list[str]:
    c = []
    if r["acc"] and len(r["acc"]) <= 200:
        c.append("~" + r["acc"])
    if r["rid"]:
        c.append("id=" + r["rid"])
    if r["label"] and len(r["label"]) <= 120 and "\n" not in r["label"]:
        c.append("text=" + r["label"])
    return c


# Collects visible interactive / textual elements of a web page. Fixed script, never user supplied.
WEB_ELEMENTS_JS = r"""
(() => {
  const W = window.innerWidth, H = window.innerHeight, out = [];
  const sel = 'a,button,input,select,textarea,label,summary,[role=button],[role=link],[role=tab],[role=checkbox],[role=menuitem],[onclick],[tabindex],h1,h2,h3,h4,p,li,span,td,th,img[alt]';
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= 400) break;
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3 || r.bottom < 0 || r.right < 0 || r.top > H || r.left > W) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    const tag = el.tagName.toLowerCase();
    const interactive = /^(a|button|input|select|textarea|summary)$/.test(tag) || el.hasAttribute('onclick') || /button|link|tab|checkbox|menuitem/.test(el.getAttribute('role') || '');
    let text = (el.innerText || el.value || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ');
    if (!interactive) {
      if (!text || text.length > 160) continue;
      if ([...el.children].some(c => (c.innerText || '').trim() === text)) continue;  // keep the innermost
    }
    if (seen.has(el)) continue; seen.add(el);
    out.push({tag, type: el.getAttribute('type') || '', text: text.slice(0, 120), aria: el.getAttribute('aria-label') || '',
      id: el.id || '', name: el.getAttribute('name') || '', placeholder: el.getAttribute('placeholder') || '',
      interactive, box: [r.left / W, r.top / H, r.width / W, r.height / H]});
  }
  return {w: W, h: H, url: location.href, items: out};
})()
"""


def web_elements(items: list[dict]) -> list[dict]:
    cnt: dict[str, int] = {}
    for r in items:
        for k in _web_candidates(r):
            cnt[k] = cnt.get(k, 0) + 1
    out = []
    for r in items:
        target = next((c for c in _web_candidates(r) if cnt.get(c) == 1), None)
        if not target:
            if not r.get("interactive"):
                continue
            target = next(iter(_web_candidates(r)), None)
            if not target:
                continue
        out.append({"type": r["tag"] + (f"[{r['type']}]" if r.get("type") else ""), "text": _short(r.get("text", "")),
                    "desc": _short(r.get("aria") or r.get("placeholder") or ""), "id": r.get("id", ""),
                    "interactive": bool(r.get("interactive")), "target": target,
                    "box": [round(max(0.0, float(v)), 4) for v in r["box"]]})
    out.sort(key=lambda e: (not e["interactive"], round(e["box"][1], 2), e["box"][0]))
    out = out[:MAX_ELEMENTS]
    for i, e in enumerate(out, 1):
        e["ref"] = i
    return out


_CSS_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def _web_candidates(r: dict) -> list[str]:
    c = []
    if r.get("id") and _CSS_ID.match(r["id"]):
        c.append("id=" + r["id"])
    if r.get("aria"):
        c.append("~" + r["aria"][:200])
    if r.get("name") and r["tag"] in ("input", "select", "textarea"):
        c.append(f"css={r['tag']}[name=\"{r['name'][:100]}\"]")
    if r.get("placeholder"):
        c.append(f"css=[placeholder=\"{r['placeholder'][:100]}\"]")
    t = r.get("text") or ""
    if t and len(t) <= 80:
        c.append("text=" + t)
    # attribute values containing quotes would need escaping: skip those css candidates
    return [x for x in c if not (x.startswith("css=") and x.count('"') != 2)]


def element_at(elements: list[dict], fx: float, fy: float) -> dict | None:
    """Smallest element whose box contains the point (interactive ones preferred)."""
    hits = [e for e in elements if e["box"][0] <= fx <= e["box"][0] + e["box"][2] and e["box"][1] <= fy <= e["box"][1] + e["box"][3]]
    if not hits:
        return None
    return min(hits, key=lambda e: (not e["interactive"], e["box"][2] * e["box"][3]))


# --------------------------------------------------------------------------- driver base
class Driver:
    web = False
    timeout_s = 15.0

    def __init__(self, secrets: dict[str, str] | None = None):
        from .config import get_settings

        self.secrets = secrets or {}
        self.timeout_s = max(1.0, get_settings().step_timeout_ms / 1000)

    def resolve(self, text: str) -> str:
        return SECRET_RE.sub(lambda m: self.secrets.get(m.group(1), ""), text)

    # implemented by subclasses
    def observe(self, with_elements: bool = True) -> Observation: ...
    def screenshot(self) -> tuple[bytes, str]: ...
    def close(self, passed: bool | None = None, reason: str = "") -> None: ...

    def perform(self, s: dict, start_url: str = "") -> str:
        """Runs one validated step. Returns a short note; raises StepError on failure."""
        a = s["action"]
        tmo = (s.get("timeout_ms") or self.timeout_s * 1000) / 1000
        try:
            return getattr(self, "_" + a)(s, tmo, start_url) or ""
        except StepError:
            raise
        except NoSuchElement as e:
            raise StepError(str(e)[:800])
        except WebDriverError as e:
            raise StepError(str(e)[:800])

    def _wait(self, s, tmo, start_url):
        time.sleep(s["ms"] / 1000)

    def _screenshot(self, s, tmo, start_url):
        return "checkpoint"


# --------------------------------------------------------------------------- Appium (real devices)
class AppiumDriver(Driver):
    def __init__(self, session: Session, *, web: bool, platform: str, app_id: str = "", secrets: dict | None = None,
                 provider=None, check_url=None):
        super().__init__(secrets)
        self.s, self.web, self.platform, self.app_id = session, web, platform, app_id
        self.provider, self.check_url = provider, check_url

    @property
    def session_id(self) -> str:
        return self.s.id

    def _loc(self, target: str) -> tuple[str, str]:
        return ms.locator(target, web=self.web)

    def _find(self, target: str, tmo: float, visible: bool = True) -> str:
        st, v = self._loc(target)
        return self.s.wait_find(st, v, tmo, visible=visible)

    def _viewport(self) -> tuple[int, int]:
        if self.web:
            try:
                v = self.s.execute("return [window.innerWidth, window.innerHeight]")
                return int(v[0]), int(v[1])
            except Exception:
                pass
        return self.s.size()

    # actions
    def _launch_app(self, s, tmo, start_url):
        if self.web:
            return self._open_url({"url": start_url}, tmo, start_url) if start_url else "browser ready"
        if self.app_id:
            self.s.activate_app(self.app_id)
        return "app launched"

    def _open_url(self, s, tmo, start_url):
        url = s["url"]
        if url.startswith("/") and start_url:
            from urllib.parse import urljoin
            url = urljoin(start_url, url)
        if self.check_url:
            self.check_url(url)
        self.s.navigate(url)

    def _tap(self, s, tmo, start_url):
        self.s.click(self._find(s["target"], tmo))

    def _tap_at(self, s, tmo, start_url):
        w, h = self._viewport()
        self.s.tap_at(int(s["x"] * w), int(s["y"] * h))

    def _type(self, s, tmo, start_url):
        el = self._find(s["target"], tmo)
        if s.get("clear_first", True):
            try:
                self.s.clear(el)
            except WebDriverError:
                pass
        self.s.send_keys(el, self.resolve(s["text"]))

    def _clear(self, s, tmo, start_url):
        self.s.clear(self._find(s["target"], tmo))

    def _swipe(self, s, tmo, start_url):
        w, h = self._viewport()
        d, cx, cy = s.get("distance", 0.6), w // 2, h // 2
        dx, dy = int(w * d / 2), int(h * d / 2)
        # "swipe up" moves the finger up, which scrolls the content down
        x1, y1, x2, y2 = {"up": (cx, cy + dy, cx, cy - dy), "down": (cx, cy - dy, cx, cy + dy),
                          "left": (cx + dx, cy, cx - dx, cy), "right": (cx - dx, cy, cx + dx, cy)}[s["direction"]]
        self.s.swipe(x1, y1, x2, y2)

    def _scroll_to(self, s, tmo, start_url):
        st, v = self._loc(s["target"])
        for i in range(s.get("max_swipes", 8) + 1):
            try:
                el = self.s.find(st, v)
                if self.s.displayed(el):
                    return f"found after {i} swipe(s)"
            except NoSuchElement:
                pass
            if i < s.get("max_swipes", 8):
                self._swipe({"direction": "up" if s.get("direction", "down") == "down" else "down", "distance": 0.5}, tmo, start_url)
                time.sleep(0.4)
        raise StepError(f"{s['target']} not found after {s.get('max_swipes', 8)} swipes")

    def _back(self, s, tmo, start_url):
        self.s.back()

    def _hide_keyboard(self, s, tmo, start_url):
        self.s.hide_keyboard()

    def _visible(self, target: str) -> bool:
        st, v = self._loc(target)
        try:
            return any(self.s.displayed(e) for e in self.s.find_all(st, v)[:5])
        except NoSuchElement:
            return False

    def _wait_hidden(self, target: str, tmo: float) -> None:
        end = time.monotonic() + tmo
        while self._visible(target):
            if time.monotonic() > end:
                raise StepError(f"{target} is still visible after {tmo:.0f}s")
            time.sleep(0.5)

    def _wait_for(self, s, tmo, start_url):
        if s.get("state", "visible") == "hidden":
            self._wait_hidden(s["target"], tmo)
        else:
            self._find(s["target"], tmo)

    def _expect_visible(self, s, tmo, start_url):
        try:
            self._find(s["target"], tmo)
        except NoSuchElement:
            raise StepError(f"expected {s['target']} to be visible")

    def _expect_hidden(self, s, tmo, start_url):
        self._wait_hidden(s["target"], tmo)

    def _expect_text(self, s, tmo, start_url):
        el = self._find(s["target"], tmo)
        end, got = time.monotonic() + tmo, ""
        while True:
            got = (self.s.text(el) or "").strip()
            if s.get("match", "contains") == "equals" and got == s["text"] or s.get("match", "contains") != "equals" and s["text"] in got:
                return
            if time.monotonic() > end:
                raise StepError(f"expected {s['target']} to {s.get('match', 'contains')} \"{s['text']}\" but it shows \"{_short(got, 200)}\"")
            time.sleep(0.5)

    # observation
    def screenshot(self) -> tuple[bytes, str]:
        return compress(self.s.screenshot())

    def observe(self, with_elements: bool = True) -> Observation:
        img, mime = self.screenshot()
        w, h = self._viewport()
        els, url = [], ""
        if with_elements:
            if self.web:
                try:
                    v = self.s.execute("return " + WEB_ELEMENTS_JS.strip())
                    els, url = web_elements(v.get("items") or []), v.get("url", "")
                except Exception:
                    els = []
            else:
                try:
                    els = native_elements(self.s.source(), w, h)
                except WebDriverError:
                    els = []
        return Observation(img, mime, w, h, els, url)

    def close(self, passed: bool | None = None, reason: str = "") -> None:
        if passed is not None and self.provider is not None:
            try:
                self.provider.mark(self.s, passed, reason)
            except Exception:
                pass
        self.s.quit()


# --------------------------------------------------------------------------- built-in phone emulation (mobile web)
class EmulatorDriver(Driver):
    web = True

    def __init__(self, *, device: str, policy, ports: set[int], secrets: dict | None = None, on_block=None,
                 video_dir: str | None = None, check_url=None):
        from playwright.sync_api import sync_playwright

        from .egress_proxy import EgressProxy

        super().__init__(secrets)
        self.check_url = check_url
        self.proxy = EgressProxy(policy, ports, on_block=on_block)
        pport = self.proxy.start()
        self._pw = sync_playwright().start()
        try:
            desc = self._pw.devices.get(device) or self._pw.devices["Pixel 7"]
            self.browser = self._pw.chromium.launch(
                headless=True, proxy={"server": f"http://127.0.0.1:{pport}"},
                args=["--proxy-bypass-list=<-loopback>", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                      "--webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-extensions",
                      "--disable-background-networking", "--no-first-run", "--disable-sync", "--disable-default-apps"],
                handle_sigint=False, handle_sigterm=False, handle_sighup=False)
            opts: dict[str, Any] = dict(desc, service_workers="block", accept_downloads=False)
            opts.pop("default_browser_type", None)
            if video_dir:
                opts.update(record_video_dir=video_dir, record_video_size=desc.get("viewport"))
            self.ctx = self.browser.new_context(**opts)
            self.ctx.set_default_timeout(self.timeout_s * 1000)
            self.page = self.ctx.new_page()
        except Exception:
            self._shutdown()
            raise
        self.session_id = ""

    def _pw_selector(self, target: str) -> str:
        t = target.strip()
        if t.startswith("~"):
            return f"[aria-label={ms._cssq(t[1:])}]"
        k, _, v = t.partition("=")
        return {"css": v, "class": v, "xpath": f"xpath={v}", "id": f"[id={ms._cssq(v)}]",
                "text": f"text={ms._cssq(v)}", "contains": f"text={v}"}[k]

    def _loc(self, target: str):
        return self.page.locator(self._pw_selector(target)).first

    def perform(self, s: dict, start_url: str = "") -> str:
        from playwright.sync_api import Error as PWError

        try:
            return super().perform(s, start_url)
        except PWError as e:
            raise StepError(str(e).split("\n")[0][:800])

    def _launch_app(self, s, tmo, start_url):
        if start_url:
            self._open_url({"url": start_url}, tmo, start_url)
        return "browser ready"

    def _open_url(self, s, tmo, start_url):
        from urllib.parse import urljoin
        url = urljoin(start_url, s["url"]) if s["url"].startswith("/") and start_url else s["url"]
        if self.check_url:
            self.check_url(url)
        self.page.goto(url, timeout=max(tmo, 30) * 1000, wait_until="load")

    def _tap(self, s, tmo, start_url):
        self._loc(s["target"]).tap(timeout=tmo * 1000)

    def _tap_at(self, s, tmo, start_url):
        vp = self.page.viewport_size or {"width": 390, "height": 844}
        self.page.touchscreen.tap(s["x"] * vp["width"], s["y"] * vp["height"])

    def _type(self, s, tmo, start_url):
        loc = self._loc(s["target"])
        val = self.resolve(s["text"])
        if s.get("clear_first", True):
            loc.fill(val, timeout=tmo * 1000)
        else:
            loc.press_sequentially(val, timeout=tmo * 1000)

    def _clear(self, s, tmo, start_url):
        self._loc(s["target"]).fill("", timeout=tmo * 1000)

    def _swipe(self, s, tmo, start_url):
        vp = self.page.viewport_size or {"width": 390, "height": 844}
        d = s.get("distance", 0.6)
        dx, dy = {"up": (0, vp["height"] * d), "down": (0, -vp["height"] * d), "left": (vp["width"] * d, 0),
                  "right": (-vp["width"] * d, 0)}[s["direction"]]
        self.page.mouse.move(vp["width"] / 2, vp["height"] / 2)
        self.page.mouse.wheel(dx, dy)
        self.page.wait_for_timeout(300)

    def _scroll_to(self, s, tmo, start_url):
        loc = self._loc(s["target"])
        for i in range(s.get("max_swipes", 8) + 1):
            if loc.count() and loc.is_visible():
                loc.scroll_into_view_if_needed(timeout=tmo * 1000)
                return f"found after {i} swipe(s)"
            self._swipe({"direction": "up" if s.get("direction", "down") == "down" else "down", "distance": 0.5}, tmo, start_url)
        raise StepError(f"{s['target']} not found after {s.get('max_swipes', 8)} swipes")

    def _back(self, s, tmo, start_url):
        self.page.go_back(timeout=tmo * 1000)

    def _hide_keyboard(self, s, tmo, start_url):
        self.page.evaluate("document.activeElement && document.activeElement.blur && document.activeElement.blur()")

    def _wait_for(self, s, tmo, start_url):
        self._loc(s["target"]).wait_for(state=s.get("state", "visible"), timeout=tmo * 1000)

    def _expect_visible(self, s, tmo, start_url):
        try:
            self._loc(s["target"]).wait_for(state="visible", timeout=tmo * 1000)
        except Exception:
            raise StepError(f"expected {s['target']} to be visible")

    def _expect_hidden(self, s, tmo, start_url):
        try:
            self._loc(s["target"]).wait_for(state="hidden", timeout=tmo * 1000)
        except Exception:
            raise StepError(f"{s['target']} is still visible after {tmo:.0f}s")

    def _expect_text(self, s, tmo, start_url):
        loc, end, got = self._loc(s["target"]), time.monotonic() + tmo, ""
        while True:
            try:
                got = (loc.inner_text(timeout=1000) or "").strip()
            except Exception:
                try:
                    got = loc.input_value(timeout=500)
                except Exception:
                    got = ""
            if s.get("match", "contains") == "equals" and got == s["text"] or s.get("match", "contains") != "equals" and s["text"] in got:
                return
            if time.monotonic() > end:
                raise StepError(f"expected {s['target']} to {s.get('match', 'contains')} \"{s['text']}\" but it shows \"{_short(got, 200)}\"")
            time.sleep(0.4)

    def screenshot(self) -> tuple[bytes, str]:
        return self.page.screenshot(type="jpeg", quality=70), "image/jpeg"

    def observe(self, with_elements: bool = True) -> Observation:
        img, mime = self.screenshot()
        vp = self.page.viewport_size or {"width": 0, "height": 0}
        els, url = [], self.page.url
        if with_elements:
            try:
                v = self.page.evaluate(WEB_ELEMENTS_JS)
                els = web_elements(v.get("items") or [])
            except Exception:
                els = []
        return Observation(img, mime, vp["width"], vp["height"], els, url)

    def video_path(self) -> str | None:
        try:
            v = self.page.video
            return v.path() if v else None
        except Exception:
            return None

    def _shutdown(self) -> None:
        for fn in (lambda: self.ctx.close(), lambda: self.browser.close(), lambda: self._pw.stop()):
            try:
                fn()
            except Exception:
                pass
        self.proxy.stop()

    def close(self, passed: bool | None = None, reason: str = "") -> None:
        self._shutdown()
