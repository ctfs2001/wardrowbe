from datetime import date
from uuid import uuid4

import pytest

from app.models.item import ClothingItem
from app.services.item_scorer import score_items
from app.services.outfit_quality import validate_option
from tests.test_item_scorer import _weather


def wardrobe(types):
    items = [ClothingItem(id=uuid4(), type=t, formality="casual") for t in types]
    return {n: i.id for n, i in enumerate(items, 1)}, {i.id: i for i in items}


@pytest.mark.parametrize(
    "types,selection,valid",
    [
        (["shirt", "pants", "sneakers"], [1, 2, 3], True),
        (["dress", "shoes"], [1, 2], True),
        (["shirt", "suit", "shoes"], [1, 2, 3], True),
        (["pants", "sneakers"], [1, 2], False),
        (["shirt", "pants", "sneakers"], [1, 2], False),
        (["shirt", "pants", "shorts", "sneakers"], [1, 2, 3, 4], False),
        (["shirt", "pants", "sneakers"], [1, 2, 3, 999], False),
        (["shirt", "pants", "sneakers"], [True, 2, 3], False),
    ],
)
def test_complete_outfits_only(types, selection, valid):
    numbers, items = wardrobe(types)
    _, error = validate_option({"items": selection}, numbers, items, set(), [])
    assert (error is None) == valid


def test_accessory_swap_is_not_a_new_outfit():
    numbers, items = wardrobe(["shirt", "pants", "sneakers", "hat"])
    previous = [{numbers[n] for n in [1, 2, 3]}]
    _, error = validate_option({"items": [1, 2, 3, 4]}, numbers, items, set(), previous)
    assert "repeats" in error


def test_fixed_core_can_repeat_but_identical_look_cannot():
    numbers, items = wardrobe(["shirt", "pants", "sneakers", "shoes"])
    previous = [{numbers[n] for n in [1, 2, 3]}]
    mandatory = {numbers[1], numbers[2]}
    assert validate_option({"items": [1, 2, 4]}, numbers, items, mandatory, previous)[1] is None
    assert validate_option({"items": [1, 2, 3]}, numbers, items, mandatory, previous)[1]


def test_extreme_formality_mismatch_is_rejected():
    numbers, items = wardrobe(["shirt", "pants", "shoes"])
    items[numbers[3]].formality = "very-formal"
    assert "formality" in validate_option({"items": [1, 2, 3]}, numbers, items, set(), [])[1]


def test_missing_mandatory_and_hot_legwear_are_not_silently_rewritten():
    numbers, items = wardrobe(["shirt", "pants", "sneakers", "hat", "tights"])
    assert "mandatory" in validate_option({"items": [1, 2, 3]}, numbers, items, {numbers[4]}, [])[1]
    assert (
        "legwear"
        in validate_option({"items": [1, 2, 3, 5]}, numbers, items, set(), [], drop_legwear=True)[1]
    )


def test_unworn_small_wardrobe_still_rotates_recommendations():
    numbers, items = wardrobe(["shirt", "shirt", "sneakers"])
    scored = score_items(
        list(items.values()),
        _weather(),
        "casual",
        None,
        date(2026, 10, 26),
        "fall",
        None,
        {},
        {},
        recent_suggestions={numbers[1]: 8, numbers[3]: 8},
    )
    scores = {s.item.id: s for s in scored}
    assert scores[numbers[2]].score > scores[numbers[1]].score
    assert scores[numbers[3]].exposure_score == 1
    assert scores[numbers[1]].recent_suggestions == 8


@pytest.mark.parametrize("missing", [4, 5, 6])
def test_daily_accessories_required_when_available(missing):
    numbers, items = wardrobe(["shirt", "pants", "sneakers", "hat", "bag", "accessories"])
    items[numbers[6]].subtype = "glasses"
    selection = [n for n in numbers if n != missing]
    assert "exactly one" in validate_option({"items": selection}, numbers, items, set(), [])[1]
    assert validate_option({"items": list(numbers)}, numbers, items, set(), [])[1] is None


def test_daily_accessories_can_repeat_across_distinct_looks():
    numbers, items = wardrobe(
        ["shirt", "pants", "sneakers", "hat", "bag", "accessories", "shirt", "pants"]
    )
    items[numbers[6]].subtype = "眼鏡"
    previous = [{numbers[n] for n in [1, 2, 3, 4, 5, 6]}]
    assert (
        validate_option({"items": [7, 8, 3, 4, 5, 6]}, numbers, items, set(), previous)[1] is None
    )


@pytest.mark.parametrize("subtype", ["necklace", "watch", None])
def test_other_accessories_do_not_require_eyewear(subtype):
    numbers, items = wardrobe(["shirt", "pants", "sneakers", "accessories"])
    items[numbers[4]].subtype = subtype
    assert validate_option({"items": [1, 2, 3]}, numbers, items, set(), [])[1] is None


def test_two_bags_are_not_a_valid_daily_accessory_selection():
    numbers, items = wardrobe(["shirt", "pants", "sneakers", "bag", "bag"])
    assert "bag" in validate_option({"items": list(numbers)}, numbers, items, set(), [])[1]


def test_candidate_limit_preserves_daily_accessory_categories():
    numbers, items = wardrobe(["shirt"] * 205 + ["pants", "sneakers", "hat", "bag", "accessories"])
    items[numbers[210]].subtype = "sunglasses"
    scored = score_items(
        list(items.values()),
        _weather(),
        "casual",
        None,
        date(2026, 10, 26),
        "fall",
        None,
        {},
        {},
    )
    assert len(scored) == 200
    assert {numbers[n] for n in range(206, 211)} <= {s.item.id for s in scored}
