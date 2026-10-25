import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.item import ClothingItem, ItemStatus
from app.models.learning import UserLearningProfile
from app.models.outfit import Outfit, OutfitItem, OutfitSource, OutfitStatus
from app.services.learning_service import LearningService
from app.services.recommendation_service import AIRecommendationError, RecommendationService
from tests.test_item_scorer import _weather


async def setup_wardrobe(db, user):
    items = [
        ClothingItem(
            user_id=user.id,
            type=kind,
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            formality="casual",
        )
        for kind in ["shirt"] * 3 + ["pants"] * 3 + ["sneakers"]
    ]
    db.add_all(items)
    await db.commit()
    user.preferences = None
    return {n: item.id for n, item in enumerate(items, 1)}


def reply(options):
    return SimpleNamespace(
        content=json.dumps(
            {"outfits": [{"items": nums, "headline": f"look {nums}"} for nums in options]}
        ),
        model="test",
        endpoint="test",
    )


@pytest.mark.asyncio
async def test_repairs_duplicate_key_pieces_before_persisting(db_session, test_user):
    numbers = await setup_wardrobe(db_session, test_user)
    service = RecommendationService(db_session)
    with (
        patch.object(service, "_format_items_for_prompt", return_value=("", numbers)),
        patch(
            "app.services.recommendation_service.AIService.generate_text",
            AsyncMock(
                side_effect=[reply([[1, 4, 7], [1, 5, 7], [3, 6, 7]]), reply([[2, 5, 7]])],
            ),
        ) as generate,
    ):
        outfits = await service.generate_recommendations(
            test_user,
            "casual",
            weather_override=_weather(),
            count=3,
            event_notes="test",
        )
    assert generate.await_count == 2
    assert len(outfits) == 3
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Outfit).where(Outfit.user_id == test_user.id)
        )
        == 3
    )
    assert len({oi.item_id for o in outfits for oi in o.items if oi.item.type == "shirt"}) == 3
    for outfit in outfits:
        assert {oi.item_id for oi in outfit.items} == {
            numbers[n] for n in outfit.ai_raw_response["items"]
        }


@pytest.mark.asyncio
async def test_failed_repair_does_not_save_incomplete_outfits(db_session, test_user):
    numbers = await setup_wardrobe(db_session, test_user)
    service = RecommendationService(db_session)
    with (
        patch.object(service, "_format_items_for_prompt", return_value=("", numbers)),
        patch(
            "app.services.recommendation_service.AIService.generate_text",
            AsyncMock(
                return_value=reply([[1, 7], [4, 7]]),
            ),
        ) as generate,
        pytest.raises(AIRecommendationError, match="complete, distinct"),
    ):
        await service.generate_recommendations(
            test_user,
            "casual",
            weather_override=_weather(),
            count=3,
            event_notes="test",
        )
    assert generate.await_count == 2
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Outfit).where(Outfit.user_id == test_user.id)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_rejection_avoids_combination_without_banning_its_clothes(db_session, test_user):
    numbers = await setup_wardrobe(db_session, test_user)
    rejected = Outfit(
        user_id=test_user.id,
        occasion="casual",
        status=OutfitStatus.rejected,
        source=OutfitSource.on_demand,
    )
    db_session.add(rejected)
    await db_session.flush()
    db_session.add_all(
        [
            OutfitItem(outfit_id=rejected.id, item_id=numbers[n], position=pos)
            for pos, n in enumerate([1, 4, 7])
        ]
    )
    await db_session.commit()
    service = RecommendationService(db_session)
    with (
        patch.object(service, "_format_items_for_prompt", return_value=("", numbers)),
        patch(
            "app.services.recommendation_service.AIService.generate_text",
            AsyncMock(
                return_value=reply([[1, 4, 7], [1, 5, 7]]),
            ),
        ) as generate,
    ):
        [outfit] = await service.generate_recommendations(
            test_user,
            "casual",
            weather_override=_weather(),
            count=1,
            event_notes="test",
        )
    assert {oi.item_id for oi in outfit.items} == {numbers[n] for n in [1, 5, 7]}
    assert generate.await_count == 1
    counts = await service._get_recent_suggestion_counts(test_user)
    assert counts[numbers[1]] == 2  # No actual wear log is required.


@pytest.mark.asyncio
async def test_repair_failure_returns_valid_option_without_rewriting_it(db_session, test_user):
    numbers = await setup_wardrobe(db_session, test_user)
    service = RecommendationService(db_session)
    with (
        patch.object(service, "_format_items_for_prompt", return_value=("", numbers)),
        patch(
            "app.services.recommendation_service.AIService.generate_text",
            AsyncMock(
                side_effect=[reply([[1, 4, 7], [1, 7]]), TimeoutError("test")],
            ),
        ),
    ):
        outfits = await service.generate_recommendations(
            test_user,
            "casual",
            weather_override=_weather(),
            count=3,
            event_notes="test",
        )
    assert len(outfits) == 1
    assert outfits[0].ai_raw_response["items"] == [1, 4, 7]


@pytest.mark.asyncio
async def test_status_only_feedback_does_not_poison_feature_preferences(db_session, test_user):
    numbers = await setup_wardrobe(db_session, test_user)
    for iid in numbers.values():
        item = await db_session.get(ClothingItem, iid)
        item.primary_color = "black"
        item.style = ["minimal"]
    for status in [OutfitStatus.accepted, OutfitStatus.rejected]:
        outfit = Outfit(user_id=test_user.id, occasion="casual", status=status)
        db_session.add(outfit)
        await db_session.flush()
        db_session.add_all(
            [
                OutfitItem(outfit_id=outfit.id, item_id=numbers[n], position=pos)
                for pos, n in enumerate([1, 4, 7])
            ]
        )
        await db_session.commit()
        await LearningService(db_session).process_feedback(outfit.id, test_user.id)
        profile = await db_session.get(UserLearningProfile, test_user.id)
        # Three black pieces in one accepted look count as one piece of evidence.
        assert profile.learned_color_scores == {"black": 0.15}
        assert profile.learned_style_scores == {"minimal": 0.15}
    assert profile.feedback_count == 2
