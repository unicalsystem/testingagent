# TestAgent 1.9 — Plans, licences and online payments

* **Plans & pricing on the landing page:**
  * free trial plus Starter, Professional and Business plans, all editable
  * monthly, quarterly, half-yearly and annual periods, in INR and USD
* **Self-service sign-up:** the organization and its owner are created at once, then the owner lands on **Plans & billing**. The previous approval flow can be switched back on.
* **Free trial:** 7 days by default, once per organization. Afterwards the organization becomes **read-only** until it pays or redeems a coupon.
* **Payments:**
  * Razorpay in INR (UPI, cards, net banking, wallets), with a signed webhook
  * PayPal in USD
  * GST added to INR prices, GST-ready invoices, payment history
* **Coupons:** licence extensions (free days, optionally on a chosen plan) and % or fixed discounts. Each can be limited by expiry, number of uses, plans or organizations.
* **What plans control:** feature modules and limits (users, projects, browser, API and mobile runs per month, API monitors, AI requests per month), with usage meters and clear upgrade messages.
* **Platform → Plans & billing** for Unical Super Admins: plans, coupons, gateways and tax, licences (manual grants for offline payments), payments.
* **Reminder e-mails** before expiry and on expiry.
* **Upgrade:** migration `0011` (additive). Existing organizations get an unlimited licence; the Unical organization is always unlimited. The web Content-Security-Policy now allows Razorpay Checkout.

See docs/BILLING.md.
