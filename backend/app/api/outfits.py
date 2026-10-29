import logging
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, computed_field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.pagination import PaginationParams
from app.config import get_settings
from app.database import DbSession
from app.models.item import ClothingItem
from app.models.outfit import (
    FamilyOutfitRating,
    Outfit,
    OutfitItem,
    OutfitStatus,
    TimeOfDay,
    UserFeedback,
)
from app.models.user import User
from app.schemas.outfit import (
    MAX_AUTHORING_TEXT_LENGTH,
    Occasion,
    OutfitAttributeFields,
    stored_occasion_or_default,
)
from app.services.ai_service import AIDisabledError
from app.services.external_outfit_service import ExternalOutfitService
from app.services.item_service import ItemService
from app.services.learning_service import LearningService
from app.services.outfit_service import OutfitListFilters, OutfitService
from app.services.recommendation_service import (
    MAX_FILL_ATTEMPTS,
    AIRecommendationError,
    FillLimitReachedError,
    InsufficientWardrobeError,
    NoValidOptionError,
    RecommendationService,
    SuggestionBatch,
    SuggestionBatchBusyError,
    SuggestionBatchError,
    SuggestionBatchExpiredError,
    SuggestionBatchNotFoundError,
)
from app.services.studio_service import (
    ItemOwnershipError,
    OutfitNotTemplateError,
    OutfitWornImmutableError,
    StudioService,
)
from app.services.suggestion_cache import clear_suggestions
from app.services.weather_service import WeatherData
from app.utils.auth import get_current_user
from app.utils.outfit_planning import ActivityNeed, WearDay, wearing_date
from app.utils.rate_limit import rate_limit_by_user
from app.utils.scales import RATING_MAX, RATING_MIN
from app.utils.signed_urls import sign_optional
from app.utils.timezone import get_user_today

logger = logging.getLogger(__name__)

_RATING_RANGE = f"{RATING_MIN}-{RATING_MAX}"
MAX_SEARCH_OCCASIONS = 50


router = APIRouter(prefix="/outfits", tags=["Outfits"])


class WeatherOverrideRequest(BaseModel):
    temperature: float = Field(description="Temperature in Celsius")
    feels_like: float | None = Field(None, description="Feels like temperature")
    condition: str = Field(default="unknown", description="Weather condition")
    precipitation_chance: int = Field(default=0, ge=0, le=100)
    humidity: int = Field(default=50, ge=0, le=100)


def _default_occasion(user: User) -> str:
    return stored_occasion_or_default(
        user.preferences.default_occasion if user.preferences else None
    )


class SuggestRequest(BaseModel):
    wear_day: WearDay | None = None
    activity_needs: list[ActivityNeed] = Field(default_factory=list, max_length=6)
    occasion: Occasion | None = None
    time_of_day: TimeOfDay | None = None
    weather_override: WeatherOverrideRequest | None = None
    exclude_items: list[UUID] = Field(default_factory=list, description="Items to exclude")
    include_items: list[UUID] = Field(default_factory=list, description="Items to include")
    event_notes: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional event, venue or dress code; the stylist AI gives it priority",
    )


class OutfitItemResponse(BaseModel):
    id: UUID
    type: str
    subtype: str | None = None
    name: str | None = None
    primary_color: str | None = None
    colors: list[str] = []
    image_path: str | None = None
    thumbnail_path: str | None = None
    layer_type: str | None = None
    position: int

    @computed_field
    @property
    def image_url(self) -> str | None:
        return sign_optional(self.image_path)

    @computed_field
    @property
    def thumbnail_url(self) -> str | None:
        return sign_optional(self.thumbnail_path)


class WoreInsteadItem(BaseModel):
    id: UUID
    type: str
    name: str | None = None
    thumbnail_path: str | None = None

    @computed_field
    @property
    def thumbnail_url(self) -> str | None:
        return sign_optional(self.thumbnail_path)


class FeedbackSummary(BaseModel):
    rating: int | None = None
    comment: str | None = None
    worn_at: date | None = None
    actually_worn: bool | None = None
    wore_instead_items: list[WoreInsteadItem] | None = None


class FamilyRatingRequest(BaseModel):
    rating: int = Field(ge=RATING_MIN, le=RATING_MAX, description=f"Rating {_RATING_RANGE}")
    comment: str | None = Field(None, max_length=500)


class FamilyRatingResponse(BaseModel):
    id: UUID
    user_id: UUID
    user_display_name: str
    user_avatar_url: str | None = None
    rating: int
    comment: str | None = None
    created_at: datetime


class OutfitResponse(BaseModel):
    id: UUID
    occasion: str
    scheduled_for: date | None = None
    status: str
    name: str | None = None
    replaces_outfit_id: UUID | None = None
    cloned_from_outfit_id: UUID | None = None
    source: str
    reasoning: str | None = None
    style_notes: str | None = None
    season: str | None = None
    formality: str | None = None
    palette: list[str] | None = None
    notes: str | None = None
    highlights: list[str] | None = None
    weather: dict | None = None
    items: list[OutfitItemResponse]
    feedback: FeedbackSummary | None = None
    family_ratings: list[FamilyRatingResponse] | None = None
    family_rating_average: float | None = None
    family_rating_count: int | None = None
    is_starter_suggestion: bool = False
    created_at: datetime


class SuggestionBatchInfo(BaseModel):
    """How many of the requested options one suggest-options request produced.

    Older clients ignore it; the options themselves stay a plain list.
    """

    id: UUID
    target_count: int
    saved_count: int
    missing_count: int
    can_fill: bool
    filling: bool = False
    expired: bool = False
    fill_attempts_remaining: int | None = None


class SuggestedOutfitResponse(OutfitResponse):
    suggestion_batch: SuggestionBatchInfo | None = None


class SuggestionBatchResponse(BaseModel):
    batch: SuggestionBatchInfo
    outfits: list[OutfitResponse]
    added_outfit_ids: list[UUID] = []


