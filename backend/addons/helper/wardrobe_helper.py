"""Wardrowbe helper: tidy item photos and read brand / material from them.

Runs inside the wardrowbe backend image. For every ready item it has not seen yet:
  1. Asks the vision model for the garment's bounding box, brand and material label text.
  2. Crops to the garment, removes the background (rembg), centres it on a white square.
     The untouched original is kept as the item's backup, so "restore original" still works.
  3. Fills brand (only if empty) and adds the material label text to notes.
  4. Reverse image search (Google Cloud Vision web detection) + Gemini to find the product's
     brand, name, material, price and page; fills empty brand/name and adds the rest to notes.
State lives in /data/wardrobe/.helper_state.json and .helper_web_state.json.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import sys
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.item import ClothingItem, ItemStatus
from app.services import background_removal
from app.services.image_service import ImageService
from app.utils.garment_vocabulary import MATERIALS, TYPES

logging.basicConfig(level=logging.INFO, format="%(asctime)s helper %(levelname)s %(message)s")
log = logging.getLogger("helper")

settings = get_settings()
STORAGE = Path(settings.storage_path)
STATE_FILE = STORAGE / ".helper_state.json"
INTERVAL = int(os.getenv("HELPER_INTERVAL_SECONDS", "300"))
DO_IMAGES = os.getenv("HELPER_TIDY_IMAGES", "true").lower() == "true"
DO_INFO = os.getenv("HELPER_READ_INFO", "true").lower() == "true"
PADDING = float(os.getenv("HELPER_PADDING", "0.08"))
MODEL = os.getenv("HELPER_MODEL") or settings.ai_vision_model

PROMPT = f"""You are looking at a photo of one clothing item. The item's category is: {{item_type}}.
Return ONLY a JSON object with these keys:
- "photo_kind": "product" if the item is shown alone (flat lay, hanger, or product cut-out) on a plain
  background; "worn" if a person is wearing or holding it; "screenshot" if it is a shopping app or
  web page screenshot.
- "box_2d": [ymin, xmin, ymax, xmax] of that clothing item only, integers normalised to 0-1000.
  Cover the whole item tightly; exclude the rest of the person and any UI or text.
- "brand": the brand name only if it is actually readable (logo, label, tag, or text such as a store
  name in a screenshot). Otherwise null. Never guess from style.
- "material_text": the material as written on a label or in product text (name, description,
  composition), in Traditional Chinese, e.g. "棉 100%", "聚酯纖維 65%、棉 35%", "麂皮", "亞麻".
  null if no material is written anywhere in the image.
- "material": the main material as one of {list(MATERIALS)}, only when material_text is not null,
  otherwise null.
