"""Shared, bounded inputs for on-demand outfit planning."""

from datetime import date, timedelta
from typing import Literal

from app.models.user import User
from app.utils.timezone import get_user_today

WearDay = Literal["today", "tomorrow"]
ActivityNeed = Literal["indoor_ac", "outdoors", "walking", "rain_ready", "easy_move", "polished"]

ACTIVITY_NOTES = {
    "indoor_ac": "長時間待冷氣房，搭配方便穿脫的薄層次",
    "outdoors": "以戶外活動為主，留意防曬與透氣",
    "walking": "需要長時間步行，優先舒適好走的鞋",
    "rain_ready": "可能接觸雨水，優先耐濕易整理的材質；此需求不代表預報一定下雨",
    "easy_move": "需要方便活動，避免限制動作的剪裁",
    "polished": "希望稍微正式、俐落得體，仍須符合所選場合",
}


def wearing_date(user: User, day: WearDay) -> date:
    return get_user_today(user) + timedelta(days=day == "tomorrow")


def activity_notes(needs: list[ActivityNeed]) -> str:
    return "；".join(ACTIVITY_NOTES[need] for need in dict.fromkeys(needs))
