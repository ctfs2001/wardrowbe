import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from arq import Retry
from sqlalchemy import select, update

from app.config import get_settings
from app.models.item import TAG_COLUMNS, ClothingItem, ItemStatus, TaggedBy, TaggingStatus
from app.models.preference import UserPreference
from app.models.user import User
from app.services.ai_service import AIService, ClothingTags, repeats_identically
from app.services.image_service import ImageService
from app.services.product_photo_service import ProductPhotoService
from app.utils.locale import DEFAULT_LOCALE
from app.workers.db import get_db_session

logger = logging.getLogger(__name__)

TAGGING_MAX_TRIES = 3

# Margin left below job_timeout so the in-job call budget always fires before
# arq's own kill (which does not retry - see _tagging_call_budget).
CALL_BUDGET_MARGIN_SECONDS = 30
MIN_CALL_BUDGET_SECONDS = 30


class ItemVanishedError(Exception):
    """The item row is not visible to this worker's connection.

    Almost always means the producing request has not committed yet, so the job
    must be retried rather than dropped: returning here would leave the item
    stuck in `processing` until the stale sweep condemns it.
    """


def _is_final_attempt(ctx: dict) -> bool:
    return int(ctx.get("job_try") or 1) >= TAGGING_MAX_TRIES


def retry_delay_seconds(ctx: dict) -> int:
    # Exponential, so a provider that is rate limiting or restarting gets room to
    # recover instead of being hit again immediately.
    return min(2 ** int(ctx.get("job_try") or 1) * 5, 120)


def worker_job_timeout_seconds() -> int:
    settings = get_settings()
    base = max(settings.ai_timeout * settings.ai_max_retries + 60, 600)
    return base + (settings.ai_image_timeout + 30 if settings.ai_product_photo_enabled else 0)


def _tagging_call_budget(ai_service: AIService) -> float:
    # No fixed formula bounds the real worst case (per-user endpoint count is
    # unknowable at import time), so this is an upper bound clamped to
    # job_timeout, not an exact figure - it deliberately omits inter-retry
    # backoff sleeps. What matters is that it always fires before arq's own
    # job_timeout kill, since that kill does not retry the job at all.
    settings = get_settings()
    endpoint_count = len(getattr(ai_service, "_endpoints", None) or [None])
    raw = endpoint_count * settings.ai_max_retries * 2 * settings.ai_timeout
    return max(
        MIN_CALL_BUDGET_SECONDS,
        min(
            raw,
            max(settings.ai_timeout * settings.ai_max_retries + 60, 600)
            - CALL_BUDGET_MARGIN_SECONDS,
        ),
    )


async def _clear_ai_started_at(ctx: dict, item_id: str) -> None:
    db = get_db_session(ctx)
    try:
        # Guarded so this only touches a row still in `processing` - a row already
        # moved to a terminal state (cancelled -> ready, or a final-attempt error)
        # must not be re-touched. Not a race guard between this attempt and the
        # next: arq only starts attempt N+1 after this write's enclosing exception
        # has propagated and the retry defer has elapsed, so there is no window
        # where two attempts' writes could interleave.
        await db.execute(
            update(ClothingItem)
            .where(ClothingItem.id == UUID(item_id), ClothingItem.status == ItemStatus.processing)
            .values(ai_started_at=None)
        )
        await db.commit()
    finally:
        await db.close()


async def clear_ai_started_at(ctx: dict, item_id: str) -> None:
    # Best-effort: this runs from an exception handler, so a failure here must
    # never replace the exception already in flight. Bounded so a failing write
    # can't hang arq's run_job wait indefinitely.
    try:
        await asyncio.wait_for(_clear_ai_started_at(ctx, item_id), timeout=5)
    except Exception:
        pass


def tags_to_item_fields(tags: ClothingTags, raw_response: str | None = None) -> dict[str, Any]:
    """Convert ClothingTags to item database fields."""
    # Build the tags JSONB object for frontend display
    tags_jsonb = {
        "colors": tags.colors or [],
        "pattern": tags.pattern,
        "material": tags.material,
        "style": tags.style or [],
        "season": tags.season or [],
        "formality": tags.formality,
        "fit": tags.fit,
    }
    if tags.logprobs_confidence is not None:
        tags_jsonb["logprobs_confidence"] = tags.logprobs_confidence

    fields = {
        "type": tags.type,
        "subtype": tags.subtype,
        "primary_color": tags.primary_color,
        "colors": tags.colors,
        "pattern": tags.pattern,
        "material": tags.material,
        "style": tags.style,
        "formality": tags.formality,
        "season": tags.season,
        "tags": tags_jsonb,  # Populate the tags JSONB field for frontend
        "ai_processed": True,
        "ai_confidence": tags.confidence,
        "ai_description": tags.description,  # Human-readable description
        "status": ItemStatus.ready,
        "tagging_status": TaggingStatus.tagged,
        "tagged_by": TaggedBy.auto,
        "tagged_at": datetime.now(UTC),
    }
    if raw_response:
        fields["ai_raw_response"] = {"raw_text": raw_response}
    if tags.unrecognized_type:
        fields.setdefault("ai_raw_response", {})["unrecognized_type"] = tags.unrecognized_type
    return fields


