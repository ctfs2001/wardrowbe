"""Reference-based image editing, with no automatic paid-request retries."""

import base64
import json
from pathlib import Path

import httpx

from app.config import get_settings
from app.utils.prompts import load_prompt

MAX_RESPONSE_BYTES = 40 * 1024 * 1024


class ProductPhotoService:
    async def redraw(self, image_path: Path) -> bytes:
        settings = get_settings()
        if not settings.ai_image_base_url:
            raise ValueError("AI_IMAGE_BASE_URL is required for product photos")
        data = {
            "model": settings.ai_image_model,
            "images": [
                {
                    "image_url": "data:image/jpeg;base64,"
                    + base64.b64encode(image_path.read_bytes()).decode("ascii")
                }
            ],
            "prompt": load_prompt("product_photo"),
            "background": "transparent",
            "output_format": "png",
            "quality": "high",
            "size": "1024x1024",
            "n": 1,
        }
        if settings.ai_image_model in {"gpt-image-1", "gpt-image-1.5"}:
            data["input_fidelity"] = "high"
        headers = {}
        if settings.ai_image_api_key:
            headers["Authorization"] = f"Bearer {settings.ai_image_api_key}"
        # No redirects: do not forward the photo or credentials to another host.
        async with httpx.AsyncClient(timeout=settings.ai_image_timeout) as client:
            async with client.stream(
                "POST",
                f"{settings.ai_image_base_url.rstrip('/')}/images/edits",
                headers=headers,
                json=data,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("Image provider response is too large")
        result = json.loads(body)
        # Require embedded bytes: arbitrary provider URLs are never fetched.
        images = result.get("data")
        if not isinstance(images, list) or not images or not isinstance(images[0], dict):
            raise ValueError("Image provider returned no image")
        encoded = images[0].get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Image provider returned no embedded image")
        return base64.b64decode(encoded, validate=True)
