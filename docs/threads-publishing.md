# Threads publishing

Threads delivery is an opt-in companion to Telegram and Instagram. Each platform
has its own Object Storage claims, processed markers and result outbox channel;
a confirmed post on one platform never marks another platform as delivered.

Publisher включён в production через `ENABLE_THREADS_PUBLISHING=1`; реальные
штатные scheduler-посты подтверждены.

## Required resources

Use a **public media bucket separate from the state bucket**. It may be the
existing social-media bucket used for Instagram, but only when the function has
write access and objects under `threads/` may be `public-read`. Never expose the
state bucket: it contains outbox items, claims, caches and diagnostics.

Configure the main function as follows:

```text
ENABLE_THREADS_PUBLISHING=1
THREADS_MEDIA_BUCKET=<public bucket name>
THREADS_MEDIA_PUBLIC_BASE_URL=https://storage.yandexcloud.net/<public bucket name>
THREADS_LOCKBOX_SECRET_ID=<Threads OAuth Lockbox secret id>
XRAY_CONFIG_JSON=<Lockbox binding containing the Xray client config>
```

`THREADS_LOCKBOX_SECRET_ID` identifies a Lockbox secret, not a token. Its
payload must contain `ACCESS_TOKEN` and `USER_ID`, written by the OAuth
function. Xray is started only around Meta calls; Lockbox and Object Storage
remain direct. The Telegram proxy is not read or changed by this flow.

Build the main function with the Xray binary:

```bash
XRAY_ENABLED=1 scripts/build_function_zip.sh
```

For a new environment, keep `ENABLE_THREADS_PUBLISHING=0` until the candidate
has passed its dry run and one uploaded public card URL has been checked without
authentication. Production is already enabled after those checks.

The separate private `cs2-threads-publish-test` function offers
`{"job":"threads_test_card"}` for an explicitly approved connectivity test and
`{"job":"threads_visual_test_card"}` for a production-rendered visual test. It
needs the same Threads media variables, `XRAY_CONFIG_JSON`, the Threads Lockbox
secret ID, Object Storage credentials and the `lockbox.payloadViewer` role. Do
not add an API Gateway route for this function.

## Behaviour

- A rendered PNG is stored under a deterministic `threads/<publication-key>/`
  URL with immutable caching before any Meta request. That makes the media URL
  stable for the lifetime of its delivery claim.
- A single card is sent as an `IMAGE` container. Two to twenty cards are first
  sent as carousel-item image containers, combined into a `CAROUSEL` container,
  then published with `threads_publish`. The publisher checks `status=FINISHED`
  before creating the carousel parent and before publishing the final container.
  It polls every two seconds with one shared 30-second budget for child readiness,
  parent creation and parent readiness. Initial image creation precedes that budget.
  A timeout, terminal media status or failed status read is a definite failure
  before publication: result outbox items remain eligible for the next retry.
  Unexpected `PUBLISHED` status is uncertain and cannot trigger an automatic repost.
- Schedule and digest use independent content claims named `threads_<job>_...`.
  Final results use the durable outbox channel `threads`, retried independently
  by the existing five-minute `retry_only` trigger.
- Immediately before the first Meta request, the claim becomes `attempting` and
  is no longer reclaimable after the normal lease expires.
- The scheduler records `sent` before the processed marker. A crash in between
  is reconciled without another Threads post.
- Network errors, HTTP 5xx and malformed successful POST responses are treated as
  `uncertain`: the claim is retained and an administrator alert is raised. A
  final-result outbox item is removed, so a retry is manual only after checking
  Threads. Definite pre-publication errors release the claim for a later retry.

Readiness waiting and automatic renewal below are implemented locally on
4 October 2026; they are **not yet deployed** to production.

## Automatic token renewal

The existing private OAuth function accepts `{"internal_job":"threads_token_refresh"}`
directly or inside a Yandex timer message. No API Gateway refresh route is added.
Run it daily: it reads the current Threads secret and skips fresh tokens without
contacting Meta or adding a secret version. Renewal starts within 14 days of saved
expiry, only for an unexpired token at least 24 hours old. The current and renewed
tokens must pass debugger checks for the configured app, exact account ID and all
three required scopes; the renewed `/me` profile must also match the owned account.
An expired/revoked token still requires owner authorization.

