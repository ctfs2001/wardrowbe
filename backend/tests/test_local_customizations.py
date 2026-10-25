"""Tests for this fork's local changes to outfit suggestions (kept apart from upstream tests)."""

import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.item import ClothingItem, ItemStatus
from app.models.outfit import Outfit, OutfitItem, OutfitSource, OutfitStatus, TimeOfDay
from app.services.item_scorer import ScoredItem
from app.services.recommendation_service import (
    EVENT_NOTES_MAX,
    RecommendationService,
    _add_event_notes,
    _repeat_ok,
    _style_notes_line,
)
from app.services.weather_service import WeatherData
from app.utils.timezone import get_user_today


def _weather() -> WeatherData:
    return WeatherData(
        temperature=20,
        feels_like=20,
        humidity=50,
        precipitation_chance=0,
        precipitation_mm=0,
        wind_speed=0,
        condition="clear",
        condition_code=0,
        is_day=True,
        uv_index=0,
        timestamp=datetime.utcnow(),
    )


async def _items(db_session, user, kinds, **kwargs) -> list[ClothingItem]:
    items = [
        ClothingItem(
            user_id=user.id,
            type=kind,
            image_path=f"test/{uuid4()}.jpg",
            status=ItemStatus.ready,
            primary_color="black",
            **kwargs,
        )
        for kind in kinds
    ]
    db_session.add_all(items)
    await db_session.commit()
    return items


class TestEventNotesPrompt:
    def test_inserted_after_the_occasion_line(self):
        prompt = "Context:\n- Occasion: work\n- Time: morning\n"
        out = _add_event_notes(prompt, "client pitch\nblazer required")
        lines = out.splitlines()
        assert lines[1] == "- Occasion: work"
        assert lines[2].startswith("- EVENT DETAILS FROM THE USER")
        assert lines[2].endswith("client pitch blazer required")
        assert lines[3] == "- Time: morning"

    def test_prepended_when_there_is_no_occasion_line(self):
        out = _add_event_notes("Pick an outfit.", "wedding")
        assert out.startswith("- EVENT DETAILS FROM THE USER")
        assert out.endswith("\n\nPick an outfit.")


class TestStyleNotes:
    def test_empty_by_default(self):
        with patch("app.services.recommendation_service.get_settings") as settings:
            settings.return_value.style_notes = "  "
            assert _style_notes_line() == ""

    def test_set_on_the_host(self):
        with patch("app.services.recommendation_service.get_settings") as settings:
            settings.return_value.style_notes = "minimal,\n earth tones"
            assert _style_notes_line() == "\n- The user's own style: minimal, earth tones"


class TestRepeatOk:
    @pytest.mark.parametrize("kind", ["sneakers", "shoes", "boots", "hat", "belt", "accessories"])
    def test_shoes_and_accessories_may_repeat(self, kind):
        assert _repeat_ok(kind)

    @pytest.mark.parametrize("kind", ["shirt", "pants", "jacket", "scarf", None])
    def test_clothes_may_not(self, kind):
        assert not _repeat_ok(kind)


class TestWornNoteHidden:
    def test_only_clothes_get_the_worn_note(self):
        service = RecommendationService.__new__(RecommendationService)
        today = date(2026, 3, 8)
        shirt = ClothingItem(id=uuid4(), type="shirt", last_worn_at=today - timedelta(days=2))
        shoes = ClothingItem(id=uuid4(), type="sneakers", last_worn_at=today - timedelta(days=2))
        text, _ = service._format_items_for_prompt(
            [ScoredItem(item=shirt), ScoredItem(item=shoes)], {}, today
        )
        shirt_line, shoes_line = text.splitlines()[:2]
        assert "worn 2 days ago" in shirt_line
        assert "worn" not in shoes_line


class TestRepeatAndRejectRules:
    @pytest.mark.asyncio
    async def test_recently_worn_dates_cover_clothes_only(self, db_session, test_user):
        worn = date(2026, 3, 1)
        shirt, shoes, hat = await _items(
            db_session, test_user, ("shirt", "sneakers", "hat"), last_worn_at=worn
        )
        dates = await RecommendationService(db_session)._get_recently_worn_dates(test_user)
        assert dates == {shirt.id: worn}

    @pytest.mark.asyncio
    async def test_rejected_outfit_keeps_shoes_and_accessories_available(
        self, db_session, test_user
    ):
        shirt, pants, shoes, hat = await _items(
            db_session, test_user, ("shirt", "pants", "sneakers", "hat")
        )
        outfit = Outfit(
            user_id=test_user.id,
            occasion="casual",
            scheduled_for=get_user_today(test_user),
            status=OutfitStatus.rejected,
            source=OutfitSource.on_demand,
        )
        db_session.add(outfit)
        await db_session.flush()
        db_session.add_all(
            OutfitItem(outfit_id=outfit.id, item_id=i.id, position=n)
            for n, i in enumerate((shirt, pants, shoes, hat))
        )
        await db_session.commit()
        service = RecommendationService(db_session)
        candidates = await service.get_candidate_items(test_user, _weather(), "casual", None, [])
        assert {i.id for i in candidates} == {shirt.id, pants.id, shoes.id, hat.id}
        counts = await service._get_recent_suggestion_counts(test_user)
        assert counts == {i.id: 1 for i in (shirt, pants, shoes, hat)}


