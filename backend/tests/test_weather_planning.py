from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.api.outfits import SuggestRequest
from app.models.outfit import TimeOfDay
from app.services.recommendation_service import cached_suggestion_fits
from app.services.weather_service import WeatherService, WeatherServiceError
from app.utils.outfit_planning import activity_notes, wearing_date


def hourly_forecast():
    return {
        "hourly": {
            "time": [f"2026-10-27T{h:02d}:00" for h in range(24)],
            "temperature_2m": [10 + h for h in range(24)],
            "apparent_temperature": [9 + h for h in range(24)],
            "relative_humidity_2m": [60] * 24,
            "precipitation_probability": [0] * 23 + [90],
            "precipitation": [0] * 23 + [2],
            "weather_code": [0] * 23 + [61],
            "wind_speed_10m": [5] * 23 + [30],
            "is_day": [int(6 <= h < 18) for h in range(24)],
            "uv_index": [int(6 <= h < 18) * 4 for h in range(24)],
        }
    }


@pytest.mark.parametrize(
    "period,start,end",
    [
        (TimeOfDay.morning, 6, 12),
        (TimeOfDay.afternoon, 12, 18),
        (TimeOfDay.evening, 18, 24),
        (TimeOfDay.full_day, 6, 24),
    ],
)
@pytest.mark.asyncio
async def test_forecast_uses_selected_local_hours(period, start, end):
    response = httpx.Response(
        200, json=hourly_forecast(), request=httpx.Request("GET", "https://weather.test")
    )
    with patch("app.services.weather_service.httpx.AsyncClient") as client:
        get = client.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
        result = await WeatherService().get_planned_weather(
            25, 121, date(2026, 10, 27), period, "Asia/Taipei"
        )
    assert result.temperature == (10 + start + 10 + end - 1) / 2
    assert result.feels_like == result.temperature - 1
    assert (result.window_min, result.window_max) == (10 + start, 10 + end - 1)
    assert result.precipitation_chance == (90 if end == 24 else 0)
    assert result.wind_speed == (30 if end == 24 else 5)
    assert result.timestamp.isoformat() == f"2026-10-27T{start:02d}:00:00+08:00"
    params = get.call_args.kwargs["params"]
    assert params["start_date"] == params["end_date"] == "2026-10-27"
    assert params["timezone"] == "Asia/Taipei"


@pytest.mark.parametrize(
    "broken", ["missing_hour", "null", "wrong_date", "http_error", "invalid_json"]
)
@pytest.mark.asyncio
async def test_unavailable_forecast_never_falls_back_to_current(broken):
    data = hourly_forecast()
    if broken == "missing_hour":
        data["hourly"]["time"] = data["hourly"]["time"][:20]
    elif broken == "null":
        data["hourly"]["temperature_2m"][19] = None
    elif broken == "wrong_date":
        data["hourly"]["time"] = [t.replace("27", "26") for t in data["hourly"]["time"]]
    response = httpx.Response(
        503 if broken == "http_error" else 200,
        json=data,
        request=httpx.Request("GET", "https://weather.test"),
    )
    if broken == "invalid_json":
        response = httpx.Response(200, content=b"not json", request=response.request)
    service = WeatherService()
    with (
        patch("app.services.weather_service.httpx.AsyncClient") as client,
        patch.object(service, "get_current_weather", AsyncMock()) as current,
    ):
        client.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
        with pytest.raises(WeatherServiceError):
            await service.get_planned_weather(
                25, 121, date(2026, 10, 27), TimeOfDay.evening, "Asia/Taipei"
            )
    current.assert_not_awaited()


def test_today_and_tomorrow_use_user_timezone_across_utc_midnight():
    user = SimpleNamespace(timezone="Asia/Taipei")
    with patch("app.utils.timezone.datetime") as clock:
        clock.now.return_value = datetime(2026, 10, 26, 18, tzinfo=UTC)
        assert wearing_date(user, "today") == date(2026, 10, 27)
        assert wearing_date(user, "tomorrow") == date(2026, 10, 28)


def test_request_bounds_and_quick_needs():
    from pydantic import ValidationError

    for payload in [
        {"wear_day": "yesterday"},
        {"activity_needs": ["unknown"]},
        {"activity_needs": ["walking"] * 7},
    ]:
        with pytest.raises(ValidationError):
            SuggestRequest(**payload)
    assert activity_notes(["walking", "walking"]).count("長時間步行") == 1
    assert "不代表預報一定下雨" in activity_notes(["rain_ready"])


def test_planned_cache_cannot_cross_wearing_dates_or_legacy_requests():
    context = {"wearing_date": "2026-10-27", "temps": [20], "time_of_day": "morning"}
    assert cached_suggestion_fits(context, context, None)
    assert not cached_suggestion_fits(context, {**context, "wearing_date": "2026-10-28"}, None)
    assert not cached_suggestion_fits(context, {"temps": [20], "time_of_day": "morning"}, None)
