# Mobile app testing (release 1.8)

TestAgent tests **native and hybrid apps** (Android APK/AAB, iOS IPA) and **mobile web** on real devices or on built-in phone emulation. Each case can run on many devices at once.

## 1. Device providers (project → Mobile → Devices & apps)

| Provider | What it gives you | What you enter |
|---|---|---|
| BrowserStack | Real iOS / Android devices (App Automate) | Username, access key |
| LambdaTest | Real devices | Username, access key |
| Sauce Labs | Real devices (us-west-1, eu-central-1, us-east-4) | Username, access key, data center |
| Own Appium server | Your Appium 2 server or device lab | HTTPS URL (public address), optional user / password |
| Unical device lab | Physical devices hosted by Unical | Nothing (operator sets `MOBILE_LAB_URL`, `_USER`, `_PASSWORD`) |
| Built-in phone emulation | TestAgent's Chromium with phone and tablet emulation; mobile web only | Nothing |

Notes:

* Access keys are stored encrypted and are never shown again.
* **Test** checks the credentials.
* Device minutes are billed by the provider on the customer's own account.

## 2. App builds

You can add a build in three ways:

* Upload an `.apk`, `.aab`, `.ipa` or zipped `.app` (up to `MOBILE_MAX_APP_MB`, default 400 MB).
* Give an HTTPS download URL, for example from CI.
* Name the package / bundle id of an app that is already installed on the devices.

How each provider gets the build:

* **Device clouds:** TestAgent uploads the build on its first use and reuses that upload for 25 days, or until the build changes.
* **Own Appium server and the Unical lab:** they receive a signed TestAgent download link that is valid for 3 hours.

iOS real devices need a build signed for testing (ad hoc or development).

## 3. Creating cases

* **Step editor:** a declarative step list. The available steps are launch app, open URL, tap, tap at point, type, clear, swipe, scroll to, back, hide keyboard, wait for, expect visible / hidden / text, screenshot and wait. Targets use these forms:
  * `~accessibility id`
  * `id=resource-id`
  * `text=Exact text`
  * `contains=Part`
  * `xpath=…`
  * `class=…`
  * `css=…` (web)

  Steps cannot run scripts or shell commands.
* **Record on a device:** opens a live device in the browser.
  * Click the screenshot to tap, type or add a check. TestAgent picks the most stable selector for the element: accessibility id, then resource id, then text, then position.
  * Swipe, back and wait are buttons.
  * Only actions that worked are recorded. Save the result as a case.
* **Generate with AI:** describe the goal, or pick a manual test case.
  * Claude operates the live device. Each turn it sees the screen and a numbered element list, chooses one action, and keeps the steps that worked, including checks for the expected result.
  * It types passwords only as `{{secret:NAME}}` and never sees their values.
  * The limits are 30 actions per session and 4 failures in a row.

Project secrets can be typed only in a "Type text" step, as `{{secret:NAME}}`. They are kept out of results, logs and reports.

## 4. Running across devices

1. Choose cases, a provider and up to 20 devices.
2. Each case runs on every compatible device:
   * Android cases on Android devices
   * iOS cases on iOS devices
   * mobile web on any device

   Incompatible combinations are skipped and listed.
3. Runs execute in parallel (`MOBILE_PARALLEL_RUNS`, default 3). The built-in emulation runs one at a time.
4. You get:
   * A matrix of cases × devices.
   * A screenshot of every step, or of failures only when `SCREENSHOT_MODE=on_failure`.
   * The provider's session link with video and logs. Cloud sessions are marked passed or failed at the provider.
   * A downloadable HTML report.

## 5. Security

* Mobile web cases and recorder sessions may open only the project's authorised hosts:
  * The built-in emulation sends all of its traffic through the per-run egress proxy, like browser runs.
  * On real devices, the start URL and every "Open URL" step are checked. The device's own browser runs in the provider's network, not in TestAgent's.
* Own Appium URLs must be HTTPS on a public address and are re-checked before every session.
* Live device sessions are closed after `MOBILE_SESSION_IDLE_MINUTES` without activity (default 10), and after at most 60 minutes. An organization can have at most 2 live sessions at a time.

## 6. Architecture

* The **worker** holds every device: runs, recorder and AI sessions. It uses W3C WebDriver / Appium for real devices and Playwright for emulation.
* The **API** sends live-session commands through the database, one at a time, and waits for the answer.
* The **AI agent** runs in the API container, the only one with the Claude key.
* Migration `0010` adds six tables.
