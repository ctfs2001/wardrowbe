import asyncio
import base64
import json
from io import BytesIO
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from arq import Retry
from PIL import Image, ImageDraw
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.item import ClothingItem, ItemStatus
from app.services.ai_service import AIService, ClothingTags, ProductPhotoAssessment
from app.services.image_service import ImageService
from app.services.product_photo_service import ProductPhotoService
from app.workers.tagging import tag_item_image


def _png(mode="RGBA", color=(0, 0, 0, 0)):
    image = Image.new(mode, (128, 128), color)
    ImageDraw.Draw(image).rectangle((32, 16, 96, 112), fill="blue")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _assessment(needs_redraw=True, confidence=0.95):
    return ProductPhotoAssessment(
        needs_redraw=needs_redraw, confidence=confidence, reason="Hanging and wrinkled"
    )


@pytest.mark.parametrize(
    "assessment",
    [
        {"needs_redraw": "false", "confidence": 0.99, "reason": "invalid boolean"},
        {"needs_redraw": True, "confidence": 1.1, "reason": "invalid confidence"},
        {"needs_redraw": True},
    ],
)
def test_invalid_assessment_cannot_trigger_paid_generation(assessment):
    service = AIService()
    tags = service._parse_tags_from_response(
        json.dumps(
            {
                "type": "jacket",
                "primary_color": "blue",
                "product_photo": assessment,
            }
        )
    )
    assert tags.type == "jacket"
    assert tags.product_photo is None


def test_old_provider_responses_remain_compatible():
    tags = AIService()._parse_tags_from_response('{"type":"jacket"}')
    assert tags.product_photo is None


def test_valid_assessment_is_parsed():
    tags = AIService()._parse_tags_from_response(
        json.dumps(
            {
                "type": "jacket",
                "product_photo": _assessment().model_dump(),
            }
        )
    )
    assert tags.product_photo == _assessment()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-image-1.5", "gpt-image-2"])