- "type": the item's category as one of {list(TYPES)}.
- "is_eyewear": true if the item is glasses or sunglasses, otherwise false.
"""


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def to_b64(img: Image.Image, max_px: int = 1536) -> str:
    img = img.copy()
    img.thumbnail((max_px, max_px))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


async def ask_model(
    client: httpx.AsyncClient,
    img: Image.Image,
    item_type: str,
    prompt: str | None = None,
    extra_images: list | None = None,
) -> dict:
    text = (prompt or PROMPT).replace("{item_type}", item_type or "unknown")
    content = [{"type": "text", "text": text}]
    for im in [img, *(extra_images or [])]:
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{to_b64(im)}"}}
        )
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
    }
    if settings.ai_reasoning_effort:
        body["reasoning_effort"] = settings.ai_reasoning_effort
    headers = {"Authorization": f"Bearer {settings.ai_api_key}"} if settings.ai_api_key else {}
    url = f"{str(settings.ai_base_url).rstrip('/')}/chat/completions"
    for attempt in range(4):
        r = await client.post(url, json=body, headers=headers)
        if r.status_code == 400 and "response_format" in body:
            body.pop("response_format")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            await asyncio.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}
    raise RuntimeError(f"model unavailable ({r.status_code})")


def crop_to_box(img: Image.Image, box) -> Image.Image:
    w, h = img.size
    if isinstance(box, list) and len(box) == 4:
        y0, x0, y1, x1 = (max(0, min(1000, int(v))) for v in box)
        if y1 - y0 > 50 and x1 - x0 > 50:
            mx, my = int((x1 - x0) * w / 1000 * 0.04), int((y1 - y0) * h / 1000 * 0.04)
            return img.crop(
                (
                    max(0, x0 * w // 1000 - mx),
                    max(0, y0 * h // 1000 - my),
                    min(w, x1 * w // 1000 + mx),
                    min(h, y1 * h // 1000 + my),
                )
            )
    return img


def on_square(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    cw, ch = img.size
    side = int(max(cw, ch) * (1 + 2 * PADDING))
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    canvas.paste(img, ((side - cw) // 2, (side - ch) // 2), img)
    return canvas.convert("RGB")


def tidy(img: Image.Image, box, kind: str) -> tuple[Image.Image, str]:
    crop = crop_to_box(img, box)
    if kind == "product":
        cut = background_removal.get_provider().remove(crop.convert("RGB")).convert("RGBA")
        bbox = cut.split()[3].point(lambda a: 255 if a > 20 else 0).getbbox()
        if bbox:
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            # Trust the cut-out only when it still covers most of the garment box.
            if bw * bh >= 0.35 * crop.size[0] * crop.size[1]:
                return on_square(cut.crop(bbox)), "cut-out"
    return on_square(crop), "cropped"


# ---------------------------------------------------------------- reverse image search
WEB_STATE_FILE = STORAGE / ".helper_web_state.json"
DO_WEB = os.getenv("HELPER_WEB_LOOKUP", "true").lower() == "true"
WEB_DRY_RUN = os.getenv("HELPER_WEB_DRY_RUN", "false").lower() == "true"
VISION_KEY = os.getenv("VISION_API_KEY") or settings.ai_api_key
VISION_URL = "https://vision.googleapis.com/v1/images:annotate"

WEB_MODEL = os.getenv("HELPER_WEB_MODEL", "gemini-3.5-flash")
GEMINI_NATIVE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

WEB_PROMPT = """You MUST run Google Search before answering. Identify this exact clothing product.
Image 1 is the item (category: {item_type}); image 2 is the original photo it came from (may be a
shopping screenshot with the product name). Use any visible brand, logo, label, model, colour and
text, plus these reverse-image-search hints:
{results}

Search, read the results, then reply with ONLY a JSON object:
- "photo_kind": "worn" if image 2 shows the item on a person (model or selfie), "screenshot" for a
  shopping page/app screenshot, "product" otherwise
- "match": "exact" only if the search results show this same product (same design and colour);
  "similar" if you only found a close alternative; "none" otherwise
- "brand": brand name, or null
- "product_name": product name as sold, in Traditional Chinese if a Taiwan/Chinese page has it,
  max 40 characters, or null
