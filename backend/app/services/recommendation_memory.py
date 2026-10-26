"""Bounded recent feedback for recommendations; lifetime analytics stay intact."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import combinations
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.models.outfit import Outfit, OutfitItem, OutfitStatus, UserFeedback
from app.utils.garment_vocabulary import ITEM_ROLE, canonical_primary_color

HALF_LIFE_DAYS = 30
MEMORY_DAYS = 180
MAX_RECENT_RESPONSES = 200


def feedback_weight(timestamp: datetime, now: datetime) -> float:
    timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
    age = max(0, (now - timestamp).total_seconds() / 86400)
    return 0.0 if age > MEMORY_DAYS else 0.5 ** (age / HALF_LIFE_DAYS)


def summarize_responses(
    outfits: list[Outfit], now: datetime
) -> tuple[dict, dict[UUID, list[UUID]]]:
    colors: dict[str, list[float]] = defaultdict(list)
    styles: dict[str, list[float]] = defaultdict(list)
    pairs: dict[tuple[UUID, UUID], list[float]] = defaultdict(list)
    seen = set()
    for outfit in outfits[:MAX_RECENT_RESPONSES]:
        feedback = outfit.feedback
        if outfit.status in (OutfitStatus.accepted, OutfitStatus.rejected):
            positive = outfit.status == OutfitStatus.accepted
        elif feedback and feedback.accepted is not None:
            positive = feedback.accepted
        else:
            continue  # Pending, skipped and viewed are not preference signals.
        timestamp = (
            outfit.responded_at or (feedback.created_at if feedback else None) or outfit.created_at
        )
        weight = feedback_weight(timestamp, now)
        if weight == 0:
            continue
        core = frozenset(
            oi.item_id
            for oi in outfit.items
            if ITEM_ROLE.get((oi.item.type or "").lower())
            in {"base_top", "bottom", "full_body", "suit"}
        )
        # Repeated variations of the same core on one day do not flood the model.
        key = (timestamp.date(), core or frozenset(oi.item_id for oi in outfit.items))
        if key in seen:
            continue
        seen.add(key)
        if positive:
            for color in {
                canonical_primary_color(oi.item.primary_color or "") for oi in outfit.items
            } - {None, ""}:
                colors[color].append(weight)
            for style in {s for oi in outfit.items for s in (oi.item.style or [])}:
                styles[style].append(weight)
        # A dislike can reduce a pair's optional bonus, never ban either garment.
        for pair in combinations(sorted({oi.item_id for oi in outfit.items}), 2):
            pairs[pair].append(weight if positive else -weight)

    def favorites(features: dict[str, list[float]], limit: int) -> list[str]:
        ranked = sorted(
            ((name, sum(signals) / (len(signals) + 3)) for name, signals in features.items()),
            key=lambda entry: (-entry[1], entry[0]),
        )
        return [name for name, score in ranked if score > 0.2][:limit]

    preferences = {}
    if favorites(colors, 5):
        preferences["learned_favorite_colors"] = favorites(colors, 5)
    if favorites(styles, 3):
        preferences["learned_preferred_styles"] = favorites(styles, 3)
    good_pairs: dict[UUID, list[UUID]] = defaultdict(list)
    ranked_pairs = sorted(
        ((pair, sum(signals) / (len(signals) + 3)) for pair, signals in pairs.items()),
        key=lambda entry: (-entry[1], entry[0]),
    )
    for (first, second), score in ranked_pairs[:50]:
        if score > 0.3:
            good_pairs[first].append(second)
            good_pairs[second].append(first)
    return preferences, dict(good_pairs)


async def recent_recommendation_memory(db, user_id: UUID) -> tuple[dict, dict[UUID, list[UUID]]]:
    now = datetime.now(UTC)
    responded = func.coalesce(Outfit.responded_at, UserFeedback.created_at, Outfit.created_at)
    result = await db.execute(
        select(Outfit)
        .outerjoin(Outfit.feedback)
        .where(
            Outfit.user_id == user_id,
            or_(
                Outfit.status.in_([OutfitStatus.accepted, OutfitStatus.rejected]),
                UserFeedback.accepted.is_not(None),
            ),
            responded >= now - timedelta(days=MEMORY_DAYS),
        )
        .options(
            selectinload(Outfit.feedback), selectinload(Outfit.items).selectinload(OutfitItem.item)
        )
        .order_by(responded.desc(), Outfit.id.desc())
        .limit(MAX_RECENT_RESPONSES)
    )
    return summarize_responses(list(result.scalars().all()), now)
