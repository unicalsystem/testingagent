# Plans, licences and payments (release 1.9)

TestAgent is sold as **prepaid licences**: an organization pays for 1, 3, 6 or 12 months of a plan. Razorpay takes INR payments (UPI, cards, net banking, wallets) and PayPal takes USD payments. A free trial plan (7 days by default) needs no payment.

## Customer journey

1. On the landing page (`/`), the customer sees **Plans & pricing** and chooses **Start free trial** or a plan.
2. **Self-service sign-up** creates the organization and the owner account immediately, then opens `/<org>/billing`:
   * If the customer picked a plan on the landing page, its checkout opens straight away.
   * Evidence storage on Unical-managed servers can be agreed during sign-up, so tests can run at once.
3. On **Plans & billing**, the owner or an administrator can:
   * start the trial (once per organization);
   * pay for a plan, optionally with a discount coupon and a GSTIN;
   * redeem a licence-extension coupon from Unical.

   Invoices are listed there, and the page shows usage against the plan limits.
4. Before the end of the licence, owners and administrators receive reminder e-mails, 7 days and 1 day before by default, plus one on expiry. SMTP must be configured for these.
5. After expiry the organization becomes **read-only**:
   * Users can still sign in, view and export data, pay, and redeem coupons.
   * Runs, AI requests, new cases and projects are refused with HTTP 402 and a clear message.
   * Monitors pause.
6. Paying again for the same plan adds the new period after the current end date. A different plan starts at once. Any paid time left on the old plan is converted into extra days at the plans' monthly list prices, so an upgrade adds fewer days and a downgrade adds more.

## What a plan controls

* **Feature modules:**
  * browser automation
  * AI features
  * API testing
  * API monitoring
  * mobile testing
  * connectors
  * Microsoft 365 SSO
  * own storage (SharePoint / Google Drive)
* **Limits:** each can be left empty (unlimited) or set to 0 (none):
  * team members (active users plus pending invitations)
  * projects
  * automated browser runs per month
  * API runs per month
  * API monitors
  * mobile runs per month
  * AI requests per month
* **Prices:** per currency (INR and USD) and per period (1, 3, 6 or 12 months). Prices exclude tax. GST is added to INR prices, 18% by default, and is configurable.

Changes to a plan's features and limits apply immediately to the organizations on it. Changes to prices apply to new purchases.

The Unical organization is always unlimited. Organizations that existed before 1.9 get an **unlimited (legacy)** licence until a Super Admin changes it.

## Super Admin: Platform → Plans & billing

* **Plans:** create, edit and retire plans: name, code, tagline, badge, order, visibility, trial length, prices, features, limits and extra lines.
  * **Auto** fills the 3-, 6- and 12-month prices from the monthly price, with 5, 10 and 20% discounts.
  * A plan that is in use is retired rather than deleted.
* **Coupons:** three kinds:
  * **Extend licence:** adds N days, optionally switching to a chosen plan, with no payment. Use it for pilots, delayed purchase orders or goodwill.
  * **% discount** and **fixed discount:** applied at checkout. They can be restricted to certain plans.

  Any coupon can also have an expiry date, a maximum number of uses, and a list of the organizations allowed to use it. Each organization can use a given coupon once, and the coupon's usage list shows who used it.
* **Payment gateways & settings:**
  * Turn enforcement and self-service sign-up on or off, and set the reminder days.
  * Razorpay: key ID, key secret and webhook secret.
  * PayPal: environment (sandbox or live), client ID and secret.
  * Seller name, address and GSTIN; tax label and rates; invoice number prefix.

  **Save & test** checks the gateway credentials.
* **Licences:** each organization's state and plan, end date and amount paid. **Change** grants a licence by hand, for example after a bank transfer or purchase order, makes an organization unlimited, or resets it.
* **Payments:** every payment with its gateway reference and invoice. **Mark paid** is for a payment that was confirmed in the gateway dashboard but whose confirmation never reached TestAgent.

## Setting up Razorpay (INR)

1. Razorpay Dashboard → **Account & Settings → API Keys**: generate keys. Use `rzp_test_…` while testing and `rzp_live_…` once your account is activated.
2. In **Platform → Plans & billing → Payment gateways**, enter the Key ID and Key secret, tick **Accept payments with Razorpay**, then choose **Save & test Razorpay**.
3. Razorpay Dashboard → **Webhooks → Add new webhook**:
   * URL: `https://testagent.coolaudit.com/api/public/billing/razorpay/webhook`
   * Secret: choose one, and enter the same value as **Webhook secret** in TestAgent.
   * Events: `payment.captured` and `order.paid`.

   The webhook confirms payments even if the customer closes the browser before returning.
4. Turn on automatic capture of payments in Razorpay (Payment Capture settings). TestAgent also captures any authorised payment of a matching order itself.

## Setting up PayPal (USD)

1. Go to developer.paypal.com → **Apps & Credentials** and create a REST app. Use the Sandbox app first, then the Live one.
2. Enter the client ID and secret, choose the environment, tick **Accept payments with PayPal**, then choose **Save & test PayPal**.
3. The customer is redirected to PayPal and returns to `/<org>/billing`, where the payment is captured and checked against the order amount before the licence is extended.

## Security

* Gateway secrets are Fernet-encrypted, are never returned by the API, and only a short hint is shown.
* Payment confirmation happens server-side only:
  * **Razorpay:** the checkout signature (`HMAC-SHA256(order_id|payment_id)`) is verified, then the payment is fetched from Razorpay and its order, amount, currency and captured status are checked.
  * **Razorpay webhooks:** the `X-Razorpay-Signature` HMAC of the body is verified.
  * **PayPal:** the order is captured and the capture amount, currency and payment reference are checked.
* Fulfilment is idempotent and row-locked. Double verification, repeated webhooks or a browser refresh never extends a licence twice.
* Only owners and administrators of an organization can pay or redeem coupons. Only Unical Super Admins can manage plans, coupons, gateways and licences.
* Card, UPI and PayPal details never reach TestAgent. The page Content-Security-Policy allows only Razorpay's checkout domains in addition to the existing ones.

## Database (migration 0011)

| Table | Contents |
|---|---|
| `billing_plans` | Plans. Four editable starter plans are seeded; their prices are placeholders. |
| `tenant_subscriptions` | Each organization's licence: plan, status, end date, trial used, billing details. |
| `billing_coupons`, `billing_coupon_redemptions` | Coupons, and who used them. |
| `billing_payments` | Checkouts and payments, with amounts in minor units, gateway references, invoice numbers and the period bought. |
| `billing_usage` | Monthly AI-request counters. Other usage is counted from existing tables. |