class OutfitListResponse(BaseModel):
    outfits: list[OutfitResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


class ReactionRequest(BaseModel):
    action: Literal["wear", "like", "dislike"]


class BulkOutfitFilters(BaseModel):
    worn_only: bool | None = None
    status_filter: str | None = None
    occasion: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    source: str | None = None
    is_lookbook: bool | None = None
    is_replacement: bool | None = None
    has_source_item: bool | None = None
    item_type: str | None = None
    search: str | None = None
    occasions: Annotated[list[str] | None, Field(max_length=MAX_SEARCH_OCCASIONS)] = None
    cloned_from_outfit_id: UUID | None = None


class BulkDeleteOutfitsRequest(BaseModel):
    # Explicit selection
    outfit_ids: list[UUID] | None = None

    # Select all with exceptions
    select_all: bool = False
    excluded_ids: list[UUID] | None = None
    filters: BulkOutfitFilters | None = None

    # Cursor into a select_all walk; see BulkSelectionRequest in schemas/item.py
    # for why a capped select_all is resumed rather than rejected.
    after_id: UUID | None = None

    def model_post_init(self, __context):
        if not self.select_all and not self.outfit_ids:
            raise ValueError("Either outfit_ids or select_all=True must be provided")
        if self.select_all and self.outfit_ids:
            raise ValueError("Cannot use both outfit_ids and select_all")


class BulkDeleteOutfitsResponse(BaseModel):
    deleted: int
    failed: int
    errors: list[str] = Field(default_factory=list)
    next_cursor: UUID | None = None
    has_more: bool = False


class FeedbackRequest(BaseModel):
    accepted: bool | None = Field(None, description="Whether outfit was accepted")
    rating: int | None = Field(
        None, ge=RATING_MIN, le=RATING_MAX, description=f"Overall rating {_RATING_RANGE}"
    )
    comfort_rating: int | None = Field(
        None, ge=RATING_MIN, le=RATING_MAX, description=f"Comfort rating {_RATING_RANGE}"
    )
    style_rating: int | None = Field(
        None, ge=RATING_MIN, le=RATING_MAX, description=f"Style rating {_RATING_RANGE}"
    )
    comment: str | None = Field(None, max_length=1000, description="Optional comment")
    worn: bool | None = Field(None, description="Whether the outfit was worn")
    worn_with_modifications: bool | None = Field(
        None, description="If worn, whether modifications were made"
    )
    modification_notes: str | None = Field(None, max_length=500)
    actually_worn: bool | None = Field(
        None, description="Did user actually wear this recommendation?"
    )
    wore_instead_items: list[UUID] | None = Field(
        None, description="Item IDs user wore instead of recommendation"
    )


class FeedbackResponse(BaseModel):
    id: UUID
    outfit_id: UUID
    accepted: bool | None = None
    rating: int | None = None
    comfort_rating: int | None = None
    style_rating: int | None = None
    comment: str | None = None
    worn_at: date | None = None
    worn_with_modifications: bool = False
    modification_notes: str | None = None
    actually_worn: bool | None = None
    wore_instead_items: list[UUID] | None = None
    created_at: datetime


async def fetch_wore_instead_items_map(
    db: AsyncSession, outfits: list[Outfit], user_id: UUID | None = None
) -> dict[str, list[WoreInsteadItem]]:
    all_item_ids: set[UUID] = set()
    outfit_to_item_ids: dict[str, list[str]] = {}

    for outfit in outfits:
        if outfit.feedback and outfit.feedback.wore_instead_items:
            item_ids: list[str] = []
            for item_data in outfit.feedback.wore_instead_items:
                try:
                    if isinstance(item_data, dict):
                        item_id = item_data.get("item_id", "")
                    else:
                        item_id = str(item_data)
                    if item_id:
                        item_ids.append(item_id)
                        all_item_ids.add(UUID(item_id))
                except (ValueError, TypeError, KeyError):
                    continue
            outfit_to_item_ids[str(outfit.id)] = item_ids

    if not all_item_ids:
        return {}

    query = select(ClothingItem).where(ClothingItem.id.in_(all_item_ids))
    if user_id is not None:
        query = query.where(ClothingItem.user_id == user_id)
    result = await db.execute(query)
    items_by_id = {str(item.id): item for item in result.scalars().all()}

    wore_instead_map: dict[str, list[WoreInsteadItem]] = {}
    for outfit_id, item_ids in outfit_to_item_ids.items():
        wore_items = []
        for item_id in item_ids:
            if item_id in items_by_id:
                item = items_by_id[item_id]
                wore_items.append(
                    WoreInsteadItem(
                        id=item.id,
                        type=item.type,
                        name=item.name,
                        thumbnail_path=item.thumbnail_path,
                    )
                )
        if wore_items:
            wore_instead_map[outfit_id] = wore_items

    return wore_instead_map


def outfit_to_response(
    outfit: Outfit,
    wore_instead_items_map: dict[str, list[WoreInsteadItem]] | None = None,
    is_starter_suggestion: bool = False,
) -> OutfitResponse:
    items = []
    for outfit_item in sorted(outfit.items, key=lambda x: x.position):
        item = outfit_item.item
        items.append(
            OutfitItemResponse(
                id=item.id,
                type=item.type,
                subtype=item.subtype,
                name=item.name,
                primary_color=item.primary_color,
                colors=item.colors or [],
                image_path=item.image_path,
                thumbnail_path=item.thumbnail_path,
                layer_type=outfit_item.layer_type,
                position=outfit_item.position,
            )
        )

    feedback_summary = None
    if outfit.feedback:
        wore_instead = None
        if wore_instead_items_map and str(outfit.id) in wore_instead_items_map:
            wore_instead = wore_instead_items_map[str(outfit.id)]
        feedback_summary = FeedbackSummary(
            rating=outfit.feedback.rating,
            comment=outfit.feedback.comment,
            worn_at=outfit.feedback.worn_at,
            actually_worn=outfit.feedback.actually_worn,
            wore_instead_items=wore_instead,
        )

    highlights = None
    if outfit.ai_raw_response and isinstance(outfit.ai_raw_response, dict):
        raw_highlights = outfit.ai_raw_response.get("highlights")
        if raw_highlights and isinstance(raw_highlights, list):
            highlights = raw_highlights

    family_ratings_list = None
    family_rating_average = None
    family_rating_count = None
    if hasattr(outfit, "family_ratings") and outfit.family_ratings:
        family_ratings_list = [
            FamilyRatingResponse(
                id=r.id,
                user_id=r.user_id,
                user_display_name=(r.user.display_name or r.user.email) if r.user else "Unknown",
                user_avatar_url=r.user.avatar_url if r.user else None,
                rating=r.rating,
                comment=r.comment,
                created_at=r.created_at,
            )
            for r in outfit.family_ratings
        ]
        family_rating_count = len(outfit.family_ratings)
        if family_rating_count > 0:
            family_rating_average = (
                sum(r.rating for r in outfit.family_ratings) / family_rating_count
            )

    return OutfitResponse(
        id=outfit.id,
        occasion=outfit.occasion,
        scheduled_for=outfit.scheduled_for,
        status=outfit.status.value,
        name=outfit.name,
        replaces_outfit_id=outfit.replaces_outfit_id,
        cloned_from_outfit_id=outfit.cloned_from_outfit_id,
        source=outfit.source.value,
        reasoning=outfit.reasoning,
        style_notes=outfit.style_notes,
        season=outfit.season,
        formality=outfit.formality,
        palette=outfit.palette,
        notes=outfit.notes,
        highlights=highlights,
        weather=outfit.weather_data,
        items=items,
        feedback=feedback_summary,
        family_ratings=family_ratings_list,
        family_rating_average=family_rating_average,
        family_rating_count=family_rating_count,
        is_starter_suggestion=is_starter_suggestion,
        created_at=outfit.created_at,
    )


@router.post("/suggest", response_model=OutfitResponse)
async def suggest_outfit(
    request: SuggestRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    await rate_limit_by_user(str(current_user.id), "suggest", max_requests=10, window_seconds=60)
    weather_override = None
    if request.weather_override:
        w = request.weather_override
        weather_override = WeatherData(
            temperature=w.temperature,
            feels_like=w.feels_like or w.temperature,
            humidity=w.humidity,
            precipitation_chance=w.precipitation_chance,
            precipitation_mm=0,
            wind_speed=0,
            condition=w.condition,
            condition_code=0,
            is_day=True,
            uv_index=0,
            timestamp=datetime.utcnow(),
        )

    service = RecommendationService(db)

    occasion = request.occasion or _default_occasion(current_user)

    try:
        outfit = await service.generate_recommendation(
            user=current_user,
            occasion=occasion,
            weather_override=weather_override,
            exclude_items=request.exclude_items,
            include_items=request.include_items,
            time_of_day=request.time_of_day or (TimeOfDay.full_day if request.wear_day else None),
            scheduled_date=wearing_date(current_user, request.wear_day)
            if request.wear_day
            else None,
            activity_needs=request.activity_needs,
            event_notes=request.event_notes,
        )
    except InsufficientWardrobeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None
    except AIDisabledError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal AI is disabled; outfit suggestions are deferred to an external agent.",
        ) from None
    except AIRecommendationError as e:
        logger.error(f"AI recommendation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from None
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None

    item_service = ItemService(db)
    total_items = await item_service.get_ready_item_count(current_user.id)
    is_starter = total_items <= 5

    wore_instead_map = await fetch_wore_instead_items_map(db, [outfit], user_id=current_user.id)
    return outfit_to_response(outfit, wore_instead_map, is_starter_suggestion=is_starter)


@router.post("/suggest-options", response_model=list[SuggestedOutfitResponse])
async def suggest_outfit_options(
    request: SuggestRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[SuggestedOutfitResponse]:
    await rate_limit_by_user(str(current_user.id), "suggest", max_requests=10, window_seconds=60)
    weather_override = None
    if request.weather_override:
        w = request.weather_override
        weather_override = WeatherData(
            temperature=w.temperature,
            feels_like=w.feels_like or w.temperature,
            humidity=w.humidity,
            precipitation_chance=w.precipitation_chance,
            precipitation_mm=0,
            wind_speed=0,
            condition=w.condition,
            condition_code=0,
            is_day=True,
            uv_index=0,
            timestamp=datetime.utcnow(),
        )

    service = RecommendationService(db)

    occasion = request.occasion or _default_occasion(current_user)

    try:
        outfits = await service.generate_recommendations(
            user=current_user,
            occasion=occasion,
            weather_override=weather_override,
            exclude_items=request.exclude_items,
            include_items=request.include_items,
            time_of_day=request.time_of_day or (TimeOfDay.full_day if request.wear_day else None),
            scheduled_date=wearing_date(current_user, request.wear_day)
            if request.wear_day
            else None,
            activity_needs=request.activity_needs,
            count=3,
            event_notes=request.event_notes,
        )
    except InsufficientWardrobeError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None
    except AIDisabledError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal AI is disabled; outfit suggestions are deferred to an external agent.",
        ) from None
    except AIRecommendationError as e:
        logger.error(f"AI recommendation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        ) from None
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from None

    item_service = ItemService(db)
    total_items = await item_service.get_ready_item_count(current_user.id)
    is_starter = total_items <= 5

    wore_instead_map = await fetch_wore_instead_items_map(db, outfits, user_id=current_user.id)
    batch = _batch_of(outfits)
    info = _batch_info(current_user, batch, attempts_remaining=MAX_FILL_ATTEMPTS) if batch else None
    return [
        SuggestedOutfitResponse(
            **dict(outfit_to_response(outfit, wore_instead_map, is_starter_suggestion=is_starter)),
            suggestion_batch=info,
        )
        for outfit in outfits
    ]


def _batch_of(outfits: list[Outfit]) -> SuggestionBatch | None:
    meta = (outfits[0].ai_raw_response or {}).get("_batch") if outfits else None
    if not isinstance(meta, dict):
        return None
    return SuggestionBatch(
        id=UUID(meta["id"]), target=int(meta["target"]), request={}, outfits=outfits
    )


def _batch_info(
    user: User,
    batch: SuggestionBatch,
    *,
    attempts_remaining: int | None,
    filling: bool = False,
) -> SuggestionBatchInfo:
    expired = bool(batch.wearing_date and batch.wearing_date < get_user_today(user))
    return SuggestionBatchInfo(
        id=batch.id,
        target_count=batch.target,
        saved_count=len(batch.outfits),
        missing_count=batch.missing,
        can_fill=bool(batch.missing) and not filling and not expired and attempts_remaining != 0,
        filling=filling,
        expired=expired,
        fill_attempts_remaining=attempts_remaining,
    )


async def _batch_response(
    db: AsyncSession,
    service: RecommendationService,
    user: User,
    batch: SuggestionBatch,
    added: list[Outfit] | None = None,
) -> SuggestionBatchResponse:
    filling = await service.is_filling(batch.id) if batch.missing else False
    remaining = await service.fill_attempts_remaining(batch.id) if batch.missing else None
    wore_instead_map = await fetch_wore_instead_items_map(db, batch.outfits, user_id=user.id)
    return SuggestionBatchResponse(
        batch=_batch_info(user, batch, attempts_remaining=remaining, filling=filling),
        outfits=[outfit_to_response(o, wore_instead_map) for o in batch.outfits],
        added_outfit_ids=[o.id for o in added or []],
    )


_BATCH_ERROR_STATUS = {
    SuggestionBatchNotFoundError: status.HTTP_404_NOT_FOUND,
    SuggestionBatchBusyError: status.HTTP_409_CONFLICT,
    SuggestionBatchExpiredError: status.HTTP_409_CONFLICT,
    FillLimitReachedError: status.HTTP_429_TOO_MANY_REQUESTS,
}


def _coded_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


@router.get("/suggestion-batches/{batch_id}", response_model=SuggestionBatchResponse)
async def get_suggestion_batch(
    batch_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> SuggestionBatchResponse:
    """Current options of one suggest-options request, e.g. after a fill timed out."""
    service = RecommendationService(db)
    batch = await service.get_suggestion_batch(current_user, batch_id)
    if batch is None:
        raise _coded_error(
            status.HTTP_404_NOT_FOUND, "batch_not_found", "Outfit suggestions not found."
        )
    return await _batch_response(db, service, current_user, batch)


@router.post("/suggestion-batches/{batch_id}/fill", response_model=SuggestionBatchResponse)
async def fill_suggestion_batch(
    batch_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> SuggestionBatchResponse:
    """Generate only the missing options of a batch from its saved request snapshot.

    Safe to repeat: a complete batch is returned without calling the AI, and a fill
    already running for the batch answers 409 fill_in_progress.
    """
    await rate_limit_by_user(str(current_user.id), "suggest", max_requests=10, window_seconds=60)
    service = RecommendationService(db)
    try:
        batch, added = await service.fill_suggestion_batch(current_user, batch_id)
    except SuggestionBatchError as e:
        raise _coded_error(_BATCH_ERROR_STATUS[type(e)], e.code, str(e)) from None
    except AIDisabledError:
        raise _coded_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ai_disabled",
            "Internal AI is disabled; outfit suggestions are deferred to an external agent.",
        ) from None
    except NoValidOptionError as e:
        raise _coded_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "fill_no_valid_option", str(e)
        ) from None
    except AIRecommendationError as e:
        logger.error(f"AI recommendation error while filling batch {batch_id}: {e}")
        raise _coded_error(status.HTTP_503_SERVICE_UNAVAILABLE, "ai_unavailable", str(e)) from None
    except (InsufficientWardrobeError, ValueError) as e:
        raise _coded_error(status.HTTP_400_BAD_REQUEST, "request_invalid", str(e)) from None
    return await _batch_response(db, service, current_user, batch, added)


class SuggestionCreateRequest(OutfitAttributeFields):
    model_config = ConfigDict(extra="forbid")

    items: list[UUID] = Field(min_length=1, max_length=20)
    occasion: Occasion
    name: Annotated[str | None, Field(max_length=100)] = None
    scheduled_for: date | None = Field(
        default=None, description="Defaults to the user's current date"
    )
    reasoning: Annotated[str | None, Field(max_length=MAX_AUTHORING_TEXT_LENGTH)] = None
    style_notes: Annotated[str | None, Field(max_length=MAX_AUTHORING_TEXT_LENGTH)] = None


@router.post("/suggestions", response_model=OutfitResponse, status_code=status.HTTP_201_CREATED)
async def create_external_suggestion(
    request: SuggestionCreateRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    """Persist an externally-authored suggestion; available regardless of the AI flags."""
    await rate_limit_by_user(
        str(current_user.id), "external_suggestion", max_requests=20, window_seconds=60
    )

    service = ExternalOutfitService(db)
    try:
        outfit = await service.create_suggestion(
            user=current_user,
            item_ids=request.items,
            occasion=request.occasion,
            name=request.name,
            scheduled_for=request.scheduled_for,
            reasoning=request.reasoning,
            style_notes=request.style_notes,
            season=request.season,
            formality=request.formality,
            palette=request.palette,
            notes=request.notes,
        )
    except ItemOwnershipError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "OUTFIT_ITEM_OWNERSHIP",
                "message": "One or more items do not belong to you",
            },
        ) from None

    await db.commit()
    # No learning pass here, unlike the studio path: that one synthesizes accepted feedback, while
    # an authored suggestion lands pending and unrated, so process_feedback would return early.
    # Learning fires when the user actually accepts or rates it.
    await clear_suggestions(current_user.id, request.occasion)

    full = await service.get_full_outfit(outfit.id)
    return outfit_to_response(full)


@router.get("", response_model=OutfitListResponse)
async def list_outfits(
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
    pagination: Annotated[PaginationParams, Depends()],
    status_filter: str | None = Query(None, alias="status"),
    occasion: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    family_member_id: UUID | None = Query(None, description="View a family member's outfits"),
    source: str | None = Query(None, description="Comma-separated source enum filter"),
    is_lookbook: bool | None = Query(None, description="true for templates only"),
    is_replacement: bool | None = Query(None),
    has_source_item: bool | None = Query(None),
    item_type: str | None = Query(None),
    source_type: str | None = Query(
        None, description="Legacy alias for item_type used by /pairings"
    ),
    search: str | None = Query(None, max_length=50),
    occasions: str | None = Query(
        None, max_length=500, description="Comma-separated occasion keys the search also matches"
    ),
    worn_only: bool | None = Query(None, description="Filter actual wear records, not likes"),
    cloned_from_outfit_id: UUID | None = Query(
        None, description="Filter to wear instances of a specific template"
    ),
) -> OutfitListResponse:
    service = OutfitService(db)

    target_user_id = current_user.id
    if family_member_id:
        target_user_id = await service.verify_family_access(current_user, family_member_id)

    filters = OutfitListFilters(
        user_id=target_user_id,
        status_filter=status_filter,
        worn_only=worn_only,
        occasion=occasion,
        date_from=date_from,
        date_to=date_to,
        source=source,
        is_lookbook=is_lookbook,
        is_replacement=is_replacement,
        has_source_item=has_source_item,
        item_type=item_type or source_type,
        family_member_view=family_member_id is not None,
        search=search,
        occasions=[key.strip() for key in occasions.split(",") if key.strip()]
        if occasions
        else None,
        cloned_from_outfit_id=cloned_from_outfit_id,
    )

    outfits, total = await service.list_with_filters(filters, pagination.page, pagination.page_size)

    wore_instead_map = await fetch_wore_instead_items_map(db, outfits, user_id=current_user.id)

    outfit_responses = [outfit_to_response(o, wore_instead_map) for o in outfits]

    return OutfitListResponse(
        outfits=outfit_responses,
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
        has_more=pagination.has_more(total),
    )


@router.post("/bulk/delete", response_model=BulkDeleteOutfitsResponse)
async def bulk_delete_outfits(
    request: BulkDeleteOutfitsRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> BulkDeleteOutfitsResponse:
    service = OutfitService(db)
    deleted = 0
    failed = 0
    errors: list[str] = []
    limit = get_settings().max_bulk_action_count
    next_cursor: UUID | None = None
    has_more = False

    if request.select_all:
        list_filters = OutfitListFilters(
            user_id=current_user.id,
            status_filter=request.filters.status_filter if request.filters else None,
            occasion=request.filters.occasion if request.filters else None,
            date_from=request.filters.date_from if request.filters else None,
            date_to=request.filters.date_to if request.filters else None,
            source=request.filters.source if request.filters else None,
            worn_only=request.filters.worn_only if request.filters else None,
            is_lookbook=request.filters.is_lookbook if request.filters else None,
            is_replacement=request.filters.is_replacement if request.filters else None,
            has_source_item=request.filters.has_source_item if request.filters else None,
            item_type=request.filters.item_type if request.filters else None,
            search=request.filters.search if request.filters else None,
            occasions=request.filters.occasions if request.filters else None,
            cloned_from_outfit_id=request.filters.cloned_from_outfit_id
            if request.filters
            else None,
        )
        outfit_ids = await service.get_ids_by_filter(
            list_filters,
            excluded_ids=list(request.excluded_ids) if request.excluded_ids else None,
            after_id=request.after_id,
            limit=limit + 1,
        )
        has_more = len(outfit_ids) > limit
        outfit_ids = outfit_ids[:limit]
        next_cursor = outfit_ids[-1] if has_more and outfit_ids else None
        logger.info(f"Bulk delete select_all: {len(outfit_ids)} outfits, has_more={has_more}")
    else:
        outfit_ids = request.outfit_ids or []
        if len(outfit_ids) > limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Maximum {limit} outfits per bulk action",
            )

    for outfit_id in outfit_ids:
        try:
            result = await db.execute(
                select(Outfit).where(
                    and_(Outfit.id == outfit_id, Outfit.user_id == current_user.id)
                )
            )
            outfit = result.scalar_one_or_none()
            if not outfit:
                errors.append(f"Outfit {outfit_id} not found or not owned by user")
                failed += 1
                continue

            await db.delete(outfit)
            deleted += 1
        except Exception as e:
            logger.error(f"Failed to delete outfit {outfit_id}: {e}")
            errors.append(f"Failed to delete outfit {outfit_id}")
            failed += 1

    await db.commit()

    return BulkDeleteOutfitsResponse(
        deleted=deleted,
        failed=failed,
        errors=errors,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/{outfit_id}", response_model=OutfitResponse)
async def get_outfit(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    query = (
        select(Outfit)
        .where(and_(Outfit.id == outfit_id, Outfit.user_id == current_user.id))
        .options(
            selectinload(Outfit.items).selectinload(OutfitItem.item),
            selectinload(Outfit.feedback),
            selectinload(Outfit.family_ratings).selectinload(FamilyOutfitRating.user),
        )
    )

    result = await db.execute(query)
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Outfit not found", "error_code": "OUTFIT_NOT_FOUND"},
        )

    return outfit_to_response(
        outfit, await fetch_wore_instead_items_map(db, [outfit], user_id=current_user.id)
    )


@router.post("/{outfit_id}/reaction", response_model=OutfitResponse)
async def react_to_outfit(
    outfit_id: UUID,
    request: ReactionRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    # Serialize rapid taps/retries with the legacy feedback route too. One wear
    # event per outfit; changing a preference never deletes an actual wear.
    query = (
        select(Outfit)
        .where(Outfit.id == outfit_id, Outfit.user_id == current_user.id)
        .with_for_update()
        .options(
            selectinload(Outfit.feedback),
            selectinload(Outfit.items).selectinload(OutfitItem.item),
            selectinload(Outfit.family_ratings).selectinload(FamilyOutfitRating.user),
        )
        .execution_options(populate_existing=True)
    )
    outfit = (await db.execute(query)).scalar_one_or_none()
    if not outfit:
        raise HTTPException(status_code=404, detail="Outfit not found")
    today = get_user_today(current_user)
    if request.action == "wear" and (outfit.scheduled_for is None or outfit.scheduled_for > today):
        raise HTTPException(
            status_code=400,
            detail="Like a future outfit now; record wearing it on the day you wear it.",
        )
    feedback = outfit.feedback
    if feedback is None:
        feedback = UserFeedback(outfit_id=outfit.id)
        outfit.feedback = feedback
        db.add(feedback)
    positive = request.action != "dislike"
    new_status = OutfitStatus.accepted if positive else OutfitStatus.rejected
    changed = feedback.accepted != positive or outfit.status != new_status
    feedback.accepted = positive
    outfit.status = new_status
    if request.action == "wear" and feedback.actually_worn is not True:
        feedback.actually_worn = True
        changed = True
    if request.action == "wear" and feedback.worn_at is None:
        feedback.worn_at = today
        feedback.actually_worn = True
        await ItemService(db).record_wears(
            current_user.id,
            [oi.item_id for oi in outfit.items],
            today,
            occasion=outfit.occasion,
            outfit_id=outfit.id,
        )
        changed = True
    if changed:
        outfit.responded_at = datetime.utcnow()
    await db.commit()
    if changed:
        await _run_learning_safely(db, outfit.id, current_user.id)
        await clear_suggestions(current_user.id, outfit.occasion)
    outfit = (await db.execute(query)).scalar_one()
    return outfit_to_response(
        outfit, await fetch_wore_instead_items_map(db, [outfit], user_id=current_user.id)
    )


@router.post("/{outfit_id}/accept", response_model=OutfitResponse)
async def accept_outfit(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    query = (
        select(Outfit)
        .where(and_(Outfit.id == outfit_id, Outfit.user_id == current_user.id))
        .options(
            selectinload(Outfit.items).selectinload(OutfitItem.item),
            selectinload(Outfit.feedback),
            selectinload(Outfit.family_ratings).selectinload(FamilyOutfitRating.user),
        )
    )

    result = await db.execute(query)
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Outfit not found", "error_code": "OUTFIT_NOT_FOUND"},
        )

    outfit.status = OutfitStatus.accepted
    outfit.responded_at = datetime.utcnow()
    await db.commit()
    await _run_learning_safely(db, outfit.id, current_user.id)
    await db.refresh(outfit)

    return outfit_to_response(
        outfit, await fetch_wore_instead_items_map(db, [outfit], user_id=current_user.id)
    )


@router.post("/{outfit_id}/reject", response_model=OutfitResponse)
async def reject_outfit(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    query = (
        select(Outfit)
        .where(and_(Outfit.id == outfit_id, Outfit.user_id == current_user.id))
        .options(
            selectinload(Outfit.items).selectinload(OutfitItem.item),
            selectinload(Outfit.feedback),
            selectinload(Outfit.family_ratings).selectinload(FamilyOutfitRating.user),
        )
    )

    result = await db.execute(query)
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Outfit not found", "error_code": "OUTFIT_NOT_FOUND"},
        )

    outfit.status = OutfitStatus.rejected
    outfit.responded_at = datetime.utcnow()
    await db.commit()
    await _run_learning_safely(db, outfit.id, current_user.id)
    await db.refresh(outfit)

    await clear_suggestions(current_user.id, outfit.occasion)

    return outfit_to_response(
        outfit, await fetch_wore_instead_items_map(db, [outfit], user_id=current_user.id)
    )


@router.post("/{outfit_id}/skip", response_model=OutfitResponse)
async def skip_outfit(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    service = OutfitService(db)
    outfit = await service.set_status(outfit_id, current_user.id, OutfitStatus.skipped)
    await clear_suggestions(current_user.id, outfit.occasion)
    return outfit_to_response(
        outfit, await fetch_wore_instead_items_map(db, [outfit], user_id=current_user.id)
    )


@router.delete("/{outfit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_outfit(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    query = select(Outfit).where(and_(Outfit.id == outfit_id, Outfit.user_id == current_user.id))

    result = await db.execute(query)
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Outfit not found", "error_code": "OUTFIT_NOT_FOUND"},
        )

    await db.delete(outfit)
    await db.commit()


@router.post("/{outfit_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    outfit_id: UUID,
    request: FeedbackRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> FeedbackResponse:
    query = (
        select(Outfit)
        .where(and_(Outfit.id == outfit_id, Outfit.user_id == current_user.id))
        .with_for_update()
        .options(
            selectinload(Outfit.feedback), selectinload(Outfit.items).selectinload(OutfitItem.item)
        )
    )

    result = await db.execute(query)
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Outfit not found", "error_code": "OUTFIT_NOT_FOUND"},
        )

    if outfit.feedback:
        feedback = outfit.feedback
    else:
        feedback = UserFeedback(outfit_id=outfit.id)
        outfit.feedback = feedback
        db.add(feedback)

    if request.accepted is not None:
        feedback.accepted = request.accepted
        outfit.status = OutfitStatus.accepted if request.accepted else OutfitStatus.rejected
        outfit.responded_at = datetime.utcnow()

    if request.rating is not None:
        feedback.rating = request.rating
    if request.comfort_rating is not None:
        feedback.comfort_rating = request.comfort_rating
    if request.style_rating is not None:
        feedback.style_rating = request.style_rating
    if request.comment is not None:
        feedback.comment = request.comment
    if request.worn and not feedback.worn_at:
        user_today = get_user_today(current_user)
        feedback.worn_at = user_today
        await ItemService(db).record_wears(
            current_user.id,
            [outfit_item.item_id for outfit_item in outfit.items],
            user_today,
            occasion=outfit.occasion,
            outfit_id=outfit.id,
        )
    if request.worn_with_modifications is not None:
        feedback.worn_with_modifications = request.worn_with_modifications
    if request.modification_notes is not None:
        feedback.modification_notes = request.modification_notes
    if request.actually_worn is not None:
        feedback.actually_worn = request.actually_worn
    replacement: Outfit | None = None
    if request.wore_instead_items is not None:
        feedback.wore_instead_items = [str(item_id) for item_id in request.wore_instead_items]
        if request.wore_instead_items:
            studio_service = StudioService(db)
            try:
                replacement = await studio_service.create_wore_instead(
                    user=current_user,
                    original_outfit_id=outfit_id,
                    item_ids=list(request.wore_instead_items),
                    rating=request.rating,
                    comment=request.comment,
                    scheduled_for=None,
                )
            except ItemOwnershipError:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error_code": "OUTFIT_ITEM_OWNERSHIP",
                        "message": "One or more items do not belong to you",
                    },
                ) from None

    await db.commit()
    await db.refresh(feedback)

    await _run_learning_safely(db, outfit_id, current_user.id)
    if replacement is not None:
        await _run_learning_safely(db, replacement.id, current_user.id)

    return FeedbackResponse(
        id=feedback.id,
        outfit_id=feedback.outfit_id,
        accepted=feedback.accepted,
        rating=feedback.rating,
        comfort_rating=feedback.comfort_rating,
        style_rating=feedback.style_rating,
        comment=feedback.comment,
        worn_at=feedback.worn_at,
        worn_with_modifications=feedback.worn_with_modifications,
        modification_notes=feedback.modification_notes,
        actually_worn=feedback.actually_worn,
        wore_instead_items=[UUID(item_id) for item_id in (feedback.wore_instead_items or [])],
        created_at=feedback.created_at,
    )


@router.get("/{outfit_id}/feedback", response_model=FeedbackResponse)
async def get_feedback(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> FeedbackResponse:
    query = (
        select(Outfit)
        .where(and_(Outfit.id == outfit_id, Outfit.user_id == current_user.id))
        .options(selectinload(Outfit.feedback))
    )

    result = await db.execute(query)
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"message": "Outfit not found", "error_code": "OUTFIT_NOT_FOUND"},
        )

    if not outfit.feedback:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No feedback found for this outfit",
        )

    feedback = outfit.feedback
    return FeedbackResponse(
        id=feedback.id,
        outfit_id=feedback.outfit_id,
        accepted=feedback.accepted,
        rating=feedback.rating,
        comfort_rating=feedback.comfort_rating,
        style_rating=feedback.style_rating,
        comment=feedback.comment,
        worn_at=feedback.worn_at,
        worn_with_modifications=feedback.worn_with_modifications,
        modification_notes=feedback.modification_notes,
        actually_worn=feedback.actually_worn,
        wore_instead_items=[UUID(item_id) for item_id in (feedback.wore_instead_items or [])],
        created_at=feedback.created_at,
    )


@router.post("/{outfit_id}/family-rating", response_model=FamilyRatingResponse)
async def submit_family_rating(
    outfit_id: UUID,
    request: FamilyRatingRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> FamilyRatingResponse:
    result = await db.execute(select(Outfit).where(Outfit.id == outfit_id))
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outfit not found")

    if outfit.scheduled_for is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "OUTFIT_IS_TEMPLATE",
                "message": "Cannot rate a lookbook template",
            },
        )

    if outfit.user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot rate your own outfit",
        )

    if not current_user.family_id or not outfit.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "You must be in the same family to rate outfits",
                "error_code": "NOT_IN_FAMILY",
            },
        )

    owner_result = await db.execute(
        select(User).where(User.id == outfit.user_id, User.is_active == True)  # noqa: E712
    )
    owner = owner_result.scalar_one_or_none()
    if not owner or owner.family_id != current_user.family_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "You must be in the same family to rate outfits",
                "error_code": "NOT_IN_FAMILY",
            },
        )

    existing = await db.execute(
        select(FamilyOutfitRating).where(
            and_(
                FamilyOutfitRating.outfit_id == outfit_id,
                FamilyOutfitRating.user_id == current_user.id,
            )
        )
    )
    rating = existing.scalar_one_or_none()

    if rating:
        rating.rating = request.rating
        rating.comment = request.comment
    else:
        rating = FamilyOutfitRating(
            outfit_id=outfit_id,
            user_id=current_user.id,
            rating=request.rating,
            comment=request.comment,
        )
        db.add(rating)

    await db.flush()
    await db.refresh(rating)

    return FamilyRatingResponse(
        id=rating.id,
        user_id=rating.user_id,
        user_display_name=current_user.display_name or current_user.email,
        user_avatar_url=current_user.avatar_url,
        rating=rating.rating,
        comment=rating.comment,
        created_at=rating.created_at,
    )


