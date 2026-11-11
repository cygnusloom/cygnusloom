# Credit payments plan

Status: planned, not implemented. Moved out of `ISSUE_TRACKER.md`; this is the working design and
implementation plan for selling credits.

## Current state

Cygnus Loom already has a credit system: `User.credit_balance`, a flat
`LlmModel.credits_per_turn` price per completed AI GM reply, and the append-only `CreditEntry`
ledger. The campaign owner pays, administrators and the desktop app's local accounts are exempt, and an
administrator grants credits manually. Payment is not integrated (`README.md`,
`src/cygnusloom/services/credits.py`).

## Goals

- Sell prepaid credit packs through Stripe Checkout.
- Offer optional auto top-up with a saved card.
- Configure the provider, credentials, packs, terms, and auto top-up in the admin panel.
- Keep the provider seam open so another provider can be added later.
- Claw back refunds and chargebacks automatically.
- Produce an archived chargeback evidence pack per payment.

## Non-goals

- Subscriptions.
- Credit expiry.
- Multiple simultaneously active providers.
- Gift credits or campaign credit pools.
- In-app refunds; refunds are issued in the provider dashboard.

## Decisions

- One configured provider per deployment, chosen on `/admin/payments`. The provider,
  credentials, currency, packs, and auto top-up settings are stored in the database; no new
  environment variables. Bootstrap settings stay in the environment: `CYGNUSLOOM_SECRET_KEY`,
  `CYGNUSLOOM_DATABASE_URL`, `CYGNUSLOOM_BASE_URL`, and the OIDC/single-player settings.
- The first and only adapter is Stripe. The protocol keeps other providers possible later; do
  not add a plugin loader.
- Sell prepaid credit packs and optional auto top-up. No subscriptions.
- One currency on the provider config (lower-case ISO 4217); every pack inherits it.
- Checkout return and webhook URLs use the existing `CYGNUSLOOM_BASE_URL`; the admin page shows
  the full webhook URL to register with the provider.
- Refunds and chargebacks are issued in the provider's dashboard and clawed back automatically,
  even when the balance goes negative; a negative balance already blocks new turns. Partial
  refunds claw back a prorated share, multiple refunds deduct only the delta, and a dispute that
  closes as won restores the disputed credits. A payment that was never granted (for example
  after an amount mismatch) is never clawed back. There is no manual refund review.
- A chargeback evidence pack is generated per payment: account identity, checkout evidence,
  terms acceptance, delivery, and every charged turn after the purchase. The pack is archived so
  later account or campaign deletion cannot erase the evidence.
