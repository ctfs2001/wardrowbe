"""Short suggestion batches: diagnostics, the 2/3 response and filling only the gap.

Isolated test database and a mocked AI provider only; nothing here calls a paid model.
"""

import asyncio
import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.models.item import ClothingItem, ItemStatus
from app.models.outfit import Outfit
from app.models.user import User
from app.services.recommendation_service import (
    MAX_FILL_ATTEMPTS,
    RecommendationService,
    SuggestionBatchBusyError,
    _with_option_count,
)
from app.utils.timezone import get_user_today
from tests.test_recommendation_quality_flow import reply

AI = "app.services.recommendation_service.AIService.generate_text"
NOTES = "Halloween picnic, private detail 4711"


async def wardrobe(db, user, shirts=4, pants=4):
    kinds = ["shirt"] * shirts + ["pants"] * pants + ["sneakers"]
    items = [
        ClothingItem(
            user_id=user.id,
            type=kind,
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            formality="casual",
        )
        for kind in kinds
    ]
    db.add_all(items)
    await db.commit()
    return {n: item.id for n, item in enumerate(items, 1)}


def prompt_of(call) -> str:
    return call.kwargs.get("prompt") or call.args[0]


def no_weather_lookup():
    """A fill must reuse the batch's weather snapshot, never fetch a new reading."""
    return (
        patch(
            "app.services.weather_service.WeatherService.get_planned_weather",
            AsyncMock(side_effect=AssertionError("weather refetched")),
        ),
        patch(
            "app.services.weather_service.WeatherService.get_current_weather",
            AsyncMock(side_effect=AssertionError("weather refetched")),
        ),
    )