@router.get("/{outfit_id}/family-ratings", response_model=list[FamilyRatingResponse])
async def get_family_ratings(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[FamilyRatingResponse]:
    result = await db.execute(select(Outfit).where(Outfit.id == outfit_id))
    outfit = result.scalar_one_or_none()

    if not outfit:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outfit not found")

    if outfit.user_id != current_user.id:
        if not current_user.family_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        owner_result = await db.execute(
            select(User).where(User.id == outfit.user_id, User.is_active == True)  # noqa: E712
        )
        owner = owner_result.scalar_one_or_none()
        if not owner or owner.family_id != current_user.family_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    ratings_result = await db.execute(
        select(FamilyOutfitRating)
        .where(FamilyOutfitRating.outfit_id == outfit_id)
        .options(selectinload(FamilyOutfitRating.user))
        .order_by(FamilyOutfitRating.created_at.desc())
    )
    ratings = list(ratings_result.scalars().all())

    return [
        FamilyRatingResponse(
            id=r.id,
            user_id=r.user_id,
            user_display_name=r.user.display_name or r.user.email,
            user_avatar_url=r.user.avatar_url,
            rating=r.rating,
            comment=r.comment,
            created_at=r.created_at,
        )
        for r in ratings
    ]


def _check_studio_kill_switch() -> None:
    if get_settings().studio_disabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "STUDIO_UNAVAILABLE",
                "message": "Studio is temporarily unavailable. AI features still work.",
            },
        )


