"""Validate model selections before saving them or their accompanying explanations."""

from uuid import UUID

from app.models.item import ClothingItem
from app.utils.clothing import BODY_SLOTS, covered_body_slots, deduplicate_by_body_slot
from app.utils.daily_accessories import daily_accessory
from app.utils.garment_vocabulary import FORMALITY, ITEM_ROLE

CORE_ROLES = frozenset({"base_top", "bottom", "full_body", "suit"})


class QualityProblem(str):
    """A rejection message for the model, plus a stable reason code for diagnostics.

    The text stays the instruction sent back to the model; the code is what logs and
    stored diagnostics use, so that wording changes never break later analysis.
    """

    code: str

    def __new__(cls, code: str, message: str) -> "QualityProblem":
        problem = super().__new__(cls, message)
        problem.code = code
        return problem


def core_items(ids: set[UUID], items: dict[UUID, ClothingItem]) -> frozenset[UUID]:
    return frozenset(
        iid
        for iid in ids
        if iid in items and ITEM_ROLE.get((items[iid].type or "").lower()) in CORE_ROLES
    )


def validate_option(
    option: object,
    number_map: dict[int, UUID],
    items: dict[UUID, ClothingItem],
    mandatory: set[UUID],
    previous: list[set[UUID]],
    *,
    drop_legwear: bool = False,
    batch_previous: list[set[UUID]] | None = None,
    variety_pool: set[UUID] | None = None,
) -> tuple[set[UUID], QualityProblem | None]:
    """Do not silently swap pieces: the model's explanation must describe what is saved.

    Unknown tags do not justify a hard style rule. Known extreme formality clashes do.
    Accessories and shoes may repeat; only independent core choices need variety.
    """
    if not isinstance(option, dict) or not isinstance(option.get("items"), list):
        return set(), QualityProblem("invalid_format", "items must be a list of wardrobe numbers")
    ids: set[UUID] = set()
    for number in option["items"]:
        if isinstance(number, bool) or not str(number).isdigit():
            return set(), QualityProblem("invalid_format", "use only integer wardrobe numbers")
        iid = number_map.get(int(number))
        if iid is None or iid not in items:
            return set(), QualityProblem("unavailable_item", "contains an unavailable item number")
        ids.add(iid)
    if not mandatory <= ids:
        return ids, QualityProblem("missing_mandatory", "missing a mandatory item")
    types = {iid: (items[iid].type or "").lower() for iid in ids}
    if covered_body_slots(list(ids), types) != BODY_SLOTS:
        return ids, QualityProblem(
            "incomplete_outfit", "missing a top or bottom (or a full-body garment)"
        )
    if sum(ITEM_ROLE.get(types[iid]) == "footwear" for iid in ids) != 1:
        return ids, QualityProblem("footwear_count", "must select exactly one footwear item")
    if len(deduplicate_by_body_slot(list(ids), types, mandatory)) != len(ids):
        return ids, QualityProblem("slot_conflict", "conflicting pieces occupy the same body slot")
    if drop_legwear and any(ITEM_ROLE.get(types[iid]) == "legwear" for iid in ids - mandatory):
        return ids, QualityProblem(
            "hot_weather_legwear", "unrequested legwear in consistently hot weather"
        )
    levels = [
        FORMALITY.index(items[iid].formality)
        for iid in ids
        if ITEM_ROLE.get(types[iid]) in CORE_ROLES | {"footwear"}
        and items[iid].formality in FORMALITY
    ]
    if levels and max(levels) - min(levels) >= 3:
        return ids, QualityProblem(
            "formality_mismatch", "core pieces and shoes have an extreme formality mismatch"
        )
    # Only require categories present in the available candidate inventory. Never
    # fabricate a missing accessory or silently append it to the model's explanation.
    available = {daily_accessory(item) for item in items.values()} - {None}
    for category in sorted(available):
        if sum(daily_accessory(items[iid]) == category for iid in ids) != 1:
            return ids, QualityProblem(
                "missing_daily_accessory",
                f"must select exactly one {category} item from available inventory",
            )
    core = core_items(ids, items)

    def repeats(earlier: set[UUID]) -> bool:
        return ids == earlier or (core == core_items(earlier, items) and not core <= mandatory)

    if any(repeats(earlier) for earlier in previous):
        # Same message for the model; the code tells a repeat within this batch from a
        # repeat of a recently suggested look.
        in_batch = any(repeats(earlier) for earlier in batch_previous or [])
        return ids, QualityProblem(
            "duplicate_in_batch" if in_batch else "duplicate_recent",
            "repeats a core outfit; changing only accessories is not variety",
        )
    used = set().union(*(batch_previous or []))
    for iid in (core & used) - mandatory:
        role = ITEM_ROLE.get(types[iid])
        if any(
            ITEM_ROLE.get((items[other].type or "").lower()) == role
            for other in (variety_pool or set()) - used - ids - mandatory
        ):
            return ids, QualityProblem(
                "reused_key_piece",
                "reuses a key piece although suitable unused alternatives remain",
            )
    return ids, None