def diagnostics(caplog, event):
    return [
        json.loads(r.getMessage().split(" ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith(event + " ")
    ]


async def suggest(client, auth_headers, numbers, replies, **payload):
    body = {
        "occasion": "casual",
        "wear_day": "tomorrow",
        "time_of_day": "evening",
        "activity_needs": ["walking"],
        "event_notes": NOTES,
        "weather_override": {"temperature": 19, "condition": "cloudy"},
        **payload,
    }
    weather = no_weather_lookup()
    with (
        weather[0],
        weather[1],
        patch.object(RecommendationService, "_format_items_for_prompt", return_value=("", numbers)),
        patch(AI, AsyncMock(side_effect=replies)) as generate,
    ):
        response = await client.post(
            "/api/v1/outfits/suggest-options", json=body, headers=auth_headers
        )
    return response, generate


async def fill(client, auth_headers, numbers, batch_id, replies):
    weather = no_weather_lookup()
    with (
        weather[0],
        weather[1],
        patch.object(
            RecommendationService, "_format_items_for_prompt", return_value=("", numbers)
        ) as formatter,
        patch(AI, AsyncMock(side_effect=replies)) as generate,
    ):
        response = await client.post(
            f"/api/v1/outfits/suggestion-batches/{batch_id}/fill", headers=auth_headers
        )
    return response, generate, formatter


async def outfit_count(db, user):
    return await db.scalar(
        select(func.count()).select_from(Outfit).where(Outfit.user_id == user.id)
    )


def test_option_count_follows_what_the_call_needs():
    base = "Respond with valid JSON containing exactly 3 distinct outfit options. Each must..."
    assert "exactly 1 distinct outfit option." in _with_option_count(base, 1)
    assert "exactly 3" not in _with_option_count(base, 1)
    assert "exactly 2 distinct outfit options." in _with_option_count(base, 2)


@pytest.mark.asyncio
async def test_three_valid_options_use_one_call_and_report_a_complete_batch(
    client, auth_headers, db_session, test_user, caplog
):
    numbers = await wardrobe(db_session, test_user)
    caplog.set_level(logging.INFO, logger="app.services.recommendation_diagnostics")
    response, generate = await suggest(
        client, auth_headers, numbers, [reply([[1, 5, 9], [2, 6, 9], [3, 7, 9]])]
    )

    assert response.status_code == 200, response.text
    looks = response.json()
    assert isinstance(looks, list) and len(looks) == 3
    assert generate.await_count == 1
    batch = looks[0]["suggestion_batch"]
    assert batch["target_count"] == 3 and batch["saved_count"] == 3
    assert batch["missing_count"] == 0 and batch["can_fill"] is False
    assert {look["suggestion_batch"]["id"] for look in looks} == {batch["id"]}
    [result] = diagnostics(caplog, "recommendation_result")
    assert result["request_id"] == batch["id"]
    assert result["outcome"] == "complete" and result["ai_calls"] == 1


@pytest.mark.asyncio
async def test_repair_completes_a_short_first_answer_and_asks_only_for_the_gap(
    client, auth_headers, db_session, test_user, caplog
):
    numbers = await wardrobe(db_session, test_user)
    caplog.set_level(logging.INFO, logger="app.services.recommendation_diagnostics")
    response, generate = await suggest(
        client,
        auth_headers,
        numbers,
        [reply([[1, 5, 9], [2, 6, 9]]), reply([[3, 7, 9]])],
    )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 3
    assert generate.await_count == 2
    repair = prompt_of(generate.await_args_list[1])
    assert "exactly 1 distinct outfit option." in repair
    assert "exactly 3 distinct" not in repair
    assert "Return exactly 1 new complete option" in repair
    [result] = diagnostics(caplog, "recommendation_result")
    assert result["outcome"] == "complete"
    assert result["reasons"] == ["model_short_response"]
    assert [a["phase"] for a in result["attempts"]] == ["recommendation", "recommendation_repair"]
    assert result["attempts"][0]["short_by"] == 1


@pytest.mark.asyncio
async def test_two_of_three_is_reported_with_traceable_reasons(
    client, auth_headers, db_session, test_user, caplog
):
    numbers = await wardrobe(db_session, test_user)
    caplog.set_level(logging.INFO, logger="app.services.recommendation_diagnostics")
    response, generate = await suggest(
        client,
        auth_headers,
        numbers,
        [
            # valid, valid, no shoes, repeats option one's top and bottom
            reply([[1, 5, 9], [2, 6, 9], [3, 7], [1, 5, 9]]),
            SimpleNamespace(content="not json at all", model="test", endpoint="test"),
        ],
    )

    assert response.status_code == 200, response.text
    looks = response.json()
    assert len(looks) == 2
    assert generate.await_count == 2  # one repair, never more
    batch = looks[0]["suggestion_batch"]
    assert batch == {
        "id": batch["id"],
        "target_count": 3,
        "saved_count": 2,
        "missing_count": 1,
        "can_fill": True,
        "filling": False,
        "expired": False,
        "fill_attempts_remaining": MAX_FILL_ATTEMPTS,
    }

    attempts = diagnostics(caplog, "recommendation_attempt")
    [result] = diagnostics(caplog, "recommendation_result")
    assert {a["request_id"] for a in attempts} == {result["request_id"]} == {batch["id"]}
    assert attempts[0]["returned"] == 4 and attempts[0]["approved"] == 2
    assert attempts[0]["rejected"] == {"duplicate_in_batch": 1, "footwear_count": 1}
    assert attempts[1]["rejected"] == {"parse_error": 1}
    assert result["outcome"] == "partial" and result["saved"] == 2
    assert result["reasons"] == ["duplicate_in_batch", "footwear_count", "parse_error"]

    # The same reasons stay with the saved options for later inspection.
    stored = await db_session.get(Outfit, UUID(looks[0]["id"]))
    assert stored.ai_raw_response["_batch"]["diagnostics"]["reasons"] == result["reasons"]

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "4711" not in log_text and "Authorization" not in log_text
    assert "not json at all" not in log_text


@pytest.mark.asyncio
async def test_ai_failure_is_its_own_reason_code(
    client, auth_headers, db_session, test_user, caplog
):
    numbers = await wardrobe(db_session, test_user)
    caplog.set_level(logging.INFO, logger="app.services.recommendation_diagnostics")
    response, generate = await suggest(
        client, auth_headers, numbers, [RuntimeError("provider down")]
    )
    assert response.status_code == 503
    assert generate.await_count == 1
    [result] = diagnostics(caplog, "recommendation_result")
    assert result["outcome"] == "failed" and result["reasons"] == ["ai_call_failed"]
    assert await outfit_count(db_session, test_user) == 0


@pytest.mark.asyncio
async def test_fill_adds_only_the_missing_option_and_keeps_existing_ones(
    client, auth_headers, db_session, test_user, caplog
):
    numbers = await wardrobe(db_session, test_user)
    base_shirt = numbers[1]
    response, _ = await suggest(
        client,
        auth_headers,
        numbers,
        [reply([[1, 5, 9], [1, 6, 9]]), reply([[1, 5, 9]])],
        include_items=[str(base_shirt)],
        exclude_items=[str(numbers[8])],
    )
    assert response.status_code == 200, response.text
    kept = response.json()
    assert len(kept) == 2
    batch_id = kept[0]["suggestion_batch"]["id"]

    liked = await client.post(
        f"/api/v1/outfits/{kept[0]['id']}/reaction", json={"action": "like"}, headers=auth_headers
    )
    assert liked.status_code == 200, liked.text
    before = {
        look["id"]: look
        for look in (
            await client.get(f"/api/v1/outfits/suggestion-batches/{batch_id}", headers=auth_headers)
        ).json()["outfits"]
    }

    caplog.set_level(logging.INFO, logger="app.services.recommendation_diagnostics")
    response, generate, formatter = await fill(
        client, auth_headers, numbers, batch_id, [reply([[1, 7, 9]])]
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert generate.await_count == 1
    assert body["batch"]["saved_count"] == 3 and body["batch"]["missing_count"] == 0
    assert body["batch"]["can_fill"] is False
    assert len(body["added_outfit_ids"]) == 1
    ids = [look["id"] for look in body["outfits"]]
    assert ids[:2] == [look["id"] for look in kept]
    for look in body["outfits"][:2]:
        old = before[look["id"]]
        assert look["items"] == old["items"]
        assert look["reasoning"] == old["reasoning"]
        assert look["feedback"] == old["feedback"]
        assert look["status"] == old["status"]
    assert before[kept[0]["id"]]["status"] == "accepted"
    assert before[kept[0]["id"]]["feedback"] is not None

    added = body["outfits"][2]
    assert added["id"] == body["added_outfit_ids"][0]
    # Same wearing date, period, weather, notes and base piece as the original request.
    for key in ("scheduled_for", "notes", "occasion"):
        assert added[key] == kept[0][key]
    assert added["weather"] == kept[0]["weather"]
    assert str(base_shirt) in {item["id"] for item in added["items"]}

    prompt = prompt_of(generate.await_args)
    assert NOTES in prompt and "長時間步行" in prompt
    assert "Wearing date: " + kept[0]["scheduled_for"] in prompt
    assert "exactly 1 distinct outfit option." in prompt and "exactly 3" not in prompt
    assert "ALREADY SAVED FOR THIS REQUEST" in prompt
    scored = formatter.call_args.args[0]
    assert numbers[8] not in {si.item.id for si in scored}  # excluded item stays excluded

    [result] = diagnostics(caplog, "recommendation_result")
    assert result["request_id"] == batch_id
    assert result["kind"] == "fill" and result["kept"] == 2 and result["outcome"] == "complete"
    assert await outfit_count(db_session, test_user) == 3


@pytest.mark.asyncio
async def test_fill_rejects_options_repeating_kept_core_pieces(
    client, auth_headers, db_session, test_user, caplog
):
    numbers = await wardrobe(db_session, test_user)
    response, _ = await suggest(
        client, auth_headers, numbers, [reply([[1, 5, 9], [2, 6, 9]]), reply([[1, 5, 9]])]
    )
    batch_id = response.json()[0]["suggestion_batch"]["id"]

    # Repeats a kept look, then reuses a kept top while unused tops remain.
    caplog.set_level(logging.INFO, logger="app.services.recommendation_diagnostics")
    response, generate, _ = await fill(
        client, auth_headers, numbers, batch_id, [reply([[1, 5, 9]]), reply([[2, 7, 9]])]
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "fill_no_valid_option"
    assert generate.await_count == 2
    [result] = diagnostics(caplog, "recommendation_result")
    assert result["request_id"] == batch_id and result["kind"] == "fill"
    assert result["outcome"] == "failed"
    assert result["reasons"] == ["duplicate_in_batch", "reused_key_piece"]
    assert await outfit_count(db_session, test_user) == 2


@pytest.mark.asyncio
async def test_failed_fill_keeps_existing_options_and_explains(
    client, auth_headers, db_session, test_user
):
    numbers = await wardrobe(db_session, test_user)
    response, _ = await suggest(
        client, auth_headers, numbers, [reply([[1, 5, 9], [2, 6, 9]]), reply([])]
    )
    kept_ids = [look["id"] for look in response.json()]
    batch_id = response.json()[0]["suggestion_batch"]["id"]

    response, generate, _ = await fill(
        client, auth_headers, numbers, batch_id, [RuntimeError("provider down")]
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_unavailable"
    assert response.json()["detail"]["message"]
    assert generate.await_count == 1

    current = await client.get(
        f"/api/v1/outfits/suggestion-batches/{batch_id}", headers=auth_headers
    )
    assert [look["id"] for look in current.json()["outfits"]] == kept_ids
    assert current.json()["batch"]["missing_count"] == 1
    assert current.json()["batch"]["can_fill"] is True


@pytest.mark.asyncio
async def test_repeated_fill_after_success_does_not_call_ai_or_save_again(
    client, auth_headers, db_session, test_user
):
    numbers = await wardrobe(db_session, test_user)
    response, _ = await suggest(
        client, auth_headers, numbers, [reply([[1, 5, 9], [2, 6, 9]]), reply([])]
    )
    batch_id = response.json()[0]["suggestion_batch"]["id"]
    first, _, _ = await fill(client, auth_headers, numbers, batch_id, [reply([[3, 7, 9]])])
    assert first.status_code == 200

    again, generate, _ = await fill(client, auth_headers, numbers, batch_id, [reply([[4, 8, 9]])])
    assert again.status_code == 200
    assert again.json()["added_outfit_ids"] == []
    assert [o["id"] for o in again.json()["outfits"]] == [o["id"] for o in first.json()["outfits"]]
    generate.assert_not_awaited()
    assert await outfit_count(db_session, test_user) == 3


@pytest.mark.asyncio
async def test_fill_while_another_runs_is_refused_without_ai(
    client, auth_headers, db_session, test_user
):
    numbers = await wardrobe(db_session, test_user)
    response, _ = await suggest(
        client, auth_headers, numbers, [reply([[1, 5, 9], [2, 6, 9]]), reply([])]
    )
    batch_id = UUID(response.json()[0]["suggestion_batch"]["id"])

    async with RecommendationService(db_session)._fill_lock(batch_id) as acquired:
        assert acquired
        busy, generate, _ = await fill(
            client, auth_headers, numbers, batch_id, [reply([[3, 7, 9]])]
        )
        status = await client.get(
            f"/api/v1/outfits/suggestion-batches/{batch_id}", headers=auth_headers
        )
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "fill_in_progress"
    generate.assert_not_awaited()
    assert status.json()["batch"]["filling"] is True
    assert status.json()["batch"]["can_fill"] is False
    assert await outfit_count(db_session, test_user) == 2


@pytest.mark.asyncio
async def test_concurrent_fills_save_the_missing_option_once(
    client, auth_headers, db_session, test_user, session_maker
):
    numbers = await wardrobe(db_session, test_user)
    response, _ = await suggest(
        client, auth_headers, numbers, [reply([[1, 5, 9], [2, 6, 9]]), reply([])]
    )
    batch_id = UUID(response.json()[0]["suggestion_batch"]["id"])

    async def slow_reply(*args, **kwargs):
        await asyncio.sleep(0.3)
        return reply([[3, 7, 9]])

    async def run():
        async with session_maker() as session:
            user = await session.scalar(
                select(User).where(User.id == test_user.id).options(selectinload(User.preferences))
            )
            try:
                _, added = await RecommendationService(session).fill_suggestion_batch(
                    user, batch_id
                )
                return len(added)
            except SuggestionBatchBusyError:
                return "busy"

    with (
        patch.object(RecommendationService, "_format_items_for_prompt", return_value=("", numbers)),
        patch(AI, AsyncMock(side_effect=slow_reply)) as generate,
    ):
        results = await asyncio.gather(run(), run())

    assert sorted(results, key=str) == [1, "busy"]
    assert generate.await_count == 1
    assert await outfit_count(db_session, test_user) == 3


@pytest.mark.asyncio
async def test_fill_limits_and_expiry_are_enforced_before_any_ai_call(
    client, auth_headers, db_session, test_user
):
    numbers = await wardrobe(db_session, test_user)
    response, _ = await suggest(
        client, auth_headers, numbers, [reply([[1, 5, 9], [2, 6, 9]]), reply([])]
    )
    batch_id = response.json()[0]["suggestion_batch"]["id"]

    with patch.object(RecommendationService, "_claim_fill_attempt", AsyncMock(return_value=False)):
        limited, generate, _ = await fill(
            client, auth_headers, numbers, batch_id, [reply([[3, 7, 9]])]
        )
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "fill_limit_reached"
    generate.assert_not_awaited()

    await db_session.execute(
        update(Outfit)
        .where(Outfit.user_id == test_user.id)
        .values(scheduled_for=get_user_today(test_user) - timedelta(days=1))
    )
    await db_session.commit()
    expired, generate, _ = await fill(client, auth_headers, numbers, batch_id, [reply([[3, 7, 9]])])
    assert expired.status_code == 409
    assert expired.json()["detail"]["code"] == "batch_expired"
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_or_foreign_batch_is_not_found(client, auth_headers):
    response = await client.post(
        f"/api/v1/outfits/suggestion-batches/{uuid4()}/fill", headers=auth_headers
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "batch_not_found"


@pytest.mark.asyncio
async def test_single_suggestion_does_not_start_a_batch(
    client, auth_headers, db_session, test_user
):
    numbers = await wardrobe(db_session, test_user)
    weather = no_weather_lookup()
    with (
        weather[0],
        weather[1],
        patch.object(RecommendationService, "_format_items_for_prompt", return_value=("", numbers)),
        patch(AI, AsyncMock(return_value=reply([[1, 5, 9], [2, 6, 9], [3, 7, 9]]))) as generate,
    ):
        response = await client.post(
            "/api/v1/outfits/suggest",
            json={
                "occasion": "casual",
                "event_notes": NOTES,
                "weather_override": {"temperature": 19, "condition": "cloudy"},
            },
            headers=auth_headers,
        )
    assert response.status_code == 200, response.text
    assert "suggestion_batch" not in response.json()
    assert generate.await_count == 1
    stored = await db_session.get(Outfit, UUID(response.json()["id"]))
    assert "_batch" not in stored.ai_raw_response
