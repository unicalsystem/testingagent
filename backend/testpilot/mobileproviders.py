"""Device providers for mobile testing (release 1.8).

  browserstack  BrowserStack App Automate / Automate (real iOS & Android devices)
  lambdatest    LambdaTest real-device cloud
  saucelabs     Sauce Labs real devices (region us-west-1 / eu-central-1 / us-east-4)
  appium        the customer's own Appium 2 server or device lab (HTTPS, public address)
  unical_lab    a device host operated by Unical (hub configured on the server: MOBILE_LAB_URL / _USER / _PASSWORD)
  emulator      built-in phone emulation in TestAgent's own Chromium (mobile web only; no account needed)

All real-device providers speak the W3C WebDriver / Appium protocol; they differ in hub URL, capability options,
app upload API, session links and how a session is marked passed / failed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import quote

import httpx

from .config import get_settings
from .security import decrypt_secret, encrypt_secret

KINDS = ("browserstack", "lambdatest", "saucelabs", "appium", "unical_lab", "emulator")
LABEL = {"browserstack": "BrowserStack", "lambdatest": "LambdaTest", "saucelabs": "Sauce Labs", "appium": "Own Appium server",
         "unical_lab": "Unical device lab", "emulator": "Built-in phone emulation (mobile web)"}
SAUCE_REGIONS = ("us-west-1", "eu-central-1", "us-east-4")
EMULATED_DEVICES = ["iPhone 15 Pro", "iPhone 15", "iPhone 13", "iPhone SE", "Pixel 7", "Pixel 5", "Galaxy S9+",
                    "Galaxy Tab S4", "iPad Pro 11", "iPad Mini"]
# Tests point providers at a local mock; production never sets this.
TEST_HUB_OVERRIDE: dict[str, str] = {}


class ProviderError(Exception):
    pass


def lab_settings() -> dict:
    return {"url": os.environ.get("MOBILE_LAB_URL", "").rstrip("/"), "user": os.environ.get("MOBILE_LAB_USER", ""),
            "password": os.environ.get("MOBILE_LAB_PASSWORD", "")}


def lab_available() -> bool:
    return bool(lab_settings()["url"])


# --------------------------------------------------------------------------- config
def clean_config(kind: str, c: dict) -> dict:
    from .integrations import ConnectorError, check_endpoint

    c = c or {}
    if kind not in KINDS:
        raise ProviderError(f"unknown provider '{kind}'")
    if kind in ("browserstack", "lambdatest", "saucelabs"):
        user = str(c.get("username") or "").strip()[:120]
        if not user:
            raise ProviderError("the account username is required")
        out = {"username": user}
        if kind == "saucelabs":
            region = c.get("region") or "us-west-1"
            if region not in SAUCE_REGIONS:
                raise ProviderError("Sauce Labs region must be us-west-1, eu-central-1 or us-east-4")
            out["region"] = region
        return out
    if kind == "appium":
        hub = str(c.get("hub_url") or "").strip().rstrip("/")
        try:
            check_endpoint(hub)
        except ConnectorError as e:
            raise ProviderError(f"Appium URL: {e}")
        return {"hub_url": hub, "username": str(c.get("username") or "").strip()[:120]}
    if kind == "unical_lab" and not lab_available():
        raise ProviderError("The Unical device lab is not configured on this server yet")
    return {}


def set_secret(row, secret: str, keep: bool = True) -> None:
    if row.kind in ("emulator", "unical_lab"):
        row.secret_enc, row.secret_hint = "", ""
        return
    s = (secret or "").strip()
    if not s and keep and row.secret_enc:
        return
    if not s and row.kind != "appium":
        raise ProviderError("the access key is required")
    row.secret_enc = encrypt_secret(s) if s else ""
    row.secret_hint = f"…{s[-4:]}" if len(s) >= 10 else ("set" if s else "")


# --------------------------------------------------------------------------- signed app downloads (own Appium / lab)
def _sign_key() -> bytes:
    return hashlib.sha256(b"testagent-app-download:" + get_settings().credential_encryption_key.get_secret_value().encode()).digest()


def app_download_url(app_id: int, filename: str, ttl_s: int = 3 * 3600) -> str:
    exp = int(time.time()) + ttl_s
    sig = hmac.new(_sign_key(), f"{app_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{get_settings().public_url}/api/public/mobile-apps/{app_id}/{exp}/{sig}/{quote(filename or 'app')}"


def check_download(app_id: int, exp: int, sig: str) -> bool:
    good = hmac.new(_sign_key(), f"{app_id}:{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return exp >= time.time() and hmac.compare_digest(good, sig)


# --------------------------------------------------------------------------- providers
class Provider:
    native = True
    web = True

    def __init__(self, row):
        self.row = row
        self.cfg = (row.config or {}) if row else {}
        self.key = decrypt_secret(row.secret_enc) if row is not None and row.secret_enc else ""

    @property
    def kind(self) -> str:
        return self.row.kind

    def hub(self) -> str:
        raise NotImplementedError

    def auth(self) -> tuple[str, str] | None:
        return (self.cfg.get("username", ""), self.key) if self.key else None

    def _http(self) -> httpx.Client:
        return httpx.Client(auth=self.auth(), timeout=120, trust_env=False, follow_redirects=False,
                            headers={"User-Agent": "TestAgent-Mobile/1.8"})

    def base_caps(self, device: dict, target: dict, app_ref: str | None) -> dict:
        platform = (device.get("platform") or target.get("platform") or "android").lower()
        caps: dict[str, Any] = {"platformName": "iOS" if platform == "ios" else "Android",
                                "appium:automationName": "XCUITest" if platform == "ios" else "UiAutomator2",
                                "appium:deviceName": device.get("name") or ("iPhone 15" if platform == "ios" else "Android"),
                                "appium:newCommandTimeout": 300}
        if device.get("os_version"):
            caps["appium:platformVersion"] = str(device["os_version"])
        if target.get("kind") == "web":
            caps["browserName"] = "Safari" if platform == "ios" else "Chrome"
        else:
            if app_ref:
                caps["appium:app"] = app_ref
            elif target.get("app_package"):
                if platform == "ios":
                    caps["appium:bundleId"] = target["app_package"]
                else:
                    caps["appium:appPackage"] = target["app_package"]
                    if target.get("app_activity"):
                        caps["appium:appActivity"] = target["app_activity"]
            if platform == "android":
                caps["appium:autoGrantPermissions"] = True
        return caps

    def caps(self, device: dict, target: dict, app_ref: str | None, names: dict) -> dict:
        return self.base_caps(device, target, app_ref)

    def upload(self, path: str | None, url: str | None, name: str) -> str:
        raise ProviderError(f"{LABEL[self.kind]} does not accept app uploads; use an app download URL")

    def session_url(self, session_id: str, web: bool) -> str:
        return ""

    def mark(self, session, passed: bool, reason: str) -> None:
        pass

    def devices(self) -> list[dict]:
        return []

    def test(self) -> str:
        raise NotImplementedError


class BrowserStack(Provider):
    API = "https://api-cloud.browserstack.com"

    def hub(self):
        return TEST_HUB_OVERRIDE.get("browserstack") or "https://hub-cloud.browserstack.com/wd/hub"

    def api(self, path: str) -> str:
        return TEST_HUB_OVERRIDE.get("browserstack_api", self.API) + path

    def caps(self, device, target, app_ref, names):
        c = self.base_caps(device, target, app_ref)
        c["bstack:options"] = {"userName": self.cfg.get("username"), "accessKey": self.key, "projectName": names["project"][:255],
                               "buildName": names["build"][:255], "sessionName": names["session"][:255], "video": True,
                               "deviceLogs": True, "idleTimeout": 300, "realMobile": "true",
                               "deviceName": c["appium:deviceName"], **({"osVersion": c["appium:platformVersion"]} if c.get("appium:platformVersion") else {})}
        return c

    def upload(self, path, url, name):
        with self._http() as h:
            try:
                if path:
                    with open(path, "rb") as f:
                        r = h.post(self.api("/app-automate/upload"), files={"file": (name, f)}, data={"custom_id": name[:100]}, timeout=900)
                else:
                    r = h.post(self.api("/app-automate/upload"), data={"url": url, "custom_id": name[:100]}, timeout=900)
            except httpx.HTTPError as e:
                raise ProviderError(f"BrowserStack upload failed: {type(e).__name__}")
        if r.status_code != 200 or "app_url" not in r.text:
            raise ProviderError(f"BrowserStack upload failed (HTTP {r.status_code}): {r.text[:300]}")
        return r.json()["app_url"]

    def session_url(self, session_id, web):
        path = f"/automate/sessions/{session_id}.json" if web else f"/app-automate/sessions/{session_id}.json"
        try:
            with self._http() as h:
                r = h.get(self.api(path).replace("api-cloud", "api") if web else self.api(path), timeout=20)
            s = r.json().get("automation_session", {})
            return s.get("public_url") or s.get("browser_url") or ""
        except Exception:
            return ""

    def mark(self, session, passed, reason):
        arg = json.dumps({"action": "setSessionStatus", "arguments": {"status": "passed" if passed else "failed", "reason": reason[:250]}})
        try:
            session.execute(f"browserstack_executor: {arg}")
        except Exception:
            pass

    def devices(self):
        with self._http() as h:
            r = h.get(self.api("/app-automate/devices.json"), timeout=30)
        if r.status_code != 200:
            raise ProviderError(f"BrowserStack returned HTTP {r.status_code}")
        return [{"name": d.get("device"), "os_version": d.get("os_version"), "platform": (d.get("os") or "").lower()}
                for d in r.json() if d.get("realMobile", True)][:500]

    def test(self):
        with self._http() as h:
            r = h.get(self.api("/app-automate/plan.json"), timeout=30)
        if r.status_code != 200:
            raise ProviderError(f"BrowserStack rejected the credentials (HTTP {r.status_code})")
        j = r.json()
        return f"Connected; parallel sessions allowed: {j.get('parallel_sessions_max_allowed', '?')}"


class LambdaTest(Provider):
    def hub(self):
        return TEST_HUB_OVERRIDE.get("lambdatest") or "https://mobile-hub.lambdatest.com/wd/hub"

    def api(self, base: str, path: str) -> str:
        return TEST_HUB_OVERRIDE.get("lambdatest_api", base) + path

    def caps(self, device, target, app_ref, names):
        c = self.base_caps(device, target, app_ref)
        c["LT:Options"] = {"username": self.cfg.get("username"), "accessKey": self.key, "isRealMobile": True,
                           "build": names["build"][:255], "name": names["session"][:255], "project": names["project"][:255],
                           "video": True, "devicelog": True, "w3c": True, "idleTimeout": 300,
                           "deviceName": c["appium:deviceName"], "platformName": c["platformName"],
                           **({"platformVersion": c["appium:platformVersion"]} if c.get("appium:platformVersion") else {})}
        return c

    def upload(self, path, url, name):
        with self._http() as h:
            try:
                u = self.api("https://manual-api.lambdatest.com", "/app/upload/realDevice")
                if path:
                    with open(path, "rb") as f:
                        r = h.post(u, files={"appFile": (name, f)}, data={"name": name[:100]}, timeout=900)
                else:
                    r = h.post(u, data={"url": url, "name": name[:100]}, timeout=900)
            except httpx.HTTPError as e:
                raise ProviderError(f"LambdaTest upload failed: {type(e).__name__}")
        if r.status_code != 200 or "app_url" not in r.text:
            raise ProviderError(f"LambdaTest upload failed (HTTP {r.status_code}): {r.text[:300]}")
        return r.json()["app_url"]

    def session_url(self, session_id, web):
        return f"https://appautomation.lambdatest.com/test?testID={quote(session_id)}"

    def mark(self, session, passed, reason):
        try:
            session.execute(f"lambda-status={'passed' if passed else 'failed'}")
        except Exception:
            pass

    def test(self):
        with self._http() as h:
            r = h.get(self.api("https://mobile-api.lambdatest.com", "/mobile-automation/api/v1/builds?limit=1"), timeout=30)
        if r.status_code != 200:
            raise ProviderError(f"LambdaTest rejected the credentials (HTTP {r.status_code})")
        return "Connected to LambdaTest"


class SauceLabs(Provider):
    def region(self) -> str:
        return self.cfg.get("region") or "us-west-1"

    def hub(self):
        return TEST_HUB_OVERRIDE.get("saucelabs") or f"https://ondemand.{self.region()}.saucelabs.com/wd/hub"

    def api(self, path: str) -> str:
        return TEST_HUB_OVERRIDE.get("saucelabs_api", f"https://api.{self.region()}.saucelabs.com") + path

    def caps(self, device, target, app_ref, names):
        c = self.base_caps(device, target, app_ref)
        c["sauce:options"] = {"username": self.cfg.get("username"), "accessKey": self.key, "build": names["build"][:255],
                              "name": names["session"][:255], "appiumVersion": "latest"}
        return c

    def upload(self, path, url, name):
        if not path:
            raise ProviderError("Sauce Labs needs the app file: upload the build instead of a URL")
        with self._http() as h, open(path, "rb") as f:
            try:
                r = h.post(self.api("/v1/storage/upload"), files={"payload": (name, f)}, data={"name": name[:100]}, timeout=900)
            except httpx.HTTPError as e:
                raise ProviderError(f"Sauce Labs upload failed: {type(e).__name__}")
        if r.status_code not in (200, 201):
            raise ProviderError(f"Sauce Labs upload failed (HTTP {r.status_code}): {r.text[:300]}")
        return "storage:" + r.json()["item"]["id"]

    def session_url(self, session_id, web):
        return f"https://app.saucelabs.com/tests/{quote(session_id)}"

    def mark(self, session, passed, reason):
        try:
            session.execute(f"sauce:job-result={'passed' if passed else 'failed'}")
        except Exception:
            pass

    def test(self):
        with self._http() as h:
            r = h.get(self.api("/v1/storage/files?per_page=1"), timeout=30)
        if r.status_code != 200:
            raise ProviderError(f"Sauce Labs rejected the credentials (HTTP {r.status_code})")
        return f"Connected to Sauce Labs ({self.region()})"


class OwnAppium(Provider):
    def hub(self):
        return self.cfg.get("hub_url", "")

    def test(self):
        with self._http() as h:
            try:
                r = h.get(f"{self.hub()}/status", timeout=20)
            except httpx.HTTPError as e:
                raise ProviderError(f"Appium server not reachable: {type(e).__name__}")
        if r.status_code != 200:
            raise ProviderError(f"Appium /status returned HTTP {r.status_code}")
        try:
            version = ((r.json().get("value") or {}).get("build") or {}).get("version") or ""
        except ValueError:
            version = ""
        return "Appium server ready" + (f" (version {version})" if version else "")


class UnicalLab(OwnAppium):
    def __init__(self, row):
        super().__init__(row)
        s = lab_settings()
        self.cfg = {"hub_url": s["url"], "username": s["user"]}
        self.key = s["password"]


class Emulator(Provider):
    native = False

    def hub(self):
        return ""

    def devices(self):
        return [{"name": d, "os_version": "", "platform": "ios" if d.startswith("i") else "android"} for d in EMULATED_DEVICES]

    def test(self):
        return "Built in: no account needed"


CLASSES = {"browserstack": BrowserStack, "lambdatest": LambdaTest, "saucelabs": SauceLabs, "appium": OwnAppium,
           "unical_lab": UnicalLab, "emulator": Emulator}


def provider(row) -> Provider:
    return CLASSES[row.kind](row)
