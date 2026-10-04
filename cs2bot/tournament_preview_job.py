"""Independent preview delivery with the existing crash-safe content claims."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .match_sources.sources.tournament_preview_source import fetch_tournament_preview
from .tournament_preview import (
    PreviewUnavailable, format_preview_caption, format_preview_instagram_caption,
    format_preview_threads_caption, load_profiles, publication_window,
)
from .tournament_preview_cards import render_preview_cards
from .tournament_identity import event_key


def _deliver(runtime, uid, platform, send, uncertain_error, *, media_card):
    claim, confirmed = None, False
    try:
        if asyncio.run(runtime.reconcile_content_delivery(uid, "tournament_preview")):
            return {"status": "duplicate"}
        claim = asyncio.run(runtime.claim_content_delivery(uid))
        if claim is None:
            return {"status": "duplicate"}
        claim = asyncio.run(runtime.mark_delivery_claim_attempting(claim))
        post_id = send()
        confirmed = True
        claim = asyncio.run(runtime.mark_delivery_claim_sent(claim))
        asyncio.run(runtime.mark_content_processed(uid, "tournament_preview"))
        runtime._record_post_analytics(platform, uid, "tournament_preview", media_card=media_card)
        if isinstance(post_id, dict):
            result = post_id.get("result")
            result = result[0] if isinstance(result, list) and result else result
            post_id = str(result["message_id"]) if isinstance(result, dict) and "message_id" in result else None
        return {"status": "sent", **({"post_id": post_id} if isinstance(post_id, str) else {})}
    except Exception as exc:
        if isinstance(exc, uncertain_error):
            runtime._retain_uncertain_delivery_claim(claim)
            runtime._notify_admin(f"{platform}_preview_delivery_uncertain",
                f"Не подтверждена доставка превью {uid}; автоматический повтор отключён.")
            return {"status": "uncertain"}
        if claim is not None and not confirmed:
            try:
                asyncio.run(runtime.release_delivery_claim(claim))
            except Exception:
                pass
        runtime._notify_admin(f"{platform}_preview_delivery_failed",
            f"Не завершена доставка превью {uid}; нужна проверка следующего запуска.")
        return {"status": "failed", "error_type": type(exc).__name__}


def _send_telegram(runtime, channel, profile, cards, text):
    if len(cards) > 1:
        return runtime.send_media_group_to_telegram(channel["chat_id"], cards, text,
            filenames=[f"cs2-preview-{profile.key}-{i}.png" for i in range(1, len(cards) + 1)])
    if cards:
        return runtime.send_photo_to_telegram(channel["chat_id"], cards[0], text,
                                             filename=f"cs2-preview-{profile.key}.png")
    return runtime.send_to_telegram(channel["chat_id"], text)


def _allowed(runtime, destination_id: str) -> bool:
    """Legacy preview test runtimes predate owner policy and remain permissive."""
    checker = getattr(runtime, "publication_delivery_allowed", None)
    return True if checker is None else bool(checker(destination_id, "tournament_preview")[0])


def run_preview_job(preview_key: str | None, dry_run: bool, runtime, *, now=None, context=None) -> dict:
    now = now or datetime.now(timezone.utc)
    body = {"job": "tournament_preview" if preview_key else "preview_discovery", "dry_run": dry_run,
            "messages_sent": 0, "duplicates_skipped": 0, "delivery_failures": 0, "previews": []}
    if not dry_run and not runtime.ENABLE_TOURNAMENT_PREVIEWS:
        body["skipped_reason"] = "disabled"
        return {"statusCode": 200, "body": json.dumps(body)}
    try:
        profiles = load_profiles(runtime.TOURNAMENT_PREVIEW_PROFILES_PATH)
    except (ValueError, OSError):
        return runtime._error_response(503, "preview_profiles_invalid")
    if preview_key:
        profiles = [profile for profile in profiles if profile.key == preview_key]
        if not profiles:
            return runtime._error_response(400, "preview_profile_unknown")
    else:
        today = now.astimezone(ZoneInfo("Europe/Moscow")).date()
        profiles = [profile for profile in profiles if profile.approved
                    and (min(stage.start for stage in profile.stages) - today).days in {1, 2}]
    if len(profiles) > 8:
        return runtime._error_response(503, "too_many_preview_candidates")

    for profile in profiles:
        item = {"key": profile.key}
        try:
            preview = asyncio.run(fetch_tournament_preview(profile, now=now))
            eligible = publication_window(preview, now)
            text = format_preview_caption(preview)
            instagram = runtime.instagram_publishing_enabled() if not dry_run else False
            threads = runtime.threads_publishing_enabled() if not dry_run else False
            cards = render_preview_cards(preview) if dry_run or runtime.TELEGRAM_MEDIA_CARDS or instagram or threads else []
        except Exception as exc:
            item["skipped_reason"] = str(exc) if isinstance(exc, PreviewUnavailable) else type(exc).__name__
            body["previews"].append(item)
            continue
        item.update({"eligible": eligible, "card_count": len(cards), "warnings": preview.warnings})
        captions = {"telegram": text}
        caption_errors = {}
        for platform, formatter in (("instagram", format_preview_instagram_caption),
                                    ("threads", format_preview_threads_caption)):
            try:
                captions[platform] = formatter(preview)
            except PreviewUnavailable as exc:
                caption_errors[platform] = str(exc)
        if dry_run:
            item.update({"caption": text, "captions": captions, "caption_errors": caption_errors,
                         "snapshot": preview.model_dump(mode="json"),
                         "event_key": event_key(profile), "vrs_baseline": {"status": "dry_run"}})
        elif not eligible:
            item["skipped_reason"] = "outside_publication_window"
        else:
            try:
                item["vrs_baseline"] = runtime._capture_preview_vrs_baseline(profile, preview)
            except Exception as exc:
                item["vrs_baseline"] = {"status": "failed", "error_type": type(exc).__name__}
                item["skipped_reason"] = "vrs_baseline_unavailable"
                body["delivery_failures"] += 1
                runtime._notify_admin("preview_vrs_baseline_failed",
                    f"Не сохранён начальный VRS для {profile.key}; анонс ждёт повторной проверки.")
                body["previews"].append(item)
                continue
            deliveries = {}
            for channel in runtime._iter_channels():
                channel_id = str(channel.get("id") or channel.get("name", "unknown"))
                uid = f"preview_{profile.key}_{channel_id}"
                if not _allowed(runtime, channel_id):
                    deliveries[f"telegram:{channel_id}"] = {"status": "held"}
                    continue
                telegram_cards = cards if runtime.TELEGRAM_MEDIA_CARDS else []
                deliveries[f"telegram:{channel_id}"] = _deliver(runtime, uid, channel_id,
                    lambda: _send_telegram(runtime, channel, profile, telegram_cards, text),
                    runtime.TelegramDeliveryUncertainError, media_card=bool(telegram_cards))
            for platform, enabled in (("instagram", instagram), ("threads", threads)):
                if not enabled:
                    deliveries[platform] = {"status": "disabled"}
                    continue
                if not _allowed(runtime, platform):
                    deliveries[platform] = {"status": "held"}
                    continue
                if platform in caption_errors or not cards:
                    deliveries[platform] = {"status": "failed",
                        "error_type": caption_errors.get(platform, "preview_cards_missing")}
                    runtime._notify_admin(f"{platform}_preview_caption_invalid",
                        f"Превью {profile.key} не подготовлено для {platform}; проверьте подпись и карточки.")
                    continue
                uid = f"{platform}_tournament_preview_{profile.key}"
                if platform == "instagram":
                    send = lambda: runtime.publish_rendered_cards(uid, cards, captions[platform], context)
                    uncertain_error = runtime.InstagramDeliveryUncertainError
                else:
                    send = lambda: runtime._publish_threads_chain(
                        f"threads:{event_key(profile)}",
                        uid, cards, captions[platform], context, is_root=True)
                    uncertain_error = runtime.ThreadsDeliveryUncertainError
                deliveries[platform] = _deliver(runtime, uid, platform, send,
                    uncertain_error, media_card=True)
            item["deliveries"] = deliveries
            for outcome in deliveries.values():
                status = outcome["status"]
                body["messages_sent"] += status == "sent"
                body["duplicates_skipped"] += status == "duplicate"
                body["delivery_failures"] += status in {"failed", "uncertain"}
        body["previews"].append(item)
    return {"statusCode": 502 if body["delivery_failures"] else 200,
            "body": json.dumps(body, ensure_ascii=False)}