class StudioCreateRequest(OutfitAttributeFields):
    model_config = ConfigDict(extra="forbid")

    items: list[UUID] = Field(min_length=1, max_length=20)
    occasion: Occasion
    name: Annotated[str | None, Field(max_length=100)] = None
    scheduled_for: date | None = None
    mark_worn: bool = False
    source_item_id: UUID | None = None


class WoreInsteadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[UUID] = Field(min_length=1, max_length=20)
    rating: Annotated[int | None, Field(ge=RATING_MIN, le=RATING_MAX)] = None
    comment: Annotated[str | None, Field(max_length=1000)] = None
    scheduled_for: date | None = None


class CloneToLookbookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class WearTodayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_for: date | None = None


class PatchOutfitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(max_length=100)] = None
    items: Annotated[list[UUID] | None, Field(min_length=1, max_length=20)] = None


async def _run_learning_safely(db: AsyncSession, outfit_id: UUID, user_id: UUID) -> None:
    try:
        await LearningService(db).process_feedback(outfit_id, user_id)
    except Exception as e:
        logger.exception("learning process_feedback failed for outfit %s: %s", outfit_id, e)


@router.post("/studio", response_model=OutfitResponse, status_code=status.HTTP_201_CREATED)
async def create_studio_outfit(
    request: StudioCreateRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    _check_studio_kill_switch()
    await rate_limit_by_user(
        str(current_user.id), "studio_create", max_requests=20, window_seconds=60
    )

    service = StudioService(db)
    try:
        outfit = await service.create_from_scratch(
            user=current_user,
            item_ids=request.items,
            occasion=request.occasion,
            name=request.name,
            scheduled_for=request.scheduled_for,
            mark_worn=request.mark_worn,
            source_item_id=request.source_item_id,
            season=request.season,
            formality=request.formality,
            palette=request.palette,
            notes=request.notes,
        )
    except ItemOwnershipError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "OUTFIT_ITEM_OWNERSHIP",
                "message": "One or more items do not belong to you",
            },
        ) from None

    await db.commit()
    await _run_learning_safely(db, outfit.id, current_user.id)
    await clear_suggestions(current_user.id, outfit.occasion)

    full = await service.get_full_outfit(outfit.id)
    return outfit_to_response(full)