class TestShoesGuaranteed:
    async def _materialize(self, db_session, user, kinds, picked, **kwargs):
        items = await _items(db_session, user, kinds)
        number_map = {n: item.id for n, item in enumerate(items, start=1)}
        outfit = await RecommendationService(db_session)._materialize_outfit(
            {"items": picked},
            user,
            _weather(),
            occasion="casual",
            source=OutfitSource.on_demand,
            number_map=number_map,
            **kwargs,
        )
        return items, outfit

    @pytest.mark.asyncio
    async def test_best_ranked_footwear_is_added(self, db_session, test_user):
        items, outfit = await self._materialize(
            db_session, test_user, ("shirt", "pants", "boots", "sneakers"), [1, 2]
        )
        assert {oi.item_id for oi in outfit.items} == {items[0].id, items[1].id, items[2].id}

    @pytest.mark.asyncio
    async def test_picked_shoes_are_kept(self, db_session, test_user):
        items, outfit = await self._materialize(
            db_session, test_user, ("shirt", "pants", "boots", "sneakers"), [1, 2, 4]
        )
        assert {oi.item_id for oi in outfit.items} == {items[0].id, items[1].id, items[3].id}

    @pytest.mark.asyncio
    async def test_no_footwear_in_wardrobe_is_fine(self, db_session, test_user):
        items, outfit = await self._materialize(db_session, test_user, ("shirt", "pants"), [1, 2])
        assert {oi.item_id for oi in outfit.items} == {items[0].id, items[1].id}

    @pytest.mark.asyncio
    async def test_event_notes_are_saved_on_the_outfit(self, db_session, test_user):
        _, outfit = await self._materialize(
            db_session, test_user, ("shirt", "pants"), [1, 2], event_notes="wedding"
        )
        assert outfit.notes == "活動備註：wedding"


class TestEventNotesSuggestions:
    async def _generate(self, db_session, user, event_notes):
        items = await _items(db_session, user, ("shirt", "pants", "sneakers"))
        number_map = {n: item.id for n, item in enumerate(items, start=1)}
        user.preferences = None
        ai = MagicMock()
        ai.generate_text = AsyncMock(
            return_value=MagicMock(
                content=json.dumps({"outfits": [{"items": [1, 2, 3]}, {"items": [1, 2]}]}),
                model="m",
                endpoint="e",
            )
        )
        service = RecommendationService(db_session)
        with (
            patch("app.services.recommendation_service.AIService", return_value=ai),
            patch(
                "app.services.recommendation_service.pop_suggestion", AsyncMock(return_value=None)
            ) as pop,
            patch("app.services.recommendation_service.push_suggestions", AsyncMock()) as push,
            patch.object(service, "_format_items_for_prompt", return_value=("", number_map)),
        ):
            outfits = await service.generate_recommendations(
                user=user,
                occasion="casual",
                weather_override=_weather(),
                time_of_day=TimeOfDay.afternoon,
                event_notes=event_notes,
            )
        return outfits, ai.generate_text.await_args.args[0], pop, push

    @pytest.mark.asyncio
    async def test_notes_reach_the_prompt_and_skip_the_cache(self, db_session, test_user):
        outfits, prompt, pop, push = await self._generate(
            db_session, test_user, "  wedding, avoid all white  "
        )
        assert "EVENT DETAILS FROM THE USER" in prompt
        assert "[[STYLE_NOTES]]" not in prompt
        assert "wedding, avoid all white" in prompt
        pop.assert_not_awaited()
        push.assert_not_awaited()
        assert [o.notes for o in outfits] == ["活動備註：wedding, avoid all white"]

    @pytest.mark.asyncio
    async def test_long_notes_are_cut(self, db_session, test_user):
        outfits, _, _, _ = await self._generate(db_session, test_user, "x" * 900)
        assert outfits[0].notes == "活動備註：" + "x" * EVENT_NOTES_MAX

    @pytest.mark.asyncio
    async def test_without_notes_the_cache_is_used(self, db_session, test_user):
        outfits, prompt, pop, push = await self._generate(db_session, test_user, "   ")
        assert "EVENT DETAILS" not in prompt
        pop.assert_awaited()
        push.assert_not_awaited()  # Incomplete/duplicate spare looks must not be cached.
        assert outfits[0].notes is None