- Purchased credits never expire.
- Exempt accounts (administrators, the desktop app's local account) see no purchase or auto top-up controls.
- Payments are optional. With no provider configured, `/credits` keeps today's "ask an
  administrator" message and the admin page explains how to set payments up.

## Provider seam

`src/cygnusloom/payments/` contains:

- `base.py`, defining `PaymentProvider` with `name`, `label`, `fields` (form schema:
  key/label/secret/required/help), `create_checkout`, `parse_webhook`, `fetch`, and `check`.
- `stripe.py`, the only adapter.
- `fake.py`, for tests and dev.
- `registry.py`, mapping a name to an adapter.

An adapter only translates between app records and the provider API; it never decides credits or
touches balances. Packs use inline prices (Stripe `price_data`), so the provider needs no
pre-created products.

## Data model

New migration after 0008.

- `payment_config` (single row): `provider`, `enabled`, `currency`, `terms_url`,
  `refund_policy_url`, `config` (Fernet-encrypted JSON holding only the adapter's declared
  fields), `updated_at`, `updated_by`. Empty `provider` means payments are off. Secrets use a
  domain-separated key like `llm/catalog.py` (`cygnusloom-payment:<secret>`) and are never
  rendered back; the form shows set/not-set and a clear checkbox.
- `credit_pack`: `label`, `credits`, `price_minor`, `enabled`, `sort`. Validation: at least 1
  credit, positive price, whole minor units.
- `payment`: `user_id`, `provider`, `pack_id`, `credits`, `amount_minor`, `currency`,
  `checkout_id`, `external_id` (the provider payment/charge id used by later events),
  `receipt_url`, `customer_ip`, `customer_user_agent`, `session_id`, `terms_version`,
  `terms_accepted_at`, `granted_credits`, `refunded_minor`, `clawed_back_credits`, `kind`
  (`checkout`/`auto_topup`), `status`
  (`pending`/`paid`/`needs_action`/`failed`/`expired`/`refunded`/`disputed`/`needs_review`),
  `note`, timestamps. Unique `(provider, external_id)`.
- `payment_event`: `provider`, `event_id`, `payment_id`, `kind`, `received_at`; unique
  `(provider, event_id)` for webhook idempotency.
- `payment_customer`: `user_id`, `provider`, `customer_id`, `payment_method`, `auto_topup`,
  `pack_id`, `threshold`, `consent_at`, `terms_version`, `setup_session_id`,
  `consecutive_failures`, `last_error`, timestamps; unique `(user_id, provider)`.
- `payment_evidence`: `payment_id` (unique), `created_at`, `created_by`, `content` (JSON snapshot
  of a generated chargeback pack, including reply excerpts or hashes).
- `credit_entry` gains nullable indexed `payment_id` and `payment_event_id`; reasons `purchase`,
  `purchase-refund`, and `dispute-return`.
- Add `grant_credits()` beside `charge_reply()` using an atomic
  `credit_balance = credit_balance + amount` plus a ledger row. Do not reuse `set_balance()` for
  purchases; it writes an absolute value and loses concurrent grants.

## Buying a pack

1. `/credits` shows the balance, recent ledger entries, and enabled packs.
2. Checkout requires accepting the terms and refund policy. `POST /credits/checkout` creates a
   `pending` payment and snapshots the terms version, acceptance time, client IP, user agent, and
   login session id, then the adapter opens a Stripe Checkout Session (`mode=payment`, inline
   price, `client_reference_id` and PaymentIntent metadata = payment id,
   `Idempotency-Key: payment-<id>`), then redirects.
3. `POST /payments/webhook/{provider}` is public (no login or CSRF), verifies the raw-body
   signature, and is the only place that grants credits. In one transaction it inserts
   `payment_event` (duplicate → reply 200 and stop), re-checks the reported amount, currency,
   and payment id against the stored payment (mismatch → `needs_review`, grant nothing), sets
   `paid`, calls `grant_credits()`, and stores the provider receipt URL. Unknown events get 200.
4. The return URL only shows status; it never grants.
5. Pending payments expire after 24 hours. A "re-check with provider" action uses `fetch`.
6. Refunds and disputes claw back automatically in the webhook transaction. `charge.refunded`
   deducts the prorated credits for the cumulative refunded amount (only the delta since the last
   event). A dispute deducts credits prorated by the disputed amount, capped at the credits not
   already clawed back; a dispute closed as won restores exactly what that dispute deducted. Each
   clawback is a `purchase-refund` ledger entry linked to the payment and provider event, may
   take the balance negative, and runs only up to `granted_credits`. A refund of a payment that
   was never granted changes no balance, and a refund for a deleted account is recorded without
   a balance change.

## Auto top-up

- The player picks a pack and a credit threshold, then completes a Setup-mode Checkout to save a
  card; the webhook stores the provider customer and payment method, plus the consent time,
  terms version, and setup session id for chargeback evidence.
- At the start of a turn, when the owner's balance is below the threshold, the app attempts one
  off-session charge for the chosen pack (the idempotency key includes the account and pack).
  Success grants credits and the turn continues; `requires_action` stores a completion link on
  `/credits`; failure still allows the turn when the remaining balance covers the price,
  otherwise the normal shortfall refusal applies.
- At most one auto top-up in flight per account (a `pending` auto-top-up payment is the lock).
  Three consecutive failures disable auto top-up and show the reason on `/credits`.
- Changing the provider disables all enrollments for the old provider (they must re-save a card).
  Auto top-up purchases appear in the admin payment list like any other purchase.

## Admin page

`/admin/payments`, with a new nav entry.

- Provider: choose from registered adapters, dynamic fields from `fields`, enable toggle,
  currency, terms and refund-policy URLs (required before payments can be enabled), test
  connection (`check()`), and the webhook URL with a copy button. Show a "test mode" badge for
  test keys. Changing provider or clearing credentials is blocked while a payment is pending.
- Packs: add/edit/enable/delete with a "credits for price" preview.
- Payments: search/filter by status, account, pack, amount, and external id; actions are re-check
  with the provider, expire a pending payment, and generate the chargeback evidence pack. Refunds
  and disputes need no admin action; `needs_review` only marks a webhook whose amount, currency,
  or payment id did not match the stored payment.
- Every mutation requires recent auth and CSRF and writes an `AdminAudit` entry with secrets
  redacted.

## Chargeback evidence pack

- Generated per payment from `Payment`, `PaymentEvent`, `CreditEntry`, `LlmUsage`, `Event`, and
  `Campaign`. It contains the account identity (issuer/subject and verified email at purchase),
  checkout IP/user agent/session, terms version and acceptance time, the receipt link, and the
  purchase's pack, credits, amount, currency, and provider references.
- A balance timeline shows the purchase grant, admin grants, every turn charge, and refunds or
  clawbacks. A usage table lists each charged turn after the purchase with time, campaign,
  player, model, credits, token usage, and a short reply excerpt or hash.
- Provider details (billing name/address, card brand/last4, 3DS result, Radar risk) are fetched
  live through `fetch` and included when available; a failed fetch is noted in the pack, not
  fatal.
- Generating a pack archives an immutable JSON snapshot in `payment_evidence`; the admin view
  renders it and links the receipt. Generation is audited.
- This proves delivery and use for "product not received"; it cannot prove cardholder
  authorization for a fraudulent dispute, which rests on 3DS/Radar and the card network.
- `Payment`, `CreditEntry`, `PaymentEvent`, and `PaymentEvidence` keep snapshot ids and must
  survive account and campaign deletion; archived reply excerpts preserve usage evidence.

## Testing

- `tests/test_payments.py`: disabled state; config CRUD, encryption at rest, secret masking; pack
  validation; checkout creation; webhook grants exactly once on replay; bad signature;
  amount/currency mismatch; unknown event; full and partial refund clawback with cumulative
  refunds, dispute created/won/lost, and a refund of an ungranted payment; the pending
  provider-switch block; auto top-up success, `requires_action`, and the failure counter; exempt
  accounts see no controls; checkout evidence snapshots; the evidence pack's contents and totals;
  an archived pack that survives campaign and account deletion; a failed provider fetch still
  producing the app-side pack; auto top-up consent appearing in the pack.
- The fake adapter's checkout URL is an internal pay/cancel page available only when the fake
  adapter is registered (tests/dev), so the live UI test can exercise the whole flow without
  `stripe`.
- Update `tests/test_credits.py` expectations where the credits page or help text changes.

## Docs and cleanup

- Update the README credits section (currently says payment is not integrated), the `/credits`
  page copy, and the admin help text, including the evidence pack and the privacy note for
  stored checkout IP/user agent and terms acceptance.
- After the migration, delete the local `cygnusloom.db` for a fresh manual-test state.

## Suggested implementation order

1. Schema and ledger: tables, migration, `grant_credits()`, admin config and packs.
2. Checkout and webhook: credits page packs, checkout route, webhook grant, refunds and disputes.
3. Auto top-up: Setup-mode Checkout, off-session charges, failure handling.
4. Evidence: pack generation, archive, and admin view.
5. Docs, tests, and a live UI run.
