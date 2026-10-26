"""Identify daily accessories from structured wardrobe classification."""

import re

from app.models.item import ClothingItem


def daily_accessory(item: ClothingItem) -> str | None:
    kind = (item.type or "").strip().lower()
    if kind in {"hat", "bag"}:
        return kind
    if kind != "accessories":
        return None
    subtype = (item.subtype or "").strip().lower()
    if re.search(r"\b(glasses|eyeglasses|sunglasses|eyewear|spectacles)\b", subtype) or any(
        word in subtype for word in ("眼鏡", "眼镜", "墨鏡", "墨镜")
    ):
        return "eyewear"
    return None
