import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models.item import ClothingItem, ItemStatus
from app.models.outfit import (
    FamilyOutfitRating,
    Outfit,
    OutfitItem,
    OutfitSource,
    OutfitStatus,
    TimeOfDay,
    UserFeedback,
)
from app.models.preference import UserPreference
from app.models.user import User
from app.services.ai_service import AIResponseTruncatedError, AIService, require_internal_ai
from app.services.item_scorer import (
    get_season,
    rain_likely,
    score_items,
    scoring_temp_range,
    stays_above_hot_threshold,
    temperature_band,
)
from app.services.outfit_quality import core_items, validate_option
from app.services.recommendation_diagnostics import (
    AI_CALL_FAILED,
    AI_RESPONSE_TRUNCATED,
    PARSE_ERROR,
    RecommendationDiagnostics,
)
from app.services.recommendation_memory import recent_recommendation_memory
from app.services.suggestion_cache import pop_suggestion, push_suggestions
from app.services.weather_service import (
    GeocodingServiceError,
    WeatherData,
    WeatherService,
    WeatherServiceError,
)
from app.utils.clothing import canonical_item_order, deduplicate_by_body_slot, remove_legwear
from app.utils.garment_vocabulary import ITEM_ROLE, render_outfit_prompt
from app.utils.outfit_planning import ActivityNeed, activity_notes
from app.utils.preference_defaults import (
    DEFAULT_LAYERING_PREFERENCE,
    DEFAULT_TEMPERATURE_SENSITIVITY,
)
from app.utils.prompts import load_prompt
from app.utils.redis_lock import get_redis
from app.utils.timezone import get_user_now, get_user_timezone, get_user_today

logger = logging.getLogger(__name__)

# How far the temperature may drift before a cached option, picked by the AI for the old
# reading, is regenerated rather than served.
CACHED_SUGGESTION_TEMP_TOLERANCE = 3.0

SINGLE_OUTFIT_FORMAT = (
    "Respond with valid JSON:\n"
    '{{"items": [item numbers], "headline": "Short catchy outfit title (max 5 words)", '
    '"highlights": ["One short sentence each — vary your reasoning across color, texture, '
    'proportion, occasion, weather, or time of day"], '
    '"styling_tip": "One specific, actionable styling detail — do not suggest rolling up '
    'sleeves every time, vary your advice"}}'
)


# Explicit AI call budget. One generation request makes the initial call plus at most
# MAX_REPAIR_CALLS repairs; a suggestion batch accepts at most MAX_FILL_ATTEMPTS manual
# "fill the missing options" requests that reach the AI, each with the same budget.
MAX_REPAIR_CALLS = 1
MAX_FILL_ATTEMPTS = 3
FILL_ATTEMPTS_TTL_SECONDS = 2 * 24 * 3600

OPTION_COUNT_SENTENCE = "Respond with valid JSON containing exactly 3 distinct outfit options."


def _with_option_count(prompt: str, count: int) -> str:
    """Ask for the number of options this call needs.

    The template asks for three; a repair or fill that needs one must not also say
    "exactly 3" in the same prompt.
    """
    noun = "option" if count == 1 else "options"
    return prompt.replace(
        OPTION_COUNT_SENTENCE,
        f"Respond with valid JSON containing exactly {count} distinct outfit {noun}.",
    ).replace("across the 3 outfits", "across the outfits")


def _fill_lock_key(batch_id: UUID) -> int:
    # pg advisory locks take a signed bigint.
    return int.from_bytes(batch_id.bytes[:8], "big", signed=True)


def _weather_from_dict(data: dict) -> WeatherData:
    names = {f.name for f in fields(WeatherData)}
    values = {k: v for k, v in data.items() if k in names}
    values["timestamp"] = datetime.fromisoformat(values["timestamp"])
    return WeatherData(**values)


def _fill_attempts_key(batch_id: UUID) -> str:
    return f"suggest:fill-attempts:{batch_id}"


@dataclass
class SuggestionBatch:
    """Options generated together by one multi-option request.

    Stored without a schema change: every saved option carries the batch id, target and
    the original request snapshot under ``ai_raw_response["_batch"]``.
    """

    id: UUID
    target: int
    request: dict
    outfits: list[Outfit] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return max(self.target - len(self.outfits), 0)

    @property
    def wearing_date(self) -> date | None:
        dates = [o.scheduled_for for o in self.outfits if o.scheduled_for]
        return min(dates) if dates else None


def get_time_of_day(user: User) -> TimeOfDay:
    hour = get_user_now(user).hour
    if 6 <= hour < 12:
        return TimeOfDay.morning
    elif 12 <= hour < 17:
        return TimeOfDay.afternoon
    elif 17 <= hour < 21:
        return TimeOfDay.evening
    else:
        return TimeOfDay.night


def format_temp_range_text(weather: WeatherData) -> str:
    temp_range = scoring_temp_range(weather)
    if temp_range is None:
        return ""
    low, high = temp_range
    return (
        f"- Temperature ahead ranges from {low:.1f}°C to {high:.1f}°C, a large swing, "
        "favour adaptable layers that work at both ends."
    )


def suggestion_context(
    weather: WeatherData,
    time_of_day: TimeOfDay,
    preferences: UserPreference | None,
    body_measurements: dict | None,
) -> dict:
    return {
        "quality_version": 3,
        "memory_version": 1,
        "memory_day": datetime.now(UTC).date().isoformat(),
        "temps": [weather.temperature, weather.feels_like, *(scoring_temp_range(weather) or ())],
        "condition": weather.condition,
        "rain_likely": rain_likely(weather),
        "time_of_day": str(time_of_day),
        "preferences": str(preferences.updated_at) if preferences else None,
        # A digest, so that personal measurements are not copied into Redis.
        "body_measurements": hashlib.sha256(
            json.dumps(body_measurements, sort_keys=True).encode()
        ).hexdigest(),
    }


def cached_suggestion_fits(
    cached: dict | None, current: dict, preferences: UserPreference | None
) -> bool:
    if not cached or cached.get("wearing_date") != current.get("wearing_date"):
        return False
    if any(
        key not in cached or cached[key] != value
        for key, value in current.items()
        if key != "temps"
    ):
        return False
    then, now = cached.get("temps") or [], current["temps"]
    return len(then) == len(now) and all(
        abs(a - b) <= CACHED_SUGGESTION_TEMP_TOLERANCE
        and temperature_band(a, preferences) == temperature_band(b, preferences)
        for a, b in zip(then, now, strict=True)
    )


@dataclass
class RecommendationContext:
    user: User
    preferences: UserPreference | None
    weather: WeatherData
    occasion: str
    exclude_items: list[UUID]
    include_items: list[UUID]


