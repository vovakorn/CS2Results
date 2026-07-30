# P0 release gate

## Product decision

The first channel publishes only confirmed Tier-1 LAN CS2 results. Qualifiers,
showmatches, academy/youth/junior teams, unfinished matches, 0:0 records, tied
series and invalid BO1/BO3/BO5 scores are blocked.

The final public templates are Russian. Result posts omit match time, while the
morning schedule uses Moscow time. Score and winner are hidden with Telegram
spoilers by default; public messages contain source attribution and hashtags
and do not expose internal match IDs or filter diagnostics.

## Automated evidence

- Unit, integration and security tests cover source selection, freshness,
  validation, formatting, atomic delivery claims and duplicate prevention.
- A production-shaped PandaScore sample of 100 finished records is required
  before release. The sample must contain at least 50 records, and no record
  rejected by the quality gate may be returned for publication.
- PandaScore failure recovery is covered by tests for Liquipedia fallback.
  Runtime fallback remains disabled until an approved Liquipedia API key is
  installed.
- PandaScore requests use an explicit recent `begin_at` range. The API may
  otherwise place old records with null dates before fresh finished matches.

## Cloud gate

Before enabling a timer:

1. Store Telegram, PandaScore and Object Storage credentials in Yandex Lockbox.
2. Grant the function service account payload-viewer access only to those
   secrets.
3. Configure a private `TELEGRAM_ADMIN_CHAT_ID`.
4. Run a dry-run with production filters.
5. Send exactly one qualifying real result.
6. Run the same invocation again and verify `duplicates_skipped=1`.
7. Enable the timer only after steps 1-6 succeed.

The timer must remain disabled when there is no qualifying real result available
for the controlled send. A fake or low-quality match must not be published to
complete the checklist.
