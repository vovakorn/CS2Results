# Threads publishing

Threads delivery is an opt-in companion to Telegram and Instagram. Each platform
has its own Object Storage claims, processed markers and result outbox channel;
a confirmed post on one platform never marks another platform as delivered.

Publisher развёрнут в production, но сейчас выключен
`ENABLE_THREADS_PUBLISHING=0`; реальных Threads-постов он не создаёт.

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

Keep `ENABLE_THREADS_PUBLISHING=0` until the candidate has passed its dry run
and one uploaded public card URL has been checked without authentication.

The separate private `cs2-social-publish` test function offers
`{"job":"threads_test_card"}` for one explicitly approved image-card test. It
needs the same Threads media variables, `XRAY_CONFIG_JSON`, the Threads Lockbox
secret ID, Object Storage credentials and the `lockbox.payloadViewer` role. Do
not add an API Gateway route for this function.

## Behaviour

- A rendered PNG is stored under a deterministic `threads/<publication-key>/`
  URL with immutable caching before any Meta request. That makes the media URL
  stable for the lifetime of its delivery claim.
- A single card is sent as an `IMAGE` container. Two to twenty cards are first
  sent as carousel-item image containers, combined into a `CAROUSEL` container,
  then published with `threads_publish`.
- Schedule and digest use independent content claims named `threads_<job>_...`.
  Final results use the durable outbox channel `threads`, retried independently
  by the existing five-minute `retry_only` trigger.
- Immediately before the first Meta request, the claim becomes `attempting` and
  is no longer reclaimable after the normal lease expires.
- The scheduler records `sent` before the processed marker. A crash in between
  is reconciled without another Threads post.
- Network errors, HTTP 5xx and malformed successful responses are treated as
  `uncertain`: the claim is retained and an administrator alert is raised. A
  final-result outbox item is removed, so a retry is manual only after checking
  Threads. Definite pre-publication errors release the claim for a later retry.
