from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.outfit import OutfitStatus
from app.services.recommendation_memory import feedback_weight, summarize_responses

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


def response(age=0, status=OutfitStatus.accepted, color="navy", ids=None):
    ids = ids or [uuid4(), uuid4()]
    return SimpleNamespace(
        status=status,
        feedback=None,
        responded_at=NOW - timedelta(days=age),
        created_at=NOW,
        items=[
            SimpleNamespace(
                item_id=i, item=SimpleNamespace(type=t, primary_color=color, style=["casual"])
            )
            for i, t in zip(ids, ["shirt", "pants"], strict=True)
        ],
    )


def test_feedback_influence_halves_after_thirty_days_and_expires():
    assert feedback_weight(NOW, NOW) == 1
    assert feedback_weight(NOW - timedelta(days=30), NOW) == 0.5
    assert feedback_weight(NOW - timedelta(days=181), NOW) == 0
    assert summarize_responses([response()], NOW)[0]["learned_favorite_colors"] == ["navy"]
    assert summarize_responses([response(age=60)], NOW) == ({}, {})


@pytest.mark.parametrize(
    "status",
    [OutfitStatus.pending, OutfitStatus.skipped, OutfitStatus.viewed, OutfitStatus.rejected],
)
def test_unselected_and_disliked_outfits_do_not_create_color_or_style_preferences(status):
    assert summarize_responses([response(status=status)], NOW)[0] == {}


def test_recent_choice_can_replace_old_favorites_without_erasing_history():
    old = [response(age=100, color="black") for _ in range(100)]
    assert summarize_responses([response(color="white"), *old], NOW)[0][
        "learned_favorite_colors"
    ] == ["white"]


def test_same_core_same_day_is_only_one_signal_and_pairs_decay():
    ids = [uuid4(), uuid4()]
    assert summarize_responses([response(ids=ids), response(ids=ids)], NOW)[1] == {}
    assert summarize_responses([response(ids=ids), response(age=1, ids=ids)], NOW)[1]
    assert summarize_responses([response(age=90, ids=ids), response(age=91, ids=ids)], NOW)[1] == {}


def test_dislike_removes_optional_pair_bonus_without_a_garment_blacklist():
    ids = [uuid4(), uuid4()]
    looks = [
        response(ids=ids),
        response(age=1, ids=ids),
        response(age=2, ids=ids, status=OutfitStatus.rejected),
    ]
    prefs, pairs = summarize_responses(looks, NOW)
    assert pairs == {}
    assert "learned_avoid_colors" not in prefs
