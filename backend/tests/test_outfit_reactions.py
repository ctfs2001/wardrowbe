from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.item import ClothingItem
from app.models.outfit import Outfit, OutfitItem, OutfitSource, OutfitStatus, UserFeedback
from app.services.outfit_service import OutfitListFilters, OutfitService
from app.utils.timezone import get_user_today
from tests.test_recommendation_quality_flow import setup_wardrobe


async def make_outfit(db, user, day=None):
    numbers = await setup_wardrobe(db, user)
    outfit = Outfit(
        user_id=user.id,
        occasion="casual",
        scheduled_for=day or get_user_today(user),
        status=OutfitStatus.pending,
        source=OutfitSource.on_demand,
    )
    db.add(outfit)
    await db.flush()
    db.add_all(
        [
            OutfitItem(outfit_id=outfit.id, item_id=numbers[n], position=position)
            for position, n in enumerate((1, 4, 7))
        ]
    )
    await db.commit()
    return outfit.id, [numbers[n] for n in (1, 4, 7)]


@pytest.mark.asyncio
async def test_three_actions_keep_preferences_separate_from_idempotent_wear(
    client, auth_headers, db_session, test_user
):
    outfit_id, ids = await make_outfit(db_session, test_user)
    url = f"/api/v1/outfits/{outfit_id}/reaction"
    like = await client.post(url, json={"action": "like"}, headers=auth_headers)
    assert like.status_code == 200, like.text
    assert like.json()["status"] == "accepted"
    assert like.json()["feedback"]["worn_at"] is None
    assert like.json()["feedback"]["actually_worn"] is None
    first_response = (await db_session.get(Outfit, outfit_id)).responded_at
    await client.post(url, json={"action": "like"}, headers=auth_headers)
    assert (await db_session.get(Outfit, outfit_id)).responded_at == first_response
    assert list(
        (
            await db_session.scalars(
                select(ClothingItem.wear_count).where(ClothingItem.id.in_(ids))
            )
        ).all()
    ) == [0, 0, 0]
    for _ in range(2):
        wear = await client.post(url, json={"action": "wear"}, headers=auth_headers)
        assert wear.status_code == 200, wear.text
        assert wear.json()["feedback"]["worn_at"] == get_user_today(test_user).isoformat()
        assert wear.json()["feedback"]["actually_worn"] is True
    assert list(
        (
            await db_session.scalars(
                select(ClothingItem.wear_count).where(ClothingItem.id.in_(ids))
            )
        ).all()
    ) == [1, 1, 1]
    dislike = await client.post(url, json={"action": "dislike"}, headers=auth_headers)
    assert dislike.status_code == 200
    assert dislike.json()["status"] == "rejected"
    assert dislike.json()["feedback"]["worn_at"] == get_user_today(test_user).isoformat()
    assert (
        await db_session.scalar(
            select(func.count(UserFeedback.id)).where(UserFeedback.outfit_id == outfit_id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_unselected_outfits_are_neutral_and_worn_filter_requires_actual_wear(
    client, auth_headers, db_session, test_user
):
    liked, _ = await make_outfit(db_session, test_user)
    worn, _ = await make_outfit(db_session, test_user)
    untouched, _ = await make_outfit(db_session, test_user)
    skipped, _ = await make_outfit(db_session, test_user)
    await client.post(
        f"/api/v1/outfits/{liked}/reaction", json={"action": "like"}, headers=auth_headers
    )
    await client.post(
        f"/api/v1/outfits/{worn}/reaction", json={"action": "wear"}, headers=auth_headers
    )
    await client.post(f"/api/v1/outfits/{skipped}/skip", headers=auth_headers)
    assert (
        await db_session.scalar(
            select(func.count(UserFeedback.id)).where(
                UserFeedback.outfit_id.in_([untouched, skipped])
            )
        )
        == 0
    )
    response = await client.get("/api/v1/outfits?worn_only=true", headers=auth_headers)
    assert response.status_code == 200
    assert {o["id"] for o in response.json()["outfits"]} == {str(worn)}
    # Bulk selection uses the same predicate, so selecting all worn cannot include likes.
    selected, total = await OutfitService(db_session).list_with_filters(
        OutfitListFilters(user_id=test_user.id, worn_only=True), 1, 50
    )
    assert total == 1
    assert {o.id for o in selected} == {worn}


@pytest.mark.asyncio
async def test_future_outfit_can_be_liked_but_not_marked_worn(
    client, auth_headers, db_session, test_user
):
    outfit_id, _ = await make_outfit(
        db_session, test_user, get_user_today(test_user) + timedelta(days=1)
    )
    url = f"/api/v1/outfits/{outfit_id}/reaction"
    assert (
        await client.post(url, json={"action": "wear"}, headers=auth_headers)
    ).status_code == 400
    assert (
        await client.post(url, json={"action": "like"}, headers=auth_headers)
    ).status_code == 200
    assert (
        await client.post(url, json={"action": "unknown"}, headers=auth_headers)
    ).status_code == 422
    assert (
        await client.post(
            f"/api/v1/outfits/{uuid4()}/reaction", json={"action": "like"}, headers=auth_headers
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_concurrent_accepts_record_a_single_wear(db_session, test_user):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api.outfits import ReactionRequest, react_to_outfit

    outfit_id, ids = await make_outfit(db_session, test_user)
    user = SimpleNamespace(id=test_user.id, timezone=test_user.timezone)
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def accept():
        async with sessions() as session:
            return await react_to_outfit(outfit_id, ReactionRequest(action="wear"), session, user)

    first, second = await asyncio.gather(accept(), accept())
    assert first.feedback.worn_at == second.feedback.worn_at == get_user_today(test_user)
    assert list(
        (
            await db_session.scalars(
                select(ClothingItem.wear_count).where(ClothingItem.id.in_(ids))
            )
        ).all()
    ) == [1, 1, 1]