async def test_image_edit_sends_reference_and_requires_transparent_png(
    tmp_path, monkeypatch, model
):
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_image_base_url", "https://image.test/v1/")
    monkeypatch.setattr(settings, "ai_image_api_key", "test-image-key")
    monkeypatch.setattr(settings, "ai_image_model", model)
    source = tmp_path / "source.jpg"
    source.write_bytes(b"reference bytes")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "b64_json": base64.b64encode(_png()).decode(),
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("app.services.product_photo_service.httpx.AsyncClient", return_value=client):
        output = await ProductPhotoService().redraw(source)
    assert output == _png()
    assert len(requests) == 1
    assert str(requests[0].url) == "https://image.test/v1/images/edits"
    assert requests[0].headers["Authorization"] == "Bearer test-image-key"
    payload = json.loads(requests[0].content)
    assert payload["images"][0]["image_url"].endswith(
        base64.b64encode(source.read_bytes()).decode()
    )
    assert payload["background"] == "transparent"
    assert payload["output_format"] == "png"
    assert ("input_fidelity" in payload) == (model == "gpt-image-1.5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"data": [{"url": "http://private.test/photo.png"}]}),
        httpx.Response(200, json={"data": [{"b64_json": "invalid-base64"}]}),
        httpx.Response(200, json={"error": {"message": "provider failed"}}),
        httpx.Response(429, json={"error": "rate limited"}),
        httpx.Response(302, headers={"Location": "https://elsewhere.test"}),
    ],
)
async def test_provider_failure_never_retries_or_fetches_urls(tmp_path, monkeypatch, response):
    monkeypatch.setattr(get_settings(), "ai_image_base_url", "https://image.test/v1")
    source = tmp_path / "source.jpg"
    source.write_bytes(b"reference")
    requests = []

    def respond(request):
        requests.append(request)
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("app.services.product_photo_service.httpx.AsyncClient", return_value=client):
        with pytest.raises((ValueError, httpx.HTTPError)):
            await ProductPhotoService().redraw(source)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_png_variants_restore_and_later_background_removal_keep_original(tmp_path):
    service = ImageService(str(tmp_path))
    original = await service.process_and_store(uuid4(), _png(), "upload.png")
    source_bytes = service.get_image_path(original["image_path"]).read_bytes()
    result = service.store_product_photo(_png(), original["image_path"])
    for field in ("image_path", "medium_path", "thumbnail_path"):
        with Image.open(service.get_image_path(result[field])) as image:
            assert image.format == "PNG"
            assert image.getchannel("A").getextrema() == (0, 255)
    assert service.get_image_path(result["original_image_path"]).read_bytes() == source_bytes
    # Rotation must not flatten the generated photo's alpha channel.
    service.rotate_image(result["image_path"])
    assert Image.open(service.get_image_path(result["image_path"])).getchannel(
        "A"
    ).getextrema() == (0, 255)
    provider = type("Provider", (), {"remove": lambda self, image: image.convert("RGBA")})()
    with patch("app.services.background_removal.get_provider", return_value=provider):
        removal = service.remove_background(
            result["image_path"], original_backup_path=result["original_image_path"]
        )
    assert removal["original_backup_path"] == result["original_image_path"]
    assert service.get_image_path(result["original_image_path"]).read_bytes() == source_bytes
    restored = service.restore_original(result["image_path"], result["original_image_path"])
    for field in ("image_path", "medium_path", "thumbnail_path"):
        image = Image.open(service.get_image_path(restored[field]))
        assert image.format == "PNG"
        assert image.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize(
    "content",
    [b"not an image", _png("RGB", "white"), _png("RGBA", (0, 0, 0, 0)).replace(b"PNG", b"BAD", 1)],
)
def test_invalid_generated_images_leave_source_untouched(tmp_path, content):
    service = ImageService(str(tmp_path))
    source = tmp_path / "source.jpg"
    source.write_bytes(b"original")
    with pytest.raises((ValueError, OSError)):
        service.store_product_photo(content, "source.jpg")
    assert source.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "generate",
        "catalog",
        "uncertain",
        "no_assessment",
        "disabled",
        "retag",
        "timeout",
        "opaque",
        "replace",
        "cancel",
        "new_job",
        "enqueue_race",
        "previous_claim",
    ],
)
async def test_tagging_photo_lifecycle(
    db_session, session_maker, test_user, monkeypatch, client, auth_headers, scenario
):
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_product_photo_enabled", scenario != "disabled")
    service = ImageService()
    paths = await service.process_and_store(test_user.id, _png(), "source.png")
    item = ClothingItem(
        user_id=test_user.id,
        type="unknown",
        status=ItemStatus.processing,
        ai_processed=scenario == "retag",
        ai_job_id=None if scenario == "enqueue_race" else "photo-job",
        tags={"product_photo": {**_assessment().model_dump(), "status": "generating"}}
        if scenario == "previous_claim"
        else {},
        **paths,
    )
    db_session.add(item)
    await db_session.commit()
    item_id = item.id
    source = service.get_image_path(item.image_path)
    source_bytes = source.read_bytes()
    assessment = (
        None
        if scenario == "no_assessment"
        else _assessment(
            needs_redraw=scenario != "catalog", confidence=0.5 if scenario == "uncertain" else 0.95
        )
    )

    async def redraw(path):
        if scenario == "enqueue_race":
            await db_session.execute(
                update(ClothingItem).where(ClothingItem.id == item_id).values(ai_job_id="photo-job")
            )
            await db_session.commit()
        if scenario == "timeout":
            raise TimeoutError("possibly billed")
        if scenario in {"replace", "cancel", "new_job"}:
            values = (
                {"image_path": "replacement.jpg"}
                if scenario == "replace"
                else (
                    {"status": ItemStatus.ready}
                    if scenario == "cancel"
                    else {"ai_job_id": "new-job"}
                )
            )
            await db_session.execute(
                update(ClothingItem).where(ClothingItem.id == item_id).values(**values)
            )
            await db_session.commit()
        return _png("RGB", "white") if scenario == "opaque" else _png()

    with (
        patch.object(AIService, "analyze_image", new_callable=AsyncMock) as analyze,
        patch.object(ProductPhotoService, "redraw", side_effect=redraw) as edit,
    ):
        analyze.return_value = ClothingTags(type="jacket", product_photo=assessment)
        result = await tag_item_image(
            {"db_session_factory": session_maker, "job_id": "photo-job"}, str(item_id), str(source)
        )
    assert result["status"] == (
        "skipped" if scenario in {"replace", "cancel", "new_job"} else "success"
    )
    await db_session.refresh(item)
    if scenario in {"catalog", "uncertain", "no_assessment", "disabled", "retag", "previous_claim"}:
        edit.assert_not_called()
    else:
        edit.assert_called_once()
    if scenario in {"generate", "enqueue_race"}:
        assert item.image_path.endswith(".png")
        assert item.tags["product_photo"]["status"] == "generated"
        assert item.image_hash == paths["image_hash"]
        assert service.get_image_path(item.original_image_path).read_bytes() == source_bytes
        assert not source.exists()
        restored = await client.post(
            f"/api/v1/items/{item_id}/restore-original", headers=auth_headers
        )
        assert restored.status_code == 200
        assert restored.json()["original_image_path"] is None
        assert Image.open(service.get_image_path(item.image_path)).format == "PNG"
    else:
        assert source.read_bytes() == source_bytes
        assert item.original_image_path is None
        if scenario in {"timeout", "opaque", "previous_claim"}:
            assert item.tags["product_photo"]["status"] == "failed"
        if scenario == "replace":
            assert item.image_path == "replacement.jpg"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["worker_death", "final_commit"])
async def test_worker_retry_never_buys_a_second_generation(
    db_session, session_maker, test_user, monkeypatch, failure
):
    monkeypatch.setattr(get_settings(), "ai_product_photo_enabled", True)
    service = ImageService()
    paths = await service.process_and_store(test_user.id, _png(), "source.png")
    item = ClothingItem(user_id=test_user.id, type="unknown", status=ItemStatus.processing, **paths)
    db_session.add(item)
    await db_session.commit()
    item_id = str(item.id)
    source = service.get_image_path(item.image_path)
    source_bytes = source.read_bytes()
    ctx = {"db_session_factory": session_maker, "job_id": "photo-job"}
    commit = AsyncSession.commit
    commits = 0

    async def flaky_commit(session):
        nonlocal commits
        commits += 1
        if failure == "final_commit" and commits == 3:
            raise RuntimeError("database write failed")
        await commit(session)

    with (
        patch.object(AIService, "analyze_image", new_callable=AsyncMock) as analyze,
        patch.object(ProductPhotoService, "redraw", new_callable=AsyncMock) as edit,
        patch.object(AsyncSession, "commit", flaky_commit),
    ):
        analyze.return_value = ClothingTags(type="jacket", product_photo=_assessment())
        if failure == "worker_death":
            edit.side_effect = asyncio.CancelledError()
        else:
            edit.return_value = _png()
        with pytest.raises(asyncio.CancelledError if failure == "worker_death" else Retry):
            await tag_item_image(ctx, item_id, str(source))
        result = await tag_item_image({**ctx, "job_try": 2}, item_id, str(source))
        assert result["status"] == "success"
        edit.assert_awaited_once()
    await db_session.refresh(item)
    assert item.status == ItemStatus.ready
    assert item.tags["product_photo"]["status"] == "failed"
    assert source.read_bytes() == source_bytes
    assert not list(source.parent.glob("*_orig.*"))
