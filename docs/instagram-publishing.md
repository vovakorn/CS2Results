# Instagram publishing

Instagram delivery is an opt-in companion to Telegram.  It has independent
Object Storage claims and processed markers, so a confirmed Telegram post does
not mark an Instagram post as delivered (or vice versa).

## Required resources

Create a **separate** Object Storage bucket for public publication cards.  Do
not reuse the state bucket: it contains outbox items, claims, cache objects and
diagnostics.  Give the Cloud Function service account write access only to the
new media bucket.  The objects uploaded under `instagram/` use `public-read`;
the bucket must permit that ACL.

The function needs these settings, all through Lockbox bindings where
appropriate:

```text
ENABLE_INSTAGRAM_PUBLISHING=1
INSTAGRAM_MEDIA_BUCKET=<public bucket name>
INSTAGRAM_MEDIA_PUBLIC_BASE_URL=https://storage.yandexcloud.net/<public bucket name>
INSTAGRAM_LOCKBOX_SECRET_ID=<Instagram OAuth Lockbox secret id>
XRAY_CONFIG_JSON=<Lockbox binding containing the Xray client config>
```

`INSTAGRAM_LOCKBOX_SECRET_ID` identifies a secret; it is not the Instagram
access token. The secret payload must contain `ACCESS_TOKEN` and `USER_ID`,
which the OAuth function writes after a successful consent flow.

Build the main function with the Xray binary:

```bash
XRAY_ENABLED=1 scripts/build_function_zip.sh
```

В production Instagram включён (`ENABLE_INSTAGRAM_PUBLISHING=1`) после dry-run и
проверки публичного media bucket. Отключайте флаг перед изменениями, которые не
прошли эту проверку.

## Behaviour

- The daily schedule and daily digest are rendered as 1080×1080 cards. A
  schedule with two pages is published as one Instagram carousel.
- Each final match result is queued under an `instagram` delivery channel in
  the existing durable outbox. The 15-minute result trigger enqueues it and
  the five-minute `retry_only` trigger retries only that retained item.
- The state key starts with `instagram_`, keeping deduplication independent of
  Telegram.
- Immediately before the first Meta request, the claim becomes `attempting`.
  That state is not reclaimed after the normal lease expires, so a crash after
  Meta accepts a request cannot cause an automatic duplicate.
- After Meta confirms publication, the delivery claim becomes `sent`, then the
  processed marker is written. A process crash between these operations is
  reconciled without another post.
- Network errors, HTTP 5xx, malformed successful responses and a missing publish
  acknowledgement become `uncertain` and alert the administrator. For a final
  result the outbox item is removed; any retry is manual after checking
  Instagram, because Meta may already have created a post.
