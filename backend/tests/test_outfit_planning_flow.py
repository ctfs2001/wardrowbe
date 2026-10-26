from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app.services.recommendation_service import RecommendationService
from tests.test_item_scorer import _weather
from tests.test_recommendation_quality_flow import reply, setup_wardrobe


@pytest_asyncio.fixture(autouse=True)
async def planning_location(db_session, test_user):
    test_user.location_lat = 25
    test_user.location_lon = 121
    test_user.timezone = "Asia/Taipei"
    await db_session.commit()


@pytest.mark.parametrize("endpoint", ["suggest", "suggest-options"])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.asyncio
async def test_planned_request_reaches_weather_prompt_and_saved_outfit(
    client, auth_headers, db_session, test_user, endpoint, override
):
    numbers = await setup_wardrobe(db_session, test_user)
    payload = {
        "occasion": "casual",
        "wear_day": "tomorrow",
        "time_of_day": "morning",
        "activity_needs": ["walking", "indoor_ac"],
        "event_notes": "Lunch with friends",
    }
    if override:
        payload["weather_override"] = {"temperature": 18, "condition": "cloudy"}
    with (
        patch("app.utils.outfit_planning.get_user_today", return_value=date(2026, 10, 26)),
        patch(
            "app.services.weather_service.WeatherService.get_planned_weather",
            AsyncMock(return_value=_weather(temp=18)),
        ) as forecast,
        patch(
            "app.services.weather_service.WeatherService.get_current_weather", AsyncMock()
        ) as current,
        patch.object(RecommendationService, "_format_items_for_prompt", return_value=("", numbers)),
        patch(
            "app.services.recommendation_service.AIService.generate_text",
            AsyncMock(return_value=reply([[1, 4, 7], [2, 5, 7], [3, 6, 7]])),
        ) as generate,
    ):
        response = await client.post(
            f"/api/v1/outfits/{endpoint}", json=payload, headers=auth_headers
        )
    assert response.status_code == 200, response.text
    looks = response.json() if endpoint == "suggest-options" else [response.json()]
    for look in looks:
        assert look["scheduled_for"] == "2026-10-27"
        assert look["weather"]["time_of_day"] == "morning"
        assert look["weather"]["temperature"] == 18
        assert "長時間步行" in look["notes"]
        assert "冷氣房" in look["notes"]
        assert "Lunch with friends" in look["notes"]
    prompt = generate.call_args.kwargs.get("prompt") or generate.call_args.args[0]
    assert "2026-10-27" in prompt
    assert "長時間步行" in prompt
    current.assert_not_awaited()
    if override:
        forecast.assert_not_awaited()
    else:
        assert forecast.call_args.args[2:4] == (date(2026, 10, 27), "morning")


@pytest.mark.asyncio
async def test_weather_preview_resolves_tomorrow_and_selected_period(
    client, auth_headers, test_user
):
    with (
        patch("app.utils.outfit_planning.get_user_today", return_value=date(2026, 10, 26)),
        patch(
            "app.services.weather_service.WeatherService.get_planned_weather",
            AsyncMock(return_value=_weather()),
        ) as forecast,
    ):
        response = await client.get(
            "/api/v1/weather/current?wear_day=tomorrow&time_of_day=evening", headers=auth_headers
        )
    assert response.status_code == 200
    assert response.json()["target_date"] == "2026-10-27"
    assert forecast.call_args.args[2:4] == (date(2026, 10, 27), "evening")


@pytest.mark.asyncio
async def test_forecast_failure_stops_before_ai(client, auth_headers):
    from app.services.weather_service import WeatherServiceError

    with (
        patch(
            "app.services.weather_service.WeatherService.get_planned_weather",
            AsyncMock(side_effect=WeatherServiceError("No hourly forecast")),
        ),
        patch(
            "app.services.weather_service.WeatherService.get_current_weather", AsyncMock()
        ) as current,
        patch(
            "app.services.recommendation_service.AIService.generate_text", AsyncMock()
        ) as generate,
    ):
        preview = await client.get(
            "/api/v1/weather/current?wear_day=tomorrow&time_of_day=morning", headers=auth_headers
        )
        result = await client.post(
            "/api/v1/outfits/suggest-options",
            json={
                "occasion": "casual",
                "wear_day": "tomorrow",
                "time_of_day": "morning",
            },
            headers=auth_headers,
        )
    assert preview.status_code == 503
    assert result.status_code == 400
    generate.assert_not_awaited()
    current.assert_not_awaited()