Only after validation, the function adds a Lockbox version based on the original
version, changing `ACCESS_TOKEN`, `TOKEN_EXPIRES_AT` and `GRANTED_SCOPES`. Other
entries, including app secrets and binary values, are inherited. Expiry is bounded
by both the refresh response and debugger. A refreshed token must materially extend
expiry (over the previous expiry plus 14 days); malformed responses do not replace
saved credentials. The publisher reads the latest version on its next delivery.

Keep OAuth instance concurrency at **one** for the existing proxy lifecycle;
this does not serialize different cloud instances. Use one daily timer and pause
it before manual reauthorization or secret edits; resume after the flow completes.
The job re-reads the secret version before writing and rejects an observed
intervening update. `baseVersionId` preserves entries but is not a compare-and-swap:
concurrent secret writers are not supported. Unconfirmed Lockbox
operations are reported as failures; the next invocation re-reads the current secret,
so an already completed write with a fresh expiry is skipped.

`{"internal_job":"threads_token_refresh","dry_run":true}` reads/validates without
calling the refresh endpoint or writing credentials. A timer failure raises a safe
exception; logs/responses omit token values. Deployment, IAM and daily schedule are
described in [the cloud runbook](yandex-cloud-deploy.md#threads-token-renewal).

References: [Meta refresh endpoint](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api?entity=request-34203612-ee0a2365-9d95-4cbe-8087-1cfb04d38c05),
[Lockbox version inheritance](https://yandex.cloud/en/docs/lockbox/api-ref/Secret/addVersion),
[Yandex timer envelope](https://yandex.cloud/en/docs/functions/concepts/trigger/timer).

## OAuth permissions and recovery

Tournament reply chains require `threads_basic`, `threads_content_publish` and
`threads_manage_replies`. The OAuth function requests all three scopes. An
existing token does not gain a permission when code is deployed: deploy the
updated **separate OAuth function**, then complete Threads authorization again
for the owned account. Check `/debug_token` for `is_valid=true` and all three
scopes before treating the connection as ready. The callback checks actual
grants through the debugger before writing a new secret version; an invalid
token or missing scope leaves existing credentials untouched. `GRANTED_SCOPES`
records the scopes returned by the API.

On 3 October 2026, the production token was valid but held only the first two
scopes. Creating an unpublished root container succeeded; the same request with
`reply_to_id` failed with HTTP 403, `THApiException`, code 10, "Application does
not have permission for this action". The EPL chain had confirmed root/tail
`18093589079643473` and no blocked flag or active reservation. Seven match
results and three standings items remained in the Threads outbox. Root
publication success therefore does not verify reply delivery.

After explicit approval and reauthorization, verify an unpublished reply
container first. Preserve processed markers and claims; the existing
five-minute retry worker can deliver eligible result outbox items independently
of Telegram and Instagram. Daily schedule/digest failures have no result outbox,
so they must not be described as automatically recovered historical issues.
A chain with an uncertain outcome still requires manual platform verification;
missing permissions alone do not justify resetting its tail.

Reference: [Meta Threads reply API example](https://www.postman.com/meta/threads/request/y4uzu58/respond-to-replies).

The owner completed reauthorization on 3 October. Actual grants were checked;
eight EPL results were then delivered by the existing scheduled workers, and
at 22:46 Moscow time the Threads match-result outbox was empty. The confirmed
tail was `17903135745657680`, without a reservation or blocked flag. Three older
StarLadder standings items remain a separate unresolved backlog; restoration
of match results does not establish their delivery.

Container readiness reference: [Meta Threads status API](https://www.postman.com/meta/threads/request/m47wqlq/check-container-s-publishing-status).

## Remaining reliability limits after recovery

The OAuth scope omission is corrected and the callback rejects tokens without
reply permission. This does not prevent a later token revocation or expiry.
The new token's debugger response showed expiry at 2026-12-02 22:34:10 Moscow.
The deployed OAuth version still has no scheduled token renewal; the local job
above requires a separate release and timer. External renewal jobs were not audited.

The deployed Threads publisher also calls `threads_publish` immediately after
container creation, without polling `FINISHED`. Unpublished probes showed image
processing taking about five seconds. Intermittent HTTP 400 failures observed
during recovery were resolved by scheduled result retries; the exact error body
was not captured. Container readiness and token renewal are prepared locally,
but were not deployed in this incident. Network-uncertain delivery can
still intentionally block a tournament chain to prevent duplicates.
