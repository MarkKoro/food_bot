# Native Food analysis recovery

Food owns this workflow, its database and its conversation state. Ordinary
Telegram camera photos still run in one step; a file upload is optional. Core
availability is not required. No additional consumer, provider, worker or
database is introduced.

## Before analysis and after interruption

`photo_recovery.py` stores one owner's pending photo metadata in the existing
`bot_state` JSON: Telegram file/message identifiers, original caption and human
portion/composition clarification. It never stores raw image bytes or converts
an unanalysed photo into a meal with guessed or zero macros. The native handler
durably checkpoints this metadata before its first channel/status/download/AI
network await. If that local DB write fails, no analysis should proceed.

Availability is bounded to six hours from the original Telegram `Message.date`.
This is a product limit, not a scientifically measured optimum. Redelivery and
clarification do not extend it or overwrite a later edit. An opaque UUID is a
CAS identity, not a sequence number. Expired, cancelled, purged or superseded
callbacks cannot resume or overwrite a newer draft. Legacy persisted metadata
upgrades only when sufficient owner/message information exists; malformed
metadata fails closed.

Telegram/server clock skew up to 60 seconds into the future is accepted both
at staging and subsequent validation; larger future dates are rejected. This
does not renew or change the original source deadline.

Home, payment screens and `/start` clear the active clarification mode and
offer explicit **Continue photo** / **Cancel photo** actions in the existing
single native menu. Access regrant and relaunch never run AI automatically.
Cancel durably retires the exact draft; Home preserves its recoverability.
New text after Home is a new meal description, not a hidden old-photo addition.
An initial denied channel/budget/limit preserves only bounded private metadata;
native channel, premium, payment and attempt checks still precede analysis and
meal writes. Clear-data removes the draft and advances Food's existing reset
epoch, so a late handler cannot recreate it.

Expiry makes data unavailable immediately on every read/write check. Physical
metadata cleanup is best effort on startup and each iteration of the existing
60-second payment recovery loop, at most 128 selected owners per iteration.
It rechecks current versions, keeps unrelated state and financial records, and
has its own error boundary so it cannot skip payment reconciliation. No DB
transaction is held across a network/provider call.

## Known saved result and exact billing boundary

The existing owner/source key remains the duplicate boundary. A successful
`billing.save_meal` transaction includes meal, automatic nutrient observations,
request/debit, completion of only that native action's analysis token and exact
draft retirement. A failure rolls all of them back. Wrong-owner/goal tokens
cannot finalize a meal; an old token never finishes a replacement lease. An
early duplicate does not complete an unrelated lease. Text and nested
voice-to-text use the same native token boundary.

A bounded unacknowledged result pointer survives a process crash after commit.
Explicit recovery displays the owner's existing KBJU, saved date and record
link without AI, another request/debit or a date change. The pointer is removed
after the native result menu returns successfully. Unknown Telegram delivery
is not retried automatically. The existing guard's 900-second unknown attempt
lease still applies if a process died before a known meal commit; this change
does not invent a provider cancellation or polling delivery guarantee.

## Clear-data during AI

The outer native action captures Food's existing monotonic reset epoch before
transcription, compression and provider awaits; nested voice-to-text reuses it.
Private `save_meal` arguments cannot come from a public request body. The
expected epoch is checked in the meal transaction before duplicate return or
new meal/request/debit writes. Old work cannot adopt a newer same-key record.

Exact cache keys include this captured epoch. Food's trusted predicate checks
the owner epoch inside canonical cache publication's own write transaction;
both purge-first and publication-first orders serialize correctly. See the
canonical helper and shared contract in
[Core shared/ai_cache.py](https://github.com/MarkKoro/progressor_core/blob/main/shared/ai_cache.py)
and
[Core docs/SELECTED_FEATURES.md](https://github.com/MarkKoro/progressor_core/blob/main/docs/SELECTED_FEATURES.md).
`progressor_ai_cache.py` must stay byte-identical to that canonical helper and
the other mirrors; do not fork its SQL or substitute check-then-put calls.
The hook alone is not an authorization check or a meal commit fence.

## Verification boundary

The package has private-DB/dummy-provider native path tests, exact lease and
reset isolation, cleanup races and injected actual process exits before
`finally` and after durable commit before acknowledgement. External HTTP is
denied in these harnesses. These checks do not prove Telegram polling-offset
delivery, physical iPhone/Android behavior or real payment/provider behavior.
Keep paid/user data on code rollback. An older writer may not preserve these
draft semantics; retain compatible source and journal the deployment evidence.