- "material_text": material / fibre composition in Traditional Chinese, or null
- "price_text": listed price with currency as written (e.g. "NT$1,990"), or null
- "url": the product page URL you found, or null
Base every field only on the search results or text visible in the images; null when unsure.
Never use the ASCII double-quote character inside values."""


def load_web_state() -> dict:
    try:
        return json.loads(WEB_STATE_FILE.read_text())
    except Exception:
        return {}


def save_web_state(state: dict) -> None:
    tmp = WEB_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(WEB_STATE_FILE)


async def vision_web_detect(client: httpx.AsyncClient, img: Image.Image) -> dict:
    body = {
        "requests": [
            {
                "image": {"content": to_b64(img, 1024)},
                "features": [{"type": "WEB_DETECTION", "maxResults": 10}],
                "imageContext": {"webDetectionParams": {"includeGeoResults": True}},
            }
        ]
    }
    r = await client.post(VISION_URL, params={"key": VISION_KEY}, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Vision API {r.status_code}: {r.text[:300]}")
    web = r.json()["responses"][0].get("webDetection", {})
    return {
        "best_guess": [b.get("label") for b in web.get("bestGuessLabels", [])],
        "entities": [
            e.get("description") for e in web.get("webEntities", []) if e.get("description")
        ][:10],
        "pages": [
            {"title": p.get("pageTitle", "")[:200], "url": p.get("url")}
            for p in web.get("pagesWithMatchingImages", [])[:10]
        ],
    }


def _clean(v) -> str:
    v = (v or "").strip() if isinstance(v, str) else ""
    return "" if v.lower() in ("null", "none", "unknown", "n/a") else v


async def grounded_identify(
    client: httpx.AsyncClient, images: list, item_type: str, hints: dict
) -> dict:
    prompt = WEB_PROMPT.replace("{item_type}", item_type or "unknown").replace(
        "{results}", json.dumps(hints, ensure_ascii=False)
    )
    parts = [{"text": prompt}] + [
        {"inline_data": {"mime_type": "image/jpeg", "data": to_b64(im)}} for im in images
    ]
    body = {"contents": [{"parts": parts}], "tools": [{"google_search": {}}]}
    url = GEMINI_NATIVE.format(model=WEB_MODEL)
    for attempt in range(4):
        r = await client.post(url, params={"key": settings.ai_api_key}, json=body)
        if r.status_code == 429 or r.status_code >= 500:
            await asyncio.sleep(15 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini search {r.status_code}: {r.text[:200]}")
        cand = (r.json().get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}
    raise RuntimeError(f"Gemini search unavailable ({r.status_code})")


async def resolve_url(client: httpx.AsyncClient, url: str) -> str:
    # Search-grounding links are temporary Google redirects; store the real product page instead.
    if "grounding-api-redirect" not in url:
        return url
    try:
        r = await client.get(url, follow_redirects=False, timeout=20)
        return r.headers.get("location") or ""
    except Exception:
        return ""


async def web_lookup(session: AsyncSession, client: httpx.AsyncClient, item: ClothingItem) -> str:
    current = Image.open(STORAGE / item.image_path).convert("RGB")
    backup = item.original_image_path
    original = (
        Image.open(STORAGE / backup).convert("RGB")
        if backup and (STORAGE / backup).exists()
        else current
    )
    hints = {}
    try:
        hints = await vision_web_detect(client, original)
    except Exception as e:
        log.warning(f"web {item.id}: vision hints skipped: {e}")
    info = await grounded_identify(client, [current, original], item.type, hints)
    match = _clean(info.get("match")).lower() or "none"
    kind = _clean(info.get("photo_kind")).lower()
    if kind == "worn" and match == "exact":
        match = "similar"  # worn photos gave inconsistent answers in testing: hint only
    brand, name = _clean(info.get("brand")), _clean(info.get("product_name"))
    material, price = _clean(info.get("material_text")), _clean(info.get("price_text"))
    if price and not re.search(r"NT|TWD|新台幣|台幣|元", price, re.I):
        price = ""  # only keep Taiwan prices; foreign-site prices were unreliable
    url = _clean(info.get("url"))
    if url:
        url = await resolve_url(client, url)
    summary = f"kind={kind or '-'} match={match} brand={brand or '-'} name={name or '-'} material={material or '-'} price={price or '-'} url={url or '-'}"
    if WEB_DRY_RUN or match == "none":
        return summary + (" (dry run, not saved)" if WEB_DRY_RUN else "")

    lines = []
    if match == "exact":
        if brand and not item.brand:
            item.brand = brand[:100]
        if name and not item.name:
            item.name = name[:100]
        lines.append(f"以圖搜圖（AI 自動查詢，請自行確認）：{brand} {name}".strip())
        if material:
            lines.append(f"網路資料材質：{material}")
        if price:
            lines.append(f"網路售價：{price}")
    else:  # similar: never touch brand/name, only leave a hint
        lines.append(f"以圖搜圖（相似款，僅供參考）：{brand} {name}".strip())
    if url:
        lines.append(f"商品連結：{url}")
    new = [ln for ln in lines if ln not in (item.notes or "")]
    if new:
        item.notes = "\n".join(filter(None, [item.notes, *new]))
    await session.commit()
    return summary + " (saved)"


async def run_web_once(maker) -> None:
    if not DO_WEB:
        return
    tidy_state, state = load_state(), load_web_state()
    async with maker() as session, httpx.AsyncClient(timeout=180) as client:
        rows = await session.execute(
            select(ClothingItem).where(
                ClothingItem.status == ItemStatus.ready,
                ClothingItem.is_archived.is_(False),
            )
        )
        # Only items whose photo has already been tidied, so the search uses the clean image.
        items = [i for i in rows.scalars() if str(i.id) in tidy_state and str(i.id) not in state]
        only = [x for x in os.getenv("HELPER_WEB_ONLY", "").split(",") if x]
        if only:
            items = [i for i in items if any(str(i.id).startswith(o) for o in only)]
        limit = int(os.getenv("HELPER_WEB_LIMIT", "0"))
        if limit:
            items = items[:limit]
        for item in items:
            try:
                msg = await web_lookup(session, client, item)
                if not WEB_DRY_RUN:
                    state[str(item.id)] = msg[:300]
                log.info(f"web {item.id} ({item.type}): {msg}")
            except Exception as e:
                await session.rollback()
                log.warning(f"web {item.id}: failed, will retry next round: {e}")
                if "Gemini search 4" in str(e):
                    break  # key / API not enabled: stop instead of failing every item
            if not WEB_DRY_RUN:
                save_web_state(state)


async def process(session: AsyncSession, client: httpx.AsyncClient, item: ClothingItem) -> str:
    svc = ImageService()
    base = item.image_path.rsplit(".", 1)[0]
    backup = item.original_image_path or f"{base}_orig.jpg"
    if not (STORAGE / backup).exists():
        shutil.copy2(STORAGE / item.image_path, STORAGE / backup)
    source = ImageOps.exif_transpose(Image.open(STORAGE / backup)).convert("RGB")

    info = await ask_model(client, source, item.type)
    done = []

    if DO_INFO:
        brand = (info.get("brand") or "").strip()
        if brand and brand.lower() not in ("null", "none", "unknown") and not item.brand:
            item.brand = brand[:100]
            done.append(f"brand={brand}")
        mat_text = (info.get("material_text") or "").strip()
        if mat_text and mat_text.lower() != "null":
            line = f"材質標示：{mat_text}"
            if line not in (item.notes or ""):
                item.notes = f"{item.notes}\n{line}" if item.notes else line
                done.append(line)
            mat = (info.get("material") or "").strip().lower()
            if mat in MATERIALS:
                item.material = mat

    guessed = str(info.get("type") or "").strip().lower()
    if (item.type or "unknown") in ("unknown", "item", "") and guessed in TYPES:
        item.type = guessed
        done.append(f"type={guessed}")
    if info.get("is_eyewear") is True and item.type == "accessories" and not item.subtype:
        item.subtype = "glasses"
        done.append("subtype=glasses")

    if DO_IMAGES:
        kind = str(info.get("photo_kind") or "worn").lower()
        final, how = await asyncio.to_thread(tidy, source, info.get("box_2d"), kind)
        await asyncio.to_thread(svc._save_all_sizes, final, item.image_path)
        item.original_image_path = backup
        done.append(f"image {how} ({kind})")

    await session.commit()
    return ", ".join(done) or "nothing to change"


async def run_once(maker) -> None:
    state = load_state()
    async with maker() as session, httpx.AsyncClient(timeout=180) as client:
        rows = await session.execute(
            select(ClothingItem).where(
                ClothingItem.status == ItemStatus.ready,
                ClothingItem.ai_processed.is_(True),
                ClothingItem.is_archived.is_(False),
            )
        )
        items = [i for i in rows.scalars() if str(i.id) not in state]
        limit = int(os.getenv("HELPER_LIMIT", "0"))
        if limit:
            items = items[:limit]
        if items:
            log.info(f"{len(items)} new item(s) to process")
        for item in items:
            try:
                msg = await process(session, client, item)
                state[str(item.id)] = "ok"
                log.info(f"{item.id} ({item.type}): {msg}")
            except Exception as e:
                await session.rollback()
                log.warning(f"{item.id}: failed, will retry next round: {e}")
            save_state(state)


async def main() -> None:
    engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    once = "--once" in sys.argv
    while True:
        try:
            await run_once(maker)
        except Exception as e:
            log.error(f"round failed: {e}")
        try:
            await run_web_once(maker)
        except Exception as e:
            log.error(f"web round failed: {e}")
        if once:
            break
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