RECOMMENDATION_PROMPT = render_outfit_prompt(load_prompt("recommendation"))

EVENT_NOTES_MAX = 500
EVENT_NOTES_PREFIX = "活動備註："

# Item types that may be worn again any day: the no-repeat window and today's rejections
# only apply to clothes, otherwise a small wardrobe runs out of shoes, hats and glasses.
_REPEAT_OK_TYPES = {"hat", "bag", "belt", "accessories"}


def _repeat_ok(item_type: str | None) -> bool:
    t = (item_type or "").lower()
    return t in _REPEAT_OK_TYPES or ITEM_ROLE.get(t) == "footwear"


def _style_notes_line() -> str:
    notes = " ".join(get_settings().style_notes.split())
    return f"\n- The user's own style: {notes}" if notes else ""


def _add_event_notes(prompt: str, notes: str) -> str:
    """Put the user's event notes right after the occasion line of the stylist prompt."""
    line = (
        "- EVENT DETAILS FROM THE USER (top priority; follow any dress code, venue, activity, "
        "color or formality requirement stated here, even over the generic occasion, and say in "
        "the highlights how the outfit fits it): " + notes.replace("\n", " ")
    )
    new = re.sub(r"(- Occasion: [^\n]*\n)", lambda m: m.group(1) + line + "\n", prompt, count=1)
    return new if new != prompt else line + "\n\n" + prompt


class RecommendationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.weather_service = WeatherService()

    async def get_candidate_items(
        self,
        user: User,
        weather: WeatherData,
        occasion: str,
        preferences: UserPreference | None,
        exclude_items: list[UUID],
    ) -> list[ClothingItem]:
        query = select(ClothingItem).where(
            and_(
                ClothingItem.user_id == user.id,
                ClothingItem.status == ItemStatus.ready,
                ClothingItem.is_archived.is_(False),
            )
        )

        result = await self.db.execute(query)
        items = list(result.scalars().all())

        if not items:
            return []

        items = [i for i in items if not i.needs_wash]
        items = [i for i in items if i.type and i.type != "unknown"]

        if exclude_items:
            exclude_set = set(exclude_items)
            items = [i for i in items if i.id not in exclude_set]

        if preferences and preferences.excluded_item_ids:
            excluded = set(preferences.excluded_item_ids)
            items = [i for i in items if i.id not in excluded]

        return items

    async def _get_recently_worn_dates(self, user: User) -> dict[UUID, date]:
        result = await self.db.execute(
            select(ClothingItem.id, ClothingItem.last_worn_at).where(
                and_(
                    ClothingItem.user_id == user.id,
                    ClothingItem.last_worn_at.is_not(None),
                )
            )
        )
        dates = {row[0]: row[1] for row in result.all()}
        if dates:
            rows = await self.db.execute(
                select(ClothingItem.id, ClothingItem.type).where(ClothingItem.id.in_(list(dates)))
            )
            for r in rows:
                if _repeat_ok(r.type):
                    dates.pop(r.id, None)
        return dates

    async def _get_recent_suggestion_counts(self, user: User) -> dict[UUID, int]:
        rows = await self.db.execute(
            select(OutfitItem.item_id, func.count(OutfitItem.outfit_id))
            .join(Outfit, OutfitItem.outfit_id == Outfit.id)
            .where(
                Outfit.user_id == user.id,
                Outfit.source != OutfitSource.manual,
                Outfit.created_at >= datetime.now(UTC) - timedelta(days=7),
            )
            .group_by(OutfitItem.item_id)
        )
        return dict(rows.all())

    async def _get_recently_worn_outfit_combinations(
        self, user: User, days: int = 7
    ) -> set[frozenset[UUID]]:
        if days <= 0:
            return set()

        user_today = get_user_today(user)
        cutoff_date = user_today - timedelta(days=days)

        query = (
            select(Outfit)
            .join(UserFeedback, Outfit.id == UserFeedback.outfit_id)
            .where(
                and_(
                    Outfit.user_id == user.id,
                    UserFeedback.worn_at >= cutoff_date,
                )
            )
            .options(selectinload(Outfit.items))
        )

        result = await self.db.execute(query)
        worn_outfits = list(result.scalars().all())

        combinations = set()
        for outfit in worn_outfits:
            item_ids = frozenset(outfit_item.item_id for outfit_item in outfit.items)
            if len(item_ids) >= 2:
                combinations.add(item_ids)

        logger.info(f"Found {len(combinations)} worn outfit combinations in last {days} days")
        return combinations

    async def _get_recently_suggested_outfit_combinations(
        self, user: User, hours: int = 12
    ) -> set[frozenset[UUID]]:
        if hours <= 0:
            return set()

        cutoff_time = datetime.now(UTC) - timedelta(hours=hours)

        query = (
            select(Outfit)
            .where(
                and_(
                    Outfit.user_id == user.id,
                    Outfit.created_at >= cutoff_time,
                )
            )
            .order_by(Outfit.created_at.desc())
            .limit(10)
            .options(selectinload(Outfit.items))
        )

        result = await self.db.execute(query)
        recent_outfits = list(result.scalars().all())

        combinations = set()
        for outfit in recent_outfits:
            item_ids = frozenset(outfit_item.item_id for outfit_item in outfit.items)
            if len(item_ids) >= 2:
                combinations.add(item_ids)

        return combinations

    def _format_items_for_prompt(
        self,
        scored_items: list,
        good_pairs: dict[UUID, list[UUID]],
        user_today: date,
    ) -> tuple[str, dict[int, UUID]]:
        lines = []
        number_map: dict[int, UUID] = {}

        items_with_numbers: list[tuple[int, ClothingItem]] = []
        for i, si in enumerate(scored_items, 1):
            item = si.item if hasattr(si, "item") else si
            number_map[i] = item.id
            items_with_numbers.append((i, item))

        exposure_counts = {
            si.item.id: si.recent_suggestions
            for si in scored_items
            if hasattr(si, "recent_suggestions")
        }
        id_to_number = {item.id: num for num, item in items_with_numbers}

        for num, item in items_with_numbers:
            parts = []

            item_type = item.type or "item"
            if item.subtype:
                parts.append(f"{item.subtype} ({item_type})")
            else:
                parts.append(item_type)

            if item.colors and len(item.colors) > 1:
                parts.append(f"colors: {', '.join(item.colors)}")
            elif item.primary_color:
                parts.append(item.primary_color)

            if item.pattern and item.pattern != "solid":
                parts.append(item.pattern)

            item_size = getattr(item, "size", None)
            if item_size:
                parts.append(f"size {item_size}")

            if item.material:
                parts.append(item.material)

            if item.formality:
                parts.append(item.formality)

            if item.style:
                parts.append(f"style: {', '.join(item.style)}")

            if item.season:
                parts.append(f"season: {', '.join(item.season)}")

            fit = item.tags.get("fit") if item.tags else None
            if fit:
                parts.append(f"{fit} fit")

            if item.name:
                parts.insert(0, f'"{item.name}"')

            # Recency annotation
            if item.last_worn_at:
                days_ago = (user_today - item.last_worn_at).days
                # Items that may repeat don't get the note, so the AI doesn't avoid them.
                if 0 <= days_ago <= 14 and not _repeat_ok(item.type):
                    parts.append(f"worn {days_ago} days ago")
            else:
                parts.append("never worn")

            if exposure_counts.get(item.id):
                parts.append(f"suggested {exposure_counts[item.id]} times in last 7 days")

            # Pair annotation
            partners = good_pairs.get(item.id, [])
            if partners:
                pair_nums = sorted([id_to_number[p] for p in partners if p in id_to_number])[:3]
                if pair_nums:
                    refs = ", ".join(f"[{n}]" for n in pair_nums)
                    parts.append(f"pairs well with: {refs}")

            line = f"[{num}] {' | '.join(parts)}"
            lines.append(line)

        return "\n".join(lines), number_map

    def _format_mandatory_items_section(
        self,
        mandatory_item_ids: list[UUID] | None,
        id_to_number: dict[int, UUID],
    ) -> str:
        if not mandatory_item_ids:
            return ""

        uuid_to_number = {v: k for k, v in id_to_number.items()}
        mandatory_numbers = []
        for item_id in mandatory_item_ids:
            if item_id in uuid_to_number:
                mandatory_numbers.append(uuid_to_number[item_id])

        if not mandatory_numbers:
            return ""

        mandatory_numbers_sorted = sorted(mandatory_numbers)
        mandatory_refs = ", ".join(f"[{n}]" for n in mandatory_numbers_sorted)
        return (
            f"MANDATORY ITEMS (MUST include in every outfit):\n"
            f"The following items are already selected and MUST appear in all outfit suggestions: {mandatory_refs}\n\n"
            f"Complete each outfit with complementary pieces from the available items above. "
            f"Every generated outfit option MUST include all mandatory items listed above ({mandatory_refs})."
        )

    def _format_preferences_for_prompt(
        self,
        preferences: UserPreference | None,
        learned_prefs: dict | None = None,
        worn_combinations: set[frozenset[UUID]] | None = None,
        number_map: dict[int, UUID] | None = None,
        occasion: str | None = None,
        body_measurements: dict | None = None,
    ) -> str:
        lines = []

        if body_measurements:
            m = body_measurements
            body_parts = []
            if m.get("height"):
                body_parts.append(f"height {m['height']}cm")
            if m.get("weight"):
                body_parts.append(f"weight {m['weight']}kg")
            if m.get("chest"):
                body_parts.append(f"chest {m['chest']}cm")
            if m.get("waist"):
                body_parts.append(f"waist {m['waist']}cm")
            if m.get("hips"):
                body_parts.append(f"hips {m['hips']}cm")
            if m.get("inseam"):
                body_parts.append(f"inseam {m['inseam']}cm")
            if m.get("shirt_size"):
                body_parts.append(f"shirt size {m['shirt_size']}")
            if m.get("pants_size"):
                body_parts.append(f"pants size {m['pants_size']}")
            if m.get("dress_size"):
                body_parts.append(f"dress size {m['dress_size']}")
            if m.get("shoe_size"):
                body_parts.append(f"shoe size {m['shoe_size']}")
            if body_parts:
                lines.append(f"- Body: {', '.join(body_parts)}")

        if preferences:
            if preferences.color_favorites:
                lines.append(f"- Favorite colors: {', '.join(preferences.color_favorites)}")
            if preferences.color_avoid:
                lines.append(f"- Colors to avoid: {', '.join(preferences.color_avoid)}")
            if preferences.style_profile:
                profile = preferences.style_profile
                strong = sorted(
                    [(k, v) for k, v in profile.items() if isinstance(v, (int, float)) and v > 60],
                    key=lambda x: x[1],
                    reverse=True,
                )
                weak = [k for k, v in profile.items() if isinstance(v, (int, float)) and v < 30]
                if strong:
                    desc = ", ".join(f"{k} ({v}%)" for k, v in strong)
                    lines.append(f"- Preferred styles: {desc}")
                if weak:
                    lines.append(f"- Less preferred styles: {', '.join(weak)}")
            if preferences.variety_level:
                lines.append(f"- Variety preference: {preferences.variety_level}")
            if (
                preferences.layering_preference
                and preferences.layering_preference != DEFAULT_LAYERING_PREFERENCE
            ):
                lines.append(f"- Layering preference: {preferences.layering_preference}")
            if (
                preferences.temperature_sensitivity
                and preferences.temperature_sensitivity != DEFAULT_TEMPERATURE_SENSITIVITY
            ):
                lines.append(
                    f"- Temperature sensitivity: {preferences.temperature_sensitivity} "
                    f"(user {'feels cold/hot easily' if preferences.temperature_sensitivity == 'high' else 'tolerates temperature extremes well'})"
                )

        if learned_prefs:
            if learned_prefs.get("learned_favorite_colors"):
                colors = learned_prefs["learned_favorite_colors"]
                lines.append(f"- Learned favorite colors (from feedback): {', '.join(colors)}")
            if learned_prefs.get("learned_avoid_colors"):
                colors = learned_prefs["learned_avoid_colors"]
                lines.append(f"- Learned colors to avoid (from feedback): {', '.join(colors)}")
            if learned_prefs.get("learned_preferred_styles"):
                styles = learned_prefs["learned_preferred_styles"]
                lines.append(f"- Learned preferred styles: {', '.join(styles)}")

            if occasion and learned_prefs.get("occasion_insights"):
                occ_data = learned_prefs["occasion_insights"].get(occasion)
                if occ_data:
                    pref_colors = occ_data.get("preferred_colors", [])
                    if pref_colors:
                        lines.append(f"- For {occasion}, user prefers: {', '.join(pref_colors)}")
                    success_rate = occ_data.get("success_rate")
                    if success_rate is not None and success_rate < 0.5:
                        lines.append(
                            f"- Low success rate for {occasion} outfits — try different approaches"
                        )

        if worn_combinations and number_map:
            uuid_to_number = {uuid: num for num, uuid in number_map.items()}
            worn_sets = []
            for combo in worn_combinations:
                numbers = sorted([uuid_to_number[uuid] for uuid in combo if uuid in uuid_to_number])
                if numbers:
                    worn_sets.append("[" + ", ".join(map(str, numbers)) + "]")
            if worn_sets:
                lines.append(
                    f"- Recently worn or suggested outfits (prefer variety, do not repeat identical combinations): {', '.join(worn_sets)}"
                )

        if lines:
            return "\nUSER PREFERENCES:\n" + "\n".join(lines)
        return ""

    async def _get_learned_preferences(self, user_id: UUID, occasion: str | None = None) -> dict:
        preferences, _ = await recent_recommendation_memory(self.db, user_id)
        return preferences

    async def _get_good_item_pairs(self, user_id: UUID) -> dict[UUID, list[UUID]]:
        _, pairs = await recent_recommendation_memory(self.db, user_id)
        return pairs

    def _parse_ai_response(self, content: str) -> dict:
        def strip_comments(json_str: str) -> str:
            json_str = re.sub(r"//[^\n]*", "", json_str)
            json_str = re.sub(r"/\*[\s\S]*?\*/", "", json_str)
            return json_str

        try:
            return json.loads(content.strip())
        except json.JSONDecodeError:
            pass

        try:
            return json.loads(strip_comments(content.strip()))
        except json.JSONDecodeError:
            pass

        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
        if json_match:
            extracted = json_match.group(1)
            try:
                return json.loads(extracted)
            except json.JSONDecodeError:
                pass
            try:
                return json.loads(strip_comments(extracted))
            except json.JSONDecodeError:
                pass

        start_idx = content.find("{")
        if start_idx != -1:
            brace_count = 0
            for i, char in enumerate(content[start_idx:], start_idx):
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        json_str = content[start_idx : i + 1]
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError:
                            pass
                        try:
                            return json.loads(strip_comments(json_str))
                        except json.JSONDecodeError:
                            break

        start_idx = content.find("[")
        if start_idx != -1:
            bracket_count = 0
            for i, char in enumerate(content[start_idx:], start_idx):
                if char == "[":
                    bracket_count += 1
                elif char == "]":
                    bracket_count -= 1
                    if bracket_count == 0:
                        json_str = content[start_idx : i + 1]
                        try:
                            result = json.loads(json_str)
                            if isinstance(result, list) and len(result) > 0:
                                if isinstance(result[0], dict):
                                    return result[0]
                                return {"items": result}
                            return result
                        except json.JSONDecodeError:
                            break

        # The reply can echo the user's event notes, so only its size goes to the normal log.
        logger.warning("Unparsable AI reply len=%d", len(content or ""))
        logger.debug("Unparsable AI reply: %r", content)
        raise ValueError(f"Could not parse AI response as JSON: {content[:200]}")

    def _parse_multi_outfit_response(self, content: str) -> list[dict]:
        parsed = self._parse_ai_response(content)

        if isinstance(parsed, dict) and "outfits" in parsed:
            outfits = parsed["outfits"]
            if isinstance(outfits, list) and outfits:
                return outfits

        if isinstance(parsed, list) and parsed:
            return parsed

        if isinstance(parsed, dict) and "items" in parsed:
            return [parsed]

        return [parsed]

    async def _ensure_footwear(
        self,
        valid_ids: list[UUID],
        item_type_map: dict[UUID, str],
        number_map: dict[int, UUID],
    ) -> tuple[list[UUID], dict[UUID, str]]:
        """Every outfit needs shoes: if none were picked, add the best-ranked footwear candidate.

        Runs after the empty-outfit check, so an option with nothing else left to wear is
        still skipped instead of becoming a shoes-only outfit.
        """
        if any(ITEM_ROLE.get(item_type_map.get(i, "")) == "footwear" for i in valid_ids):
            return valid_ids, item_type_map
        rows = await self.db.execute(
            select(ClothingItem.id, ClothingItem.type).where(
                ClothingItem.id.in_(list(number_map.values()))
            )
        )
        candidate_types = {r.id: (r.type or "").lower() for r in rows}
        # Lower number = ranked higher by the scorer.
        for n in sorted(number_map):
            item_id = number_map[n]
            if ITEM_ROLE.get(candidate_types.get(item_id, "")) == "footwear":
                logger.info(f"Adding footwear #{n} to outfit without shoes")
                return valid_ids + [item_id], {**item_type_map, item_id: candidate_types[item_id]}
        return valid_ids, item_type_map

    async def _materialize_outfit(
        self,
        outfit_data: dict,
        user: User,
        weather: WeatherData,
        occasion: str,
        source: OutfitSource,
        number_map: dict[int, UUID],
        scheduled_date: date | None = None,
        mandatory_item_ids: set[UUID] | None = None,
        drop_legwear: bool = False,
        event_notes: str | None = None,
        time_of_day: TimeOfDay | None = None,
    ) -> Outfit:
        selected_numbers = outfit_data.get("items", [])
        valid_ids = []

        for num in selected_numbers:
            try:
                num_int = int(num)
                if num_int in number_map:
                    valid_ids.append(number_map[num_int])
                else:
                    logger.warning(f"AI selected invalid item number: {num}")
            except (ValueError, TypeError):
                logger.warning(f"AI returned non-numeric item: {num}")

        seen = set()
        unique_ids = []
        for item_id in valid_ids:
            if item_id not in seen:
                seen.add(item_id)
                unique_ids.append(item_id)
        valid_ids = unique_ids

        # Ensure all mandatory items are included in valid_ids if they were candidates
        if mandatory_item_ids:
            mandatory_in_candidates = {
                item_id for item_id in mandatory_item_ids if item_id in number_map.values()
            }
            for mid in mandatory_in_candidates:
                if mid not in valid_ids:
                    logger.info(f"Adding missing mandatory item {mid} to outfit")
                    valid_ids.insert(0, mid)

        # Deduplicate by body slot (e.g. prevent shorts + pants)
        items_result = await self.db.execute(
            select(ClothingItem.id, ClothingItem.type).where(ClothingItem.id.in_(valid_ids))
        )
        item_type_map = {row.id: (row.type or "").lower() for row in items_result}
        valid_ids = deduplicate_by_body_slot(
            valid_ids, item_type_map, mandatory_item_ids=mandatory_item_ids
        )
        # Prompt wording alone does not keep small models from adding tights on a hot
        # day; tights the user asked for stay.
        if drop_legwear:
            valid_ids = remove_legwear(valid_ids, item_type_map, mandatory_item_ids or set())
        if not valid_ids:
            raise EmptyOutfitError("AI did not select any valid items")
        valid_ids, item_type_map = await self._ensure_footwear(valid_ids, item_type_map, number_map)
        valid_ids = canonical_item_order(valid_ids, item_type_map)

        reasoning = outfit_data.get("headline") or outfit_data.get("reasoning")
        style_notes = outfit_data.get("styling_tip") or outfit_data.get("style_notes")

        weather_snapshot = weather.to_dict()
        if scheduled_date and time_of_day:
            weather_snapshot["time_of_day"] = str(time_of_day)
            weather_snapshot["timezone"] = str(get_user_timezone(user))
        outfit = Outfit(
            user_id=user.id,
            occasion=occasion,
            weather_data=weather_snapshot,
            scheduled_for=scheduled_date or get_user_today(user),
            reasoning=reasoning,
            style_notes=style_notes,
            # Kept on the outfit so the event shows up later in the outfit history.
            notes=EVENT_NOTES_PREFIX + event_notes if event_notes else None,
            ai_raw_response=outfit_data,
            source=source,
            status=OutfitStatus.pending,
        )

        self.db.add(outfit)
        await self.db.flush()

        layers = outfit_data.get("layers", {})
        for position, item_id in enumerate(valid_ids):
            layer_type = None
            for layer_name, layer_id in layers.items():
                if layer_id == str(item_id):
                    layer_type = layer_name
                    break

            outfit_item = OutfitItem(
                outfit_id=outfit.id,
                item_id=item_id,
                position=position,
                layer_type=layer_type,
            )
            self.db.add(outfit_item)

        await self.db.commit()
        await self.db.refresh(outfit)

        result = await self.db.execute(
            select(Outfit)
            .where(Outfit.id == outfit.id)
            .options(
                selectinload(Outfit.items).selectinload(OutfitItem.item),
                selectinload(Outfit.feedback),
                selectinload(Outfit.family_ratings).selectinload(FamilyOutfitRating.user),
            )
        )
        outfit = result.scalar_one()

        logger.info(f"Created outfit {outfit.id} with {len(valid_ids)} items")
        return outfit

    async def generate_recommendations(
        self,
        user: User,
        occasion: str,
        weather_override: WeatherData | None = None,
        exclude_items: list[UUID] | None = None,
        include_items: list[UUID] | None = None,
        source: OutfitSource = OutfitSource.on_demand,
        time_of_day: TimeOfDay | None = None,
        count: int = 1,
        single_outfit: bool = False,
        scheduled_date: date | None = None,
        event_notes: str | None = None,
        activity_needs: list[ActivityNeed] | None = None,
        fill_batch: SuggestionBatch | None = None,
    ) -> list[Outfit]:
        """Generate, validate and save outfit options.

        A multi-option request becomes a suggestion batch: its saved options carry the
        request snapshot so a later fill can generate only the missing options. With
        ``fill_batch`` this generates ``count`` options for that batch, keeping its
        existing options and comparing new core pieces against them.
        """
        # Guard first so deferral is unconditional, before any location/weather work.
        require_internal_ai("text")

        exclude_items = exclude_items or []
        include_items = include_items or []

        if not time_of_day:
            time_of_day = get_time_of_day(user)

        # Determine cache eligibility before auto-merge
        event_notes = (event_notes or "").strip()[:EVENT_NOTES_MAX] or None
        request_event_notes = event_notes
        if activity_needs:
            event_notes = "\n".join(filter(None, [activity_notes(activity_needs), event_notes]))
        # Looks made for a specific event must not be cached and reused for a normal request.
        use_cache = (
            not exclude_items and not single_outfit and not event_notes and fill_batch is None
        )
        starts_batch = fill_batch is None and not single_outfit and count > 1
        request_id = fill_batch.id if fill_batch else uuid4()

        # Rejecting a combination does not blacklist its individual garments.

        if weather_override:
            weather = weather_override
        else:
            lat = float(user.location_lat) if user.location_lat is not None else None
            lon = float(user.location_lon) if user.location_lon is not None else None

            if (lat is None and lon is None) and user.location_name:
                try:
                    geocoded = await self.weather_service.geocode_location_name(user.location_name)
                except GeocodingServiceError as e:
                    logger.error(f"Geocoding failed for outfit generation: {e}")
                    raise ValueError(
                        "Could not resolve location. Please update your location in settings."
                    ) from e
                if geocoded:
                    lat, lon, _ = geocoded

            if lat is None or lon is None:
                raise ValueError("User location not set. Please set location in settings.")
            try:
                if scheduled_date:
                    weather = await self.weather_service.get_planned_weather(
                        lat, lon, scheduled_date, time_of_day, str(get_user_timezone(user))
                    )
                else:
                    weather = await self.weather_service.get_current_weather(lat, lon)
            except WeatherServiceError as e:
                logger.error(f"Weather service failed: {e}")
                raise ValueError(
                    "Could not fetch weather data. Please try again or provide weather manually."
                ) from e

        # Everything a fill needs to reproduce this request. Stored with the saved
        # options (the outfits already hold the notes), never logged.
        batch_request = (
            fill_batch.request
            if fill_batch
            else {
                "occasion": occasion,
                "time_of_day": time_of_day.value,
                "scheduled_date": scheduled_date.isoformat() if scheduled_date else None,
                "weather": weather.to_dict(),
                "event_notes": request_event_notes,
                "activity_needs": list(dict.fromkeys(activity_needs or [])),
                "include_items": [str(i) for i in include_items],
                "exclude_items": [str(i) for i in exclude_items],
                "source": source.value,
            }
        )

        preferences = user.preferences
        drop_legwear = stays_above_hot_threshold(weather, preferences)
        context = suggestion_context(
            weather, time_of_day, preferences, getattr(user, "body_measurements", None)
        )

        if scheduled_date:
            context["wearing_date"] = scheduled_date.isoformat()

        ai_endpoints = None
        if preferences and preferences.ai_endpoints:
            ai_endpoints = preferences.ai_endpoints
        ai_service = AIService(endpoints=ai_endpoints)

        # Get candidate items (hard exclusions only)
        candidates = await self.get_candidate_items(
            user=user,
            weather=weather,
            occasion=occasion,
            preferences=preferences,
            exclude_items=exclude_items,
        )

        # Force-include specific items
        if include_items:
            include_set = set(include_items)
            existing_ids = {item.id for item in candidates}
            missing_ids = include_set - existing_ids

            if missing_ids:
                result = await self.db.execute(
                    select(ClothingItem).where(
                        and_(
                            ClothingItem.id.in_(missing_ids),
                            ClothingItem.user_id == user.id,
                            ClothingItem.status == ItemStatus.ready,
                            ClothingItem.is_archived.is_(False),
                        )
                    )
                )
                forced_items = list(result.scalars().all())
                candidates.extend(forced_items)
                logger.info(f"Force-included {len(forced_items)} items in recommendation")

            unresolved = include_set - {item.id for item in candidates}
            if unresolved:
                raise ValueError("One or more selected items could not be found or are not ready.")

        if len(candidates) < 2:
            raise InsufficientWardrobeError(
                "Not enough items in wardrobe for recommendation. "
                "Please add more items or adjust filters."
            )

        candidate_map = {item.id: item for item in candidates}
        mandatory = set(include_items)
        recent_combinations = await self._get_recently_suggested_outfit_combinations(user)
        # Recent complete core combinations are excluded, rather than all their clothes.
        recent_cores = [set(core_items(set(combo), candidate_map)) for combo in recent_combinations]
        recent_cores = [combo for combo in recent_cores if combo]
        # Options the user already has from this batch: new ones must differ from them.
        kept_ids = [
            {oi.item_id for oi in outfit.items}
            for outfit in (fill_batch.outfits if fill_batch else [])
        ]

        # Check cache for pre-generated suggestions (used when count is 1)
        if use_cache and count == 1:
            candidate_ids = {item.id for item in candidates}
            while True:
                cached = await pop_suggestion(user.id, occasion, include_items=include_items)
                if not cached:
                    break
                if not cached_suggestion_fits(cached.pop("_context", None), context, preferences):
                    logger.info(
                        "Cached suggestion was made for other weather or settings, skipping"
                    )
                    continue
                cached_number_map = cached.pop("_number_map", {})
                # Only items that are still available, so _ensure_footwear can't add shoes
                # that were archived, sent to the wash or rejected since this was cached.
                number_map = {
                    int(k): UUID(v)
                    for k, v in cached_number_map.items()
                    if UUID(v) in candidate_ids
                }

                cached_item_ids = set()
                for num in cached.get("items", []):
                    try:
                        str_num = str(int(num))
                        if str_num in cached_number_map:
                            cached_item_ids.add(UUID(cached_number_map[str_num]))
                    except (ValueError, TypeError):
                        pass

                # Rejected items are covered too, because they were merged into exclude_items.
                if not cached_item_ids <= candidate_ids:
                    logger.info("Cached suggestion uses items no longer available, skipping")
                    continue

                _, problem = validate_option(
                    cached,
                    number_map,
                    candidate_map,
                    mandatory,
                    recent_cores,
                    drop_legwear=drop_legwear,
                )
                if problem:
                    logger.info("Skipping cached suggestion: %s", problem.code)
                    continue

                logger.info(f"Using cached suggestion for user {user.id}, occasion: {occasion}")
                try:
                    outfit = await self._materialize_outfit(
                        cached,
                        user,
                        weather,
                        occasion,
                        source,
                        number_map,
                        scheduled_date=scheduled_date,
                        time_of_day=time_of_day,
                        mandatory_item_ids=set(include_items) if include_items else None,
                        drop_legwear=drop_legwear,
                    )
                except EmptyOutfitError:
                    logger.info("Cached suggestion has nothing left to wear, skipping")
                    continue
                return [outfit]

        # Fetch scoring context
        recently_worn_dates = await self._get_recently_worn_dates(user)
        learned_prefs, good_pairs = await recent_recommendation_memory(self.db, user.id)
        if learned_prefs:
            logger.info(
                f"Using learned preferences for user {user.id}: {list(learned_prefs.keys())}"
            )

        recent_counts = await self._get_recent_suggestion_counts(user)

        # Score items (replaces old filter approach)
        user_today = get_user_today(user)
        lat = float(user.location_lat) if user.location_lat is not None else None
        current_season = get_season((scheduled_date or user_today).month, lat)
        scored = score_items(
            items=candidates,
            weather=weather,
            occasion=occasion,
            preferences=preferences,
            user_today=user_today,
            current_season=current_season,
            learned_prefs=learned_prefs,
            good_pairs=good_pairs,
            recently_worn_dates=recently_worn_dates,
            recent_suggestions=recent_counts,
            mandatory_item_ids=set(include_items) if include_items else None,
        )

        # Format enriched prompt
        items_text, number_map = self._format_items_for_prompt(scored, good_pairs, user_today)
        mandatory_items_section = self._format_mandatory_items_section(include_items, number_map)

        worn_combinations = await self._get_recently_worn_outfit_combinations(user, days=7)
        suggested_combinations = await self._get_recently_suggested_outfit_combinations(
            user, hours=12
        )
        avoid_combinations = worn_combinations | suggested_combinations

        preferences_text = self._format_preferences_for_prompt(
            preferences,
            learned_prefs,
            avoid_combinations,
            number_map,
            occasion=occasion,
            body_measurements=getattr(user, "body_measurements", None),
        )

        prompt = RECOMMENDATION_PROMPT.format(
            occasion=occasion,
            time_of_day=time_of_day,
            temperature=weather.temperature,
            feels_like=weather.feels_like,
            condition=weather.condition,
            precipitation_chance=weather.precipitation_chance,
            temp_range_text=format_temp_range_text(weather),
            preferences_text=preferences_text,
            items_text=items_text,
            mandatory_items_section=mandatory_items_section,
        )
        # Filled after format() so the template keeps the same placeholders as upstream.
        prompt = prompt.replace("[[STYLE_NOTES]]", _style_notes_line())
        if scheduled_date:
            prompt = prompt.replace(
                "- Time of day:",
                f"- Wearing date: {scheduled_date.isoformat()} ({get_user_timezone(user)}). "
                "Weather describes the wearing window, not the request time.\n- Time of day:",
                1,
            )
        if event_notes:
            prompt = _add_event_notes(prompt, event_notes)

        # For single_outfit mode (notifications), replace multi-outfit format
        if single_outfit:
            prompt = re.sub(
                r"Respond with valid JSON containing exactly 3.*$",
                SINGLE_OUTFIT_FORMAT,
                prompt,
                flags=re.DOTALL,
            )

        if kept_ids:
            prompt += (
                "\nALREADY SAVED FOR THIS REQUEST: the user keeps these options. New options must "
                "use different tops, bottoms, one-piece garments or suits (selected mandatory "
                "pieces may repeat): "
                f"{json.dumps(self._numbers_for(kept_ids, number_map))}."
            )

        target = 1 if single_outfit else count
        diagnostics = RecommendationDiagnostics(
            request_id=request_id,
            kind="fill" if fill_batch else ("options" if starts_batch else "single"),
            target=target,
            kept=len(kept_ids),
        )
        logger.info(
            "Generating recommendation request_id=%s user=%s occasion=%s candidates=%d "
            "target=%d kept=%d",
            request_id,
            user.id,
            occasion,
            len(scored),
            target,
            len(kept_ids),
        )

        try:
            approved = await self._select_options(
                ai_service,
                prompt,
                target=target,
                # The template asks for three: a single /suggest still gets spare options
                # for the cache. A fill asks only for what is missing.
                want=target if (fill_batch or single_outfit) else max(target, 3),
                stop_at=max(target, 3) if use_cache else target,
                number_map=number_map,
                candidate_map=candidate_map,
                mandatory=mandatory,
                previous=[*recent_cores, *kept_ids],
                kept_ids=kept_ids,
                drop_legwear=drop_legwear,
                variety_pool={
                    si.item.id
                    for si in scored
                    if si.weather_score >= 0.6 and si.formality_score >= 0.5
                },
                diagnostics=diagnostics,
                task_name="recommendation_fill" if fill_batch else "recommendation",
            )
            if not approved:
                raise NoValidOptionError(
                    "Could not build a complete, distinct outfit from the available items. "
                    "Try changing selected items or the occasion."
                )

            batch_meta = None
            if starts_batch or fill_batch:
                batch_meta = {
                    "id": str(request_id),
                    "target": fill_batch.target if fill_batch else target,
                    "request": batch_request,
                    "diagnostics": diagnostics.summary(saved=min(len(approved), target)),
                }
            first_position = len(fill_batch.outfits) if fill_batch else 0
            outfits = []
            for position, option in enumerate(approved[:target], first_position):
                if batch_meta:
                    option = {**option, "_batch": {**batch_meta, "position": position}}
                outfits.append(
                    await self._materialize_outfit(
                        option,
                        user,
                        weather,
                        occasion,
                        source,
                        number_map,
                        scheduled_date=scheduled_date,
                        time_of_day=time_of_day,
                        mandatory_item_ids=mandatory,
                        drop_legwear=drop_legwear,
                        event_notes=event_notes,
                    )
                )
            diagnostics.finish(saved=len(outfits))
            if use_cache and len(approved) > target:
                serializable_map = {str(k): str(v) for k, v in number_map.items()}
                to_cache = [
                    {**od, "_number_map": serializable_map, "_context": context}
                    for od in approved[target:]
                ]
                await push_suggestions(user.id, occasion, to_cache, include_items=include_items)
            return outfits

        except AIRecommendationError:
            diagnostics.finish(saved=0)
            raise
        except AIResponseTruncatedError as e:
            # Preserve the specific, actionable cause instead of the generic message
            # below — the AI endpoint responded successfully, it just didn't produce
            # any output content (see AIResponseTruncatedError for why).
            diagnostics.finish(saved=0)
            logger.error(f"AI recommendation failed: {e}")
            raise AIRecommendationError(str(e)) from e
        except Exception as e:
            diagnostics.finish(saved=0)
            logger.error(f"AI recommendation failed: {e}")
            raise AIRecommendationError(
                "AI service is not available. Please check your AI endpoint configuration in Settings."
            ) from e

    @staticmethod
    def _numbers_for(id_sets: list[set[UUID]], number_map: dict[int, UUID]) -> list[list[int]]:
        uuid_to_number = {v: k for k, v in number_map.items()}
        return [sorted(uuid_to_number[i] for i in ids if i in uuid_to_number) for ids in id_sets]

    async def _select_options(
        self,
        ai_service: AIService,
        prompt: str,
        *,
        target: int,
        want: int,
        stop_at: int,
        number_map: dict[int, UUID],
        candidate_map: dict[UUID, ClothingItem],
        mandatory: set[UUID],
        previous: list[set[UUID]],
        kept_ids: list[set[UUID]],
        drop_legwear: bool,
        variety_pool: set[UUID],
        diagnostics: RecommendationDiagnostics,
        task_name: str,
    ) -> list[dict]:
        """Ask the configured provider for options and keep only those passing every check.

        Makes the initial call plus at most MAX_REPAIR_CALLS repairs. Validates everything
        before anything is persisted: failed options never enter outfit history. Each repair
        asks for exactly the number still missing.
        """
        approved: list[dict] = []
        approved_ids: list[set[UUID]] = []
        previous = list(previous)
        errors: list[str] = []
        requested, phase = want, task_name
        request_prompt = _with_option_count(prompt, want)
        for attempt in range(1 + MAX_REPAIR_CALLS):
            record = diagnostics.start_attempt(phase, requested)
            try:
                result = await ai_service.generate_text(
                    request_prompt, return_metadata=True, task_name=phase
                )
            except Exception as exc:
                diagnostics.ai_failed(
                    record,
                    AI_RESPONSE_TRUNCATED
                    if isinstance(exc, AIResponseTruncatedError)
                    else AI_CALL_FAILED,
                )
                if not approved:
                    raise
                logger.warning(
                    "Repair failed; returning validated options request_id=%s",
                    diagnostics.request_id,
                    exc_info=True,
                )
                break
            diagnostics.ai_answered(record, result.model)
            logger.info(
                f"AI recommendation generated (model: {result.model}, endpoint: {result.endpoint})"
            )
            logger.debug(f"AI raw response: {result.content[:500]}")

            try:
                options = self._parse_multi_outfit_response(result.content)
            except (ValueError, TypeError) as exc:
                options = []
                errors.append(str(exc)[:200])
                record.rejected[PARSE_ERROR] += 1
            record.returned = len(options)
            for option in options:
                ids, problem = validate_option(
                    option,
                    number_map,
                    candidate_map,
                    mandatory,
                    previous,
                    drop_legwear=drop_legwear,
                    batch_previous=[*kept_ids, *approved_ids],
                    variety_pool=variety_pool,
                )
                if problem:
                    errors.append(problem)
                    record.rejected[problem.code] += 1
                    continue
                option = dict(option)
                option["_ai_model"] = result.model
                option["_ai_endpoint"] = result.endpoint
                approved.append(option)
                approved_ids.append(ids)
                previous.append(ids)
                record.approved += 1
                if len(approved) >= stop_at:
                    break
            diagnostics.finish_attempt(record)
            if len(approved) >= target or attempt == MAX_REPAIR_CALLS:
                break
            requested = target - len(approved)
            phase = f"{task_name}_repair"
            accepted = [od["items"] for od in approved] + self._numbers_for(kept_ids, number_map)
            noun = "option" if requested == 1 else "options"
            request_prompt = _with_option_count(prompt, requested) + (
                "\nQUALITY CHECK: Generate replacement options only. "
                f'Return exactly {requested} new complete {noun} in the "outfits" array. '
                "Each explanation must describe its exact selected pieces. "
                f"Already accepted (do not repeat): {json.dumps(accepted)}. "
                f"Fix these problems: {'; '.join(dict.fromkeys(errors))}."
            )
        return approved

    async def generate_recommendation(
        self,
        user: User,
        occasion: str,
        weather_override: WeatherData | None = None,
        exclude_items: list[UUID] | None = None,
        include_items: list[UUID] | None = None,
        source: OutfitSource = OutfitSource.on_demand,
        time_of_day: TimeOfDay | None = None,
        single_outfit: bool = False,
        scheduled_date: date | None = None,
        event_notes: str | None = None,
        activity_needs: list[ActivityNeed] | None = None,
    ) -> Outfit:
        outfits = await self.generate_recommendations(
            user=user,
            occasion=occasion,
            weather_override=weather_override,
            exclude_items=exclude_items,
            include_items=include_items,
            source=source,
            time_of_day=time_of_day,
            count=1,
            single_outfit=single_outfit,
            scheduled_date=scheduled_date,
            event_notes=event_notes,
            activity_needs=activity_needs,
        )
        return outfits[0]

    async def get_suggestion_batch(self, user: User, batch_id: UUID) -> SuggestionBatch | None:
        """Load a batch's saved options, whatever their status or feedback, oldest first."""
        result = await self.db.execute(
            select(Outfit)
            .where(
                Outfit.user_id == user.id,
                Outfit.ai_raw_response[("_batch", "id")].astext == str(batch_id),
            )
            .options(
                selectinload(Outfit.items).selectinload(OutfitItem.item),
                selectinload(Outfit.feedback),
                selectinload(Outfit.family_ratings).selectinload(FamilyOutfitRating.user),
            )
            .execution_options(populate_existing=True)
        )
        outfits = list(result.scalars().all())
        if not outfits:
            return None
        outfits.sort(key=lambda o: (o.ai_raw_response["_batch"].get("position", 0), o.created_at))
        meta = outfits[0].ai_raw_response["_batch"]
        return SuggestionBatch(
            id=batch_id, target=int(meta["target"]), request=meta["request"], outfits=outfits
        )

    @asynccontextmanager
    async def _fill_lock(self, batch_id: UUID) -> AsyncIterator[bool]:
        """Try to take a batch's advisory lock on its own connection, without waiting.

        Transaction scoped: released when the fill finishes or fails, and by PostgreSQL if
        the connection drops, so a crashed fill can never block the batch.
        """
        async with self.db.bind.connect() as conn, conn.begin():
            acquired = await conn.scalar(
                select(func.pg_try_advisory_xact_lock(_fill_lock_key(batch_id)))
            )
            yield bool(acquired)

    async def is_filling(self, batch_id: UUID) -> bool:
        async with self._fill_lock(batch_id) as acquired:
            return not acquired

    async def fill_attempts_remaining(self, batch_id: UUID) -> int | None:
        """None when the counter cannot be read."""
        try:
            redis = await get_redis()
            used = int(await redis.get(_fill_attempts_key(batch_id)) or 0)
        except Exception:
            logger.warning("Fill attempt counter unavailable request_id=%s", batch_id)
            return None
        return max(MAX_FILL_ATTEMPTS - used, 0)

    async def _claim_fill_attempt(self, batch_id: UUID) -> bool:
        try:
            redis = await get_redis()
            pipe = redis.pipeline(transaction=True)
            pipe.incr(_fill_attempts_key(batch_id))
            pipe.expire(_fill_attempts_key(batch_id), FILL_ATTEMPTS_TTL_SECONDS)
            used, _ = await pipe.execute()
        except Exception:
            # Same stance as the request rate limiter; each fill still has its own call budget.
            logger.warning("Fill attempt counter unavailable, allowing request_id=%s", batch_id)
            return True
        return int(used) <= MAX_FILL_ATTEMPTS

    async def fill_suggestion_batch(
        self, user: User, batch_id: UUID
    ) -> tuple[SuggestionBatch, list[Outfit]]:
        """Generate only the options a batch is missing, from its original request.

        Existing options, their ids, content and feedback are never touched. Concurrent or
        repeated requests for the same batch are serialised by an advisory lock: one runs,
        others get SuggestionBatchBusyError, and a retry after success finds the batch
        complete and returns it without calling the AI.
        """
        async with self._fill_lock(batch_id) as acquired:
            if not acquired:
                raise SuggestionBatchBusyError("These outfits are already being completed.")
            batch = await self.get_suggestion_batch(user, batch_id)
            if batch is None:
                raise SuggestionBatchNotFoundError("Outfit suggestions not found.")
            if not batch.missing:
                logger.info("Suggestion batch already complete request_id=%s", batch_id)
                return batch, []
            if batch.wearing_date and batch.wearing_date < get_user_today(user):
                raise SuggestionBatchExpiredError(
                    "These suggestions are for a past date. Generate new outfits instead."
                )
            require_internal_ai("text")
            if not await self._claim_fill_attempt(batch_id):
                raise FillLimitReachedError(
                    "No more attempts to complete these outfits. Generate new outfits instead."
                )
            request = batch.request
            added = await self.generate_recommendations(
                user=user,
                occasion=request["occasion"],
                weather_override=_weather_from_dict(request["weather"]),
                exclude_items=[UUID(i) for i in request.get("exclude_items") or []],
                include_items=[UUID(i) for i in request.get("include_items") or []],
                source=OutfitSource(request.get("source") or OutfitSource.on_demand),
                time_of_day=TimeOfDay(request["time_of_day"]),
                count=batch.missing,
                scheduled_date=date.fromisoformat(request["scheduled_date"])
                if request.get("scheduled_date")
                else None,
                event_notes=request.get("event_notes"),
                activity_needs=request.get("activity_needs") or None,
                fill_batch=batch,
            )
            return await self.get_suggestion_batch(user, batch_id) or batch, added


class InsufficientWardrobeError(Exception):
    pass


class AIRecommendationError(Exception):
    pass


class NoValidOptionError(AIRecommendationError):
    """The AI answered, but no option passed the quality checks."""


class SuggestionBatchError(Exception):
    code = "suggestion_batch_error"


class SuggestionBatchNotFoundError(SuggestionBatchError):
    code = "batch_not_found"


class SuggestionBatchBusyError(SuggestionBatchError):
    code = "fill_in_progress"


class SuggestionBatchExpiredError(SuggestionBatchError):
    code = "batch_expired"


class FillLimitReachedError(SuggestionBatchError):
    code = "fill_limit_reached"


# An outfit with nothing left to wear after cleanup; one bad option among several, or a
# stale cached one, is skipped rather than failing the whole request.
class EmptyOutfitError(AIRecommendationError):
    pass
