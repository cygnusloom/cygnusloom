This file contains issues encountered during play or feature requests. Triage them and use the questions tool to get details or clarification before moving them from Untriaged to Triaged. Combine duplicate or similar items if they can be implemented together.

Tackle triaged issues in the order presented (untraiged issues have no particular order). Ask more questions if any further details are needed (update the entry here). Once the item is done, remove it from this file.

# Untriaged Issues

None.

# Triaged Issues

Order below is the work order. Each entry lists the decisions made in triage.

1. Credit payments: prepaid packs and auto top-up through a pluggable provider configured in the
   admin panel.

   **Decisions**
   - One configured provider per deployment, chosen on `/admin/payments`. The provider,
     credentials, currency, packs, and auto top-up settings are stored in the database; no new
     environment variables. Bootstrap settings stay in the environment: `CYGNUSLOOM_SECRET_KEY`,
     `CYGNUSLOOM_DATABASE_URL`, `CYGNUSLOOM_BASE_URL`, and the OIDC/single-player settings.
   - The first and only adapter is Stripe. The protocol keeps other providers possible later; do
     not add a plugin loader.
   - Sell prepaid credit packs and optional auto top-up. No subscriptions.
   - One currency on the provider config (lower-case ISO 4217); every pack inherits it.
   - Checkout return and webhook URLs use the existing `CYGNUSLOOM_BASE_URL`; the admin page
     shows the full webhook URL to register with the provider.
   - Refunds and chargebacks are issued in the provider's dashboard and clawed back
     automatically, even when the balance goes negative; a negative balance already blocks new
     turns. Partial refunds claw back a prorated share, multiple refunds deduct only the delta,
     and a dispute that closes as won restores the disputed credits. A payment that was never
     granted (for example after an amount mismatch) is never clawed back. There is no manual
     refund review.
   - Purchased credits never expire.
   - Exempt accounts (administrators, single-player) see no purchase or auto top-up controls.
   - Payments are optional. With no provider configured, `/credits` keeps today's "ask an
     administrator" message and the admin page explains how to set payments up.

   **Provider seam** (`src/cygnusloom/payments/`)

   `base.py` defines `PaymentProvider` with `name`, `label`, `fields` (form schema:
   key/label/secret/required/help), `create_checkout`, `parse_webhook`, `fetch`, and `check`.
   `stripe.py` is the only adapter; `fake.py` exists for tests and dev; `registry.py` maps a
   name to an adapter. An adapter only translates between app records and the provider API; it
   never decides credits or touches balances. Packs use inline prices (Stripe `price_data`), so
   the provider needs no pre-created products.

   **Tables** (new migration after 0008)
   - `payment_config` (single row): `provider`, `enabled`, `currency`, `config`
     (Fernet-encrypted JSON holding only the adapter's declared fields), `updated_at`,
     `updated_by`. Empty `provider` means payments are off. Secrets use a domain-separated key
     like `llm/catalog.py` (`cygnusloom-payment:<secret>`) and are never rendered back; the form
     shows set/not-set and a clear checkbox.
   - `credit_pack`: `label`, `credits`, `price_minor`, `enabled`, `sort`. Validation: at least 1
     credit, positive price, whole minor units.
   - `payment`: `user_id`, `provider`, `pack_id`, `credits`, `amount_minor`, `currency`,
     `checkout_id`, `external_id` (the provider payment/charge id used by later events),
     `granted_credits`, `refunded_minor`, `clawed_back_credits`, `kind`
     (`checkout`/`auto_topup`), `status`
     (`pending`/`paid`/`needs_action`/`failed`/`expired`/`refunded`/`disputed`/`needs_review`),
     `note`, timestamps. Unique `(provider, external_id)`.
   - `payment_event`: `provider`, `event_id`, `payment_id`, `kind`, `received_at`; unique
     `(provider, event_id)` for webhook idempotency.
   - `payment_customer`: `user_id`, `provider`, `customer_id`, `payment_method`,
     `auto_topup`, `pack_id`, `threshold`, `consecutive_failures`, `last_error`, timestamps;
     unique `(user_id, provider)`.
   - `credit_entry` gains nullable indexed `payment_id` and `payment_event_id`; reasons
     `purchase`, `purchase-refund`, and `dispute-return`.
   - Add `grant_credits()` beside `charge_reply()` using an atomic
     `credit_balance = credit_balance + amount` plus a ledger row. Do not reuse `set_balance()`
     for purchases; it writes an absolute value and loses concurrent grants.

   **Buying a pack**
   1. `/credits` shows the balance, recent ledger entries, and enabled packs.
   2. `POST /credits/checkout` creates a `pending` payment, then the adapter opens a Stripe
      Checkout Session (`mode=payment`, inline price, `client_reference_id` and PaymentIntent
      metadata = payment id, `Idempotency-Key: payment-<id>`), then redirects.
   3. `POST /payments/webhook/{provider}` is public (no login or CSRF), verifies the raw-body
      signature, and is the only place that grants credits. In one transaction it inserts
      `payment_event` (duplicate → reply 200 and stop), re-checks the reported amount, currency,
      and payment id against the stored payment (mismatch → `needs_review`, grant nothing), sets
      `paid`, and calls `grant_credits()`. Unknown events get 200.
   4. The return URL only shows status; it never grants.
   5. Pending payments expire after 24 hours. A "re-check with provider" action uses `fetch`.
   6. Refunds and disputes claw back automatically in the webhook transaction. `charge.refunded`
      deducts the prorated credits for the cumulative refunded amount (only the delta since the
      last event). A dispute deducts credits prorated by the disputed amount, capped at the
      credits not already clawed back; a dispute closed as won restores exactly what that
      dispute deducted. Each clawback is a `purchase-refund` ledger entry linked to the payment
      and provider event, may take the balance negative, and runs only up to `granted_credits`.
      A refund of a payment that was never granted changes no balance, and a refund for a
      deleted account is recorded without a balance change.

   **Auto top-up**
   - The player picks a pack and a credit threshold, then completes a Setup-mode Checkout to save
     a card; the webhook stores the provider customer and payment method.
   - At the start of a turn, when the owner's balance is below the threshold, the app attempts
     one off-session charge for the chosen pack (the idempotency key includes the account and
     pack). Success grants credits and the turn continues; `requires_action` stores a completion
     link on `/credits`; failure still allows the turn when the remaining balance covers the
     price, otherwise the normal shortfall refusal applies.
   - At most one auto top-up in flight per account (a `pending` auto-top-up payment is the lock).
     Three consecutive failures disable auto top-up and show the reason on `/credits`.
   - Changing the provider disables all enrollments for the old provider (they must re-save a
     card). Auto top-up purchases appear in the admin payment list like any other purchase.

   **Admin page** (`/admin/payments`, new nav entry)
   - Provider: choose from registered adapters, dynamic fields from `fields`, enable toggle,
     currency, test connection (`check()`), and the webhook URL with a copy button. Show a "test
     mode" badge for test keys. Changing provider or clearing credentials is blocked while a
     payment is pending.
   - Packs: add/edit/enable/delete with a "credits for price" preview.
   - Payments: search/filter by status, account, pack, amount, and external id; actions are
     re-check with the provider and expire a pending payment. Refunds and disputes need no admin
     action; `needs_review` only marks a webhook whose amount, currency, or payment id did not
     match the stored payment.
   - Every mutation requires recent auth and CSRF and writes an `AdminAudit` entry with secrets
     redacted.

   **Testing**
   - `tests/test_payments.py`: disabled state; config CRUD, encryption at rest, secret masking;
     pack validation; checkout creation; webhook grants exactly once on replay; bad signature;
     amount/currency mismatch; unknown event; full and partial refund clawback with cumulative
     refunds, dispute created/won/lost, and a refund of an ungranted payment; the pending
     provider-switch block; auto top-up success, `requires_action`, and the failure counter;
     exempt accounts see no controls.
   - The fake adapter's checkout URL is an internal pay/cancel page available only when the fake
     adapter is registered (tests/dev), so the live UI test can exercise the whole flow without
     `stripe`.
   - Update `tests/test_credits.py` expectations where the credits page or help text changes.

   **Docs and cleanup**
   - Update the README credits section (currently says payment is not integrated), the
     `/credits` page copy, and the admin help text.
   - After the migration, delete the local `cygnusloom.db` for a fresh manual-test state.
