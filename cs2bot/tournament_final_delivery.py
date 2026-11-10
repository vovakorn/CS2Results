"""A final card has its own delivery identity, separate from an ordinary result."""
from __future__ import annotations

import asyncio

from .tournament_publications import final_content_uid


def deliver_final(pending, channel, runtime, context=None):
    match, destination = pending.match, pending.channel_id
    if match.source != "liquipedia" or not match.final_identity_confirmed or not runtime.can_render_final_card(match):
        return "held"
    if not runtime._delivery_is_current(destination, "results", pending.generation):
        return "held"
    uid, claim, confirmed = pending.content_uid or final_content_uid(match, destination), None, False
    try:
        if (asyncio.run(runtime.is_processed("content_" + runtime.safe_storage_part(uid)))
                or asyncio.run(runtime.reconcile_content_delivery(uid, "tournament_final"))):
            asyncio.run(runtime.mark_channel_processed(match, destination))
            asyncio.run(runtime.delete_result_delivery(pending))
            return "reconciled"
        claim = asyncio.run(runtime.claim_content_delivery(uid))
        if claim is None:
            return "duplicate"
        platform = channel.get("platform", "telegram")
        card = runtime.render_final_card(match, card_format="square" if platform == "telegram" else "portrait")
        text = runtime.format_match(match)
        if not runtime._delivery_is_current(destination, "results", pending.generation):
            asyncio.run(runtime.release_delivery_claim(claim))
            return "held"
        claim = asyncio.run(runtime.mark_delivery_claim_attempting(claim))
        publication_key = "final_" + runtime.safe_storage_part(uid)
        if platform == "instagram":
            runtime.publish_rendered_cards(publication_key, [card], runtime._instagram_caption(text), context)
        elif platform == "threads":
            runtime._publish_threads_chain(runtime._threads_tournament_key(match), publication_key, [card],
                runtime._instagram_caption(text), context, allow_missing_root=True)
        else:
            runtime.send_photo_to_telegram(channel["chat_id"], card,
                runtime._truncate_telegram_html(text, runtime.MAX_TELEGRAM_CAPTION_LENGTH), filename="cs2-grand-final.png",
                timeout=runtime.RESULT_TELEGRAM_TIMEOUT_SECONDS, max_attempts=runtime.RESULT_TELEGRAM_MAX_ATTEMPTS)
        confirmed = True
        claim = asyncio.run(runtime.mark_delivery_claim_sent(claim))
        asyncio.run(runtime.mark_content_processed(uid, "tournament_final"))
        asyncio.run(runtime.mark_channel_processed(match, destination))
        asyncio.run(runtime.delete_result_delivery(pending))
        runtime._record_post_analytics(destination, uid, "results", media_card=True, generation=pending.generation)
        return "sent"
    except Exception as exc:
        uncertain = isinstance(exc, (runtime.TelegramDeliveryUncertainError,
                                    runtime.InstagramDeliveryUncertainError, runtime.ThreadsDeliveryUncertainError))
        if uncertain:
            runtime._retain_uncertain_delivery_claim(claim)
            runtime._notify_admin("tournament_final_delivery_uncertain",
                f"Не подтверждена доставка финальной карточки {match.tournament_name} в {destination}; повтор удержан.")
        elif claim is not None and not confirmed:
            asyncio.run(runtime.release_delivery_claim(claim))
            runtime._notify_admin("tournament_final_delivery_failed",
                f"Не подготовлена/не доставлена финальная карточка {match.tournament_name} в {destination}: "
                f"{type(exc).__name__}. Задача сохранена для повтора.")
        if not uncertain:
            try:
                asyncio.run(runtime.record_result_delivery_attempt(pending))
            except Exception:
                pass
        return "uncertain" if uncertain else "failed"