async def mark_item_tagging_skipped(ctx: dict, item_id: str) -> None:
    db = get_db_session(ctx)
    try:
        result = await db.execute(select(ClothingItem).where(ClothingItem.id == UUID(item_id)))
        item = result.scalar_one_or_none()
        if item and item.status == ItemStatus.processing:
            item.status = ItemStatus.ready
            item.tagging_status = TaggingStatus.pending
            await db.commit()
    finally:
        await db.close()


async def update_item_status_to_error(ctx: dict, item_id: str, error_msg: str) -> None:
    try:
        db = get_db_session(ctx)
        try:
            # Guarded: only flip status if still processing, because an orphaned job
            # (worker crash, or a job the user already cancelled) must not un-ready or
            # re-error an item the user has already moved past.
            await db.execute(
                update(ClothingItem)
                .where(
                    ClothingItem.id == UUID(item_id), ClothingItem.status == ItemStatus.processing
                )
                .values(
                    status=ItemStatus.error,
                    ai_raw_response={"error": error_msg},
                    ai_failed_at=datetime.now(UTC),
                )
            )
            await db.commit()
        finally:
            await db.close()
    except Exception as e:
        logger.error(f"Failed to update item {item_id} status to error: {e}")


async def tag_item_image(ctx: dict, item_id: str, image_path: str) -> dict[str, Any]:
    """
    Analyze an item's image and update it with AI-generated tags.

    Args:
        ctx: arq context
        item_id: UUID of the item to tag
        image_path: Path to the image file

    Returns:
        Dict with status and tags
    """
    logger.info(f"Starting AI tagging for item {item_id}")

    if not get_settings().effective_ai_vision_enabled:
        logger.info(f"Internal vision disabled; skipping AI tagging for item {item_id}")
        await mark_item_tagging_skipped(ctx, item_id)
        return {"status": "skipped", "reason": "vision disabled", "item_id": item_id}

    try:
        # Verify image exists
        path = Path(image_path)
        if not path.exists():
            error_msg = f"Image not found: {image_path}"
            logger.error(error_msg)
            await update_item_status_to_error(ctx, item_id, error_msg)
            return {"status": "error", "error": "Image not found"}

        # Get user's AI endpoints from preferences, and mark this attempt as started
        ai_endpoints = None
        locale = DEFAULT_LOCALE
        db = get_db_session(ctx)
        try:
            # Get the item to find user_id
            result = await db.execute(select(ClothingItem).where(ClothingItem.id == UUID(item_id)))
            item = result.scalar_one_or_none()
            if item is None:
                raise ItemVanishedError(f"Item {item_id} not visible to worker")

            source_path = item.image_path
            # The worker can dequeue before the producer stores ai_job_id.
            source_job_id = ctx.get("job_id") or item.ai_job_id
            # Only first-time analysis is automatic. Re-tagging and restoring an
            # original must never silently bill for another generation.
            photo_eligible = (
                not item.ai_processed
                and not item.original_image_path
                and not (item.tags or {}).get("product_photo")
                and item.status == ItemStatus.processing
                and ImageService().get_image_path(source_path).resolve() == path.resolve()
            )
            previous_photo = (item.tags or {}).get("product_photo")
            if isinstance(previous_photo, dict) and previous_photo.get("status") == "generating":
                # A previous worker died after claiming a possibly billed call.
                # Finish tagging on retry without buying another generation.
                item.tags = {**item.tags, "product_photo": {**previous_photo, "status": "failed"}}

            locale_result = await db.execute(select(User.locale).where(User.id == item.user_id))
            locale = locale_result.scalar_one_or_none() or DEFAULT_LOCALE

            # Overwritten on every attempt (not set-once), so a retry's backoff wait
            # reads as "queued for retry," not "still analyzing."
            item.ai_started_at = datetime.now(UTC)
            # Cleared with it, because the pair must describe one attempt: a
            # re-analysis that kept the earlier completion would measure a
            # duration across two runs.
            item.ai_completed_at = None

            pref_result = await db.execute(
                select(UserPreference).where(UserPreference.user_id == item.user_id)
            )
            prefs = pref_result.scalar_one_or_none()
            if prefs and prefs.ai_endpoints:
                ai_endpoints = prefs.ai_endpoints
                logger.info(
                    f"Using {len(ai_endpoints)} custom AI endpoints for user {item.user_id}"
                )

            await db.commit()
        finally:
            await db.close()

        # Analyze with AI (uses custom endpoints if available)
        ai_service = AIService(endpoints=ai_endpoints)
        tags = await asyncio.wait_for(
            ai_service.analyze_image(path, locale=locale), timeout=_tagging_call_budget(ai_service)
        )

        logger.info(
            f"AI analysis complete for item {item_id}: type={tags.type}, color={tags.primary_color}"
        )

        photo_metadata = None
        generated_image = None
        settings = get_settings()
        assessment = tags.product_photo
        if settings.ai_product_photo_enabled and photo_eligible and assessment is not None:
            photo_metadata = {**assessment.model_dump(), "status": "skipped"}
            if (
                assessment.needs_redraw
                and assessment.confidence >= settings.ai_product_photo_min_confidence
            ):
                # Persist the claim before the paid request. An arq retry after
                # worker death or a failed final commit must not generate again.
                db = get_db_session(ctx)
                try:
                    result = await db.execute(
                        select(ClothingItem)
                        .where(ClothingItem.id == UUID(item_id))
                        .with_for_update()
                    )
                    current = result.scalar_one_or_none()
                    if (
                        current is None
                        or current.status != ItemStatus.processing
                        or current.image_path != source_path
                        or current.ai_job_id not in (None, source_job_id)
                        or current.ai_processed
                        or current.original_image_path
                        or (current.tags or {}).get("product_photo")
                    ):
                        return {"status": "skipped", "reason": "item changed", "item_id": item_id}
                    current.tags = {
                        **(current.tags or {}),
                        "product_photo": {**photo_metadata, "status": "generating"},
                    }
                    await db.commit()
                finally:
                    await db.close()
                try:
                    generated_image = await asyncio.wait_for(
                        ProductPhotoService().redraw(path), timeout=settings.ai_image_timeout
                    )
                except Exception as error:
                    # Do not fail tagging, expose provider bodies/keys, or retry a
                    # possibly billed request after an ambiguous timeout.
                    logger.warning(
                        "Product photo failed for %s (%s)", item_id, type(error).__name__
                    )
                    photo_metadata["status"] = "failed"

        # Update item in database
        db = get_db_session(ctx)
        try:
            result = await db.execute(
                select(ClothingItem).where(ClothingItem.id == UUID(item_id)).with_for_update()
            )
            item = result.scalar_one_or_none()

            if item is None:
                raise ItemVanishedError(f"Item {item_id} not visible to worker")

            old_image_paths = None
            new_photo_paths = None
            if photo_metadata is not None and (
                item.status != ItemStatus.processing
                or item.image_path != source_path
                or item.ai_job_id not in (None, source_job_id)
            ):
                return {"status": "skipped", "reason": "item changed", "item_id": item_id}
            # Lock only for the final write, never across the external request.
            # A cancelled job, replaced image or newer attempt owns its own photo.
            if (
                photo_metadata is not None
                and item.status == ItemStatus.processing
                and item.image_path == source_path
                and item.ai_job_id in (None, source_job_id)
                and not item.original_image_path
                and not item.ai_processed
            ):
                if generated_image is not None:
                    try:
                        new_photo_paths = await asyncio.to_thread(
                            ImageService().store_product_photo, generated_image, source_path
                        )
                        old_image_paths = {
                            key: getattr(item, key)
                            for key in ("image_path", "medium_path", "thumbnail_path")
                        }
                        for key, value in new_photo_paths.items():
                            setattr(item, key, value)
                        photo_metadata["status"] = "generated"
                    except Exception as error:
                        logger.warning(
                            "Invalid product photo for %s (%s)", item_id, type(error).__name__
                        )
                        photo_metadata["status"] = "failed"
                item.tags = {**(item.tags or {}), "product_photo": photo_metadata}

            # Unguarded by design: worst case after a cancel this backfills tags onto an
            # item the user already moved past, which is harmless (unlike the error path).
            # Update item fields - only update if user hasn't already set a value
            # Always update: ai_processed, ai_confidence, status, ai_raw_response
            # Conditionally update: type, subtype, primary_color, colors, pattern, material, style, formality, season
            ai_fields = tags_to_item_fields(tags, tags.raw_response)
            # Snapshotted once: applying tagging_status before tagged_by/tagged_at in the
            # same loop would otherwise make the guard for the later two fields see the
            # already-updated status and skip them even outside of a race.
            was_pending = item.tagging_status == TaggingStatus.pending

            for field, value in ai_fields.items():
                # Always update AI metadata fields (including tags JSONB and description)
                if field in (
                    "ai_processed",
                    "ai_confidence",
                    "status",
                    "ai_raw_response",
                    "ai_description",
                ):
                    setattr(item, field, value)
                elif field == "tags":
                    # Merge, do not replace. The JSONB also carries user-owned
                    # keys the model never emits (size, care_instructions,
                    # source_url). Same rule as the column guards below: the
                    # AI fills a key that is empty and never overwrites one
                    # that is set, so a manual `fit` survives re-analysis.
                    # Empty AI values are skipped, so a null cannot erase a
                    # real value.
                    merged = dict(item.tags or {})
                    # logprobs_confidence scores this run like ai_confidence does,
                    # so an earlier run's score must not survive the fill below.
                    merged.pop("logprobs_confidence", None)
                    for key, val in (value or {}).items():
                        if val in (None, [], "", {}):
                            continue
                        if merged.get(key) in (None, [], "", {}):
                            merged[key] = val
                    item.tags = merged
                elif field in ("tagging_status", "tagged_by", "tagged_at"):
                    if was_pending:
                        setattr(item, field, value)
                # Only update content fields if user hasn't set them (or they're default/unknown)
                elif field == "type":
                    if not item.type or item.type == "unknown":
                        setattr(item, field, value)
                elif field == "subtype":
                    if not item.subtype:
                        setattr(item, field, value)
                elif field == "primary_color":
                    if not item.primary_color or item.primary_color == "unknown":
                        setattr(item, field, value)
                else:
                    # For other fields (colors, pattern, material, style, etc.), only set if not already set
                    current_value = getattr(item, field, None)
                    if (
                        current_value is None
                        or current_value == []
                        or current_value == ""
                        or current_value == {}
                    ):
                        setattr(item, field, value)

            # The guards above may have kept a user's column value while the
            # AI's went into tags; mirror the columns back so the JSONB and the
            # columns cannot disagree.
            item.tags = {
                **(item.tags or {}),
                **{
                    k: getattr(item, k)
                    for k in TAG_COLUMNS
                    if getattr(item, k) not in (None, [], "")
                },
            }

            item.ai_completed_at = datetime.now(UTC)

            try:
                await db.commit()
            except BaseException:
                if new_photo_paths:
                    ImageService().delete_images(new_photo_paths)
                raise
            if old_image_paths:
                # Source pHash intentionally stays unchanged for duplicate-upload
                # detection, which should compare uploads rather than AI artwork.
                try:
                    ImageService().delete_images(old_image_paths)
                except OSError:
                    logger.warning("Could not clean old photo variants for %s", item_id)
            logger.info(f"Updated item {item_id} with AI tags (status=ready)")

            return {
                "status": "success",
                "item_id": item_id,
                "tags": tags.model_dump(exclude={"raw_response"}),
            }

        finally:
            await db.close()

    except asyncio.CancelledError:
        # A cancelled attempt (worker shutdown, or cancel_item_analysis's abort) must
        # not touch `status` - cancel_item_analysis owns the terminal state for an
        # aborted job via its own guarded update, and marking this `error` here would
        # race it. Only clear the in-flight marker, then let the cancellation and
        # arq's own handling proceed normally.
        await clear_ai_started_at(ctx, item_id)
        raise
    except Exception as e:
        error_msg = str(e)
        logger.exception(f"Error tagging item {item_id}: {error_msg}")
        # Returning normally here made max_tries dead: arq books a returned value
        # as success. arq only reschedules on Retry, so a plain re-raise would
        # surface the failure but still never retry.
        if _is_final_attempt(ctx) or repeats_identically(e):
            await update_item_status_to_error(ctx, item_id, error_msg)
            raise
        await clear_ai_started_at(ctx, item_id)
        raise Retry(defer=retry_delay_seconds(ctx)) from e