@router.post("/{outfit_id}/wore-instead", response_model=OutfitResponse)
async def create_wore_instead_outfit(
    outfit_id: UUID,
    request: WoreInsteadRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    _check_studio_kill_switch()
    await rate_limit_by_user(
        str(current_user.id), "wore_instead", max_requests=10, window_seconds=60
    )

    service = StudioService(db)
    try:
        replacement = await service.create_wore_instead(
            user=current_user,
            original_outfit_id=outfit_id,
            item_ids=request.items,
            rating=request.rating,
            comment=request.comment,
            scheduled_for=request.scheduled_for,
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "OUTFIT_NOT_FOUND", "message": "Outfit not found"},
        ) from None
    except ItemOwnershipError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "OUTFIT_ITEM_OWNERSHIP",
                "message": "One or more items do not belong to you",
            },
        ) from None

    await db.commit()
    await _run_learning_safely(db, replacement.id, current_user.id)
    await clear_suggestions(current_user.id, replacement.occasion)

    full = await service.get_full_outfit(replacement.id)
    return outfit_to_response(full)


@router.post("/{outfit_id}/clone-to-lookbook", response_model=OutfitResponse)
async def clone_outfit_to_lookbook(
    outfit_id: UUID,
    request: CloneToLookbookRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    _check_studio_kill_switch()
    await rate_limit_by_user(
        str(current_user.id), "clone_to_lookbook", max_requests=20, window_seconds=60
    )

    service = StudioService(db)
    try:
        clone = await service.clone_to_lookbook(
            user=current_user,
            source_outfit_id=outfit_id,
            name=request.name,
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "OUTFIT_NOT_FOUND", "message": "Outfit not found"},
        ) from None

    await db.commit()
    await _run_learning_safely(db, clone.id, current_user.id)

    full = await service.get_full_outfit(clone.id)
    return outfit_to_response(full)


