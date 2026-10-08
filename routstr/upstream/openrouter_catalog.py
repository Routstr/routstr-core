"""Shared OpenRouter discovery; optional modality outages never lose text models."""

import asyncio
import time
from collections import Counter

import httpx
from pydantic.v1 import ValidationError

from ..core.logging import get_logger
from ..modalities import ApiCapability, ModalityEndpoint

logger = get_logger(__name__)
BASE_URL = "https://openrouter.ai/api/v1"
IMAGE_ENDPOINT_CONCURRENCY = 6


def parse_models_response(response: httpx.Response | BaseException) -> list[dict]:
    if isinstance(response, BaseException):
        raise response
    response.raise_for_status()
    data = response.json().get("data", [])
    if not isinstance(data, list):
        raise ValueError("OpenRouter catalogue data must be a list")
    return [
        model
        for model in data
        if isinstance(model, dict)
        and isinstance(model.get("id"), str)
        and ":free" not in model["id"].lower()
    ]


async def fetch_openrouter_catalog(
    client: httpx.AsyncClient,
    timeout: float = 10,
) -> list[dict]:
    """One attempt: main catalogue is required, embeddings/images best-effort.

    Only attach image metadata to general records: the image catalogue omits
    required architecture/context fields and must not fabricate chat records.
    Endpoint URLs are constructed locally, never taken from upstream JSON.
    """
    main, embeddings, images = await asyncio.gather(
        client.get(
            f"{BASE_URL}/models",
            params={"output_modalities": "text,image"},
            timeout=timeout,
        ),
        client.get(f"{BASE_URL}/embeddings/models", timeout=timeout),
        client.get(f"{BASE_URL}/images/models", timeout=timeout),
        return_exceptions=True,
    )
    models = parse_models_response(main)
    try:
        models.extend(parse_models_response(embeddings))
    except Exception as exc:
        logger.warning(f"Skipping OpenRouter embeddings models: {exc}")
    try:
        image_models = parse_models_response(images)
    except Exception as exc:
        logger.warning(f"Skipping OpenRouter image capabilities: {exc}")
        image_models = []

    by_id = {model["id"]: model for model in image_models}
    semaphore = asyncio.Semaphore(IMAGE_ENDPOINT_CONCURRENCY)

    async def enrich(model: dict) -> dict:
        image = by_id.get(model["id"])
        if image is None:
            return model
        model_id = model["id"]
        # A model id is a vendor/model slug, never an arbitrary credentialed URL.
        if any(part in {"", ".", ".."} for part in model_id.split("/")) or any(
            character in model_id for character in "?%#\\"
        ):
            return model
        try:
            async with semaphore:
                response = await client.get(
                    f"{BASE_URL}/images/models/{model_id}/endpoints", timeout=timeout
                )
                response.raise_for_status()
                payload = response.json()
            endpoints = payload.get("endpoints")
            if not isinstance(endpoints, list):
                raise ValueError("image endpoints must be a list")
            # Pinning chooses a provider tag, not a particular raw record.
            # Count BEFORE validation: dropping an invalid sibling must never
            # make an ambiguous tag appear safe to bill against.
            tags = []
            for endpoint in endpoints:
                if not isinstance(endpoint, dict):
                    raise ValueError("unidentifiable image endpoint")
                tag = endpoint.get("provider_tag")
                if tag is not None and (not isinstance(tag, str) or not tag.strip()):
                    raise ValueError("unidentifiable image provider tag")
                if "provider_tag" not in endpoint:
                    raise ValueError("missing image provider tag")
                tags.append(tag)
            tag_counts = Counter(tags)
            valid_endpoints = []
            for endpoint in endpoints:
                if tag_counts[endpoint["provider_tag"]] > 1:
                    logger.warning(
                        f"Skipping ambiguous image provider tag for {model_id}"
                    )
                    continue
                try:
                    valid_endpoints.append(ModalityEndpoint.parse_obj(endpoint))
                except (ValidationError, TypeError, ValueError):
                    # One malformed rate invalidates its whole serving endpoint.
                    logger.warning(f"Skipping malformed image endpoint for {model_id}")
            capability = ApiCapability(
                fetched_at=int(time.time()),
                supported_parameters=image.get("supported_parameters", {}),
                supports_streaming=image.get("supports_streaming", False),
                endpoints=valid_endpoints,
            )
            return {**model, "api_capabilities": {"images": capability.dict()}}
        except Exception as exc:
            logger.warning(f"Skipping image endpoint metadata for {model_id}: {exc}")
            return model

    # Bound total enrichment time as well as concurrent sockets. A slow image
    # provider must not turn a ten-second catalogue refresh into many batches
    # of ten-second endpoint waits.
    tasks = [asyncio.create_task(enrich(model)) for model in models]
    done, pending = (
        await asyncio.wait(tasks, timeout=timeout) if tasks else (set(), set())
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return [
        task.result() if task in done else model for task, model in zip(tasks, models)
    ]
