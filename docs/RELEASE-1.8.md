# TestAgent 1.8 — Mobile app testing

* **Mobile tab in every project:** native and hybrid apps (APK / AAB / IPA) and mobile web.
* **Device providers:**
  * BrowserStack, LambdaTest and Sauce Labs real-device clouds
  * your own Appium 2 server
  * the Unical device lab (once configured)
  * built-in phone and tablet emulation for mobile web
* **App builds:** upload (up to 400 MB), CI download URL, or an app already installed on the devices. Uploads to clouds are cached.
* **Three ways to write mobile tests:**
  * step editor
  * recording on a live device
  * the AI agent, which drives a device toward a plain-English goal or a manual test case and writes the steps
* **Cross-device runs:** cases × devices in parallel, with a pass/fail matrix, step screenshots, provider video links and an HTML report.
* **Security:** credentials encrypted; secrets only as `{{secret:NAME}}`; mobile web is limited to authorised hosts; no script steps.
* **Upgrade:**
  * Migration `0010` adds six tables (nothing existing changes).
  * New dependency: Pillow (screenshot compression).
  * nginx allows large bodies on the app-upload endpoint only.

See docs/MOBILE-TESTING.md.