@router.post("/{outfit_id}/wear-today", response_model=OutfitResponse)
async def wear_outfit_today(
    outfit_id: UUID,
    request: WearTodayRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    _check_studio_kill_switch()
    await rate_limit_by_user(str(current_user.id), "wear_today", max_requests=10, window_seconds=60)

    service = StudioService(db)
    try:
        wear = await service.wear_today(
            user=current_user,
            template_id=outfit_id,
            scheduled_for=request.scheduled_for,
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "OUTFIT_NOT_FOUND", "message": "Outfit not found"},
        ) from None
    except OutfitNotTemplateError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "OUTFIT_NOT_TEMPLATE",
                "message": "wear-today requires a lookbook template",
            },
        ) from None

    await db.commit()
    await _run_learning_safely(db, wear.id, current_user.id)
    await clear_suggestions(current_user.id, wear.occasion)

    full = await service.get_full_outfit(wear.id)
    return outfit_to_response(full)


@router.patch("/{outfit_id}", response_model=OutfitResponse)
async def patch_outfit_endpoint(
    outfit_id: UUID,
    request: PatchOutfitRequest,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitResponse:
    _check_studio_kill_switch()
    await rate_limit_by_user(
        str(current_user.id), "patch_outfit", max_requests=30, window_seconds=60
    )

    if request.name is None and request.items is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "PATCH_EMPTY", "message": "No fields provided"},
        )

    service = StudioService(db)
    try:
        updated = await service.patch_outfit(
            user=current_user,
            outfit_id=outfit_id,
            name=request.name,
            items=request.items,
        )
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "OUTFIT_NOT_FOUND", "message": "Outfit not found"},
        ) from None
    except ItemOwnershipError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "OUTFIT_ITEM_OWNERSHIP",
                "message": "One or more items do not belong to you",
            },
        ) from None
    except OutfitWornImmutableError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "OUTFIT_WORN_IMMUTABLE",
                "message": "Cannot modify items on a worn outfit. Create a new lookbook entry instead.",
            },
        ) from None

    await db.commit()

    full = await service.get_full_outfit(updated.id)
    return outfit_to_response(full)


@router.delete("/{outfit_id}/family-rating", status_code=status.HTTP_204_NO_CONTENT)
async def delete_family_rating(
    outfit_id: UUID,
    db: DbSession,
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    result = await db.execute(
        select(FamilyOutfitRating).where(
            and_(
                FamilyOutfitRating.outfit_id == outfit_id,
                FamilyOutfitRating.user_id == current_user.id,
            )
        )
    )
    rating = result.scalar_one_or_none()

    if not rating:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rating not found",
        )

    await db.delete(rating)
    await db.flush()
