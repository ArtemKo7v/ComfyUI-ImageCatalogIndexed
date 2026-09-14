"""ComfyUI node and HTTP endpoints for persistent image catalogs."""

import asyncio
from functools import wraps
from io import BytesIO
import json
import logging
from pathlib import Path
import tempfile
from urllib.parse import quote

from aiohttp import web
import folder_paths
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from server import PromptServer
import torch

from .catalog_store import CatalogConflict, CatalogError, CatalogStore, MAX_PROPERTIES
from .catalog_archive import MAX_ARCHIVE_BYTES, export_catalog, import_catalog

STORE = CatalogStore(Path(folder_paths.get_user_directory()) / "artemko7v_image_catalog-indexed")
API_PREFIX = "/artemko7v/image-catalog"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class PropertyOutputType(str):
    """Legacy ComfyUI validates dynamic output types in the frontend."""

    def __ne__(self, other):
        """Allow legacy frontend validation for dynamically typed outputs."""
        return False


def load_image(path):
    """Decode a stored PNG as a normalized RGB IMAGE tensor.

    Stored alpha is intentionally not exposed as a separate node output.
    """
    with Image.open(path) as source:
        rgb = ImageOps.exif_transpose(source).convert("RGB")
        pixels = np.asarray(rgb, dtype=np.float32) / 255.0
    return torch.from_numpy(pixels)[None,]


class ArtemKo7vImageCatalogIndexed:
    """ComfyUI output node backed by the persistent image catalog store."""

    CATEGORY = "image/catalog"
    FUNCTION = "execute"
    OUTPUT_NODE = True
    RETURN_TYPES = ("IMAGE",) + (PropertyOutputType("*"),) * MAX_PROPERTIES
    RETURN_NAMES = ("IMAGE",) + tuple(f"property_{index + 1}" for index in range(MAX_PROPERTIES))
    DESCRIPTION = "Select a catalog image by index and output its typed properties."

    @classmethod
    def INPUT_TYPES(cls):
        """Declare inputs consumed by ComfyUI."""
        return {"required": {
            "image_index": ("INT", {"default": 0, "min": 0, "max": 9_007_199_254_740_991}),
            "catalog_state": ("STRING", {"default": "{}", "multiline": False, "socketless": True}),
        }}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """Disable result caching for state stored outside the workflow."""
        # Catalog files and save operations are external to ComfyUI's input cache.
        return float("nan")

    def execute(self, image_index, catalog_state):
        """Run the catalog transaction and shape the ComfyUI result."""
        try:
            state = json.loads(catalog_state)
        except (ValueError, TypeError) as error:
            raise CatalogError("Invalid catalog state JSON.") from error
        if not isinstance(state, dict) or not state.get("catalog_id"):
            raise CatalogError("Select or create an image catalog before running.")
        result = STORE.execute(state["catalog_id"], image_index, state, load_image)
        if result["image"] is None:
            from comfy_execution.graph import ExecutionBlocker

            outputs = (ExecutionBlocker(None),) * (MAX_PROPERTIES + 1)
        else:
            outputs = (result["image"], *result["values"])
            outputs += (None,) * (MAX_PROPERTIES + 1 - len(outputs))
        return {"ui": {"catalog_result": [{key: value for key, value in result.items()
                                          if key not in ("image", "values")}]}, "result": outputs}


def route_errors(handler):
    """Convert domain errors to stable API responses and log storage failures."""
    @wraps(handler)
    async def wrapped(request):
        """Translate expected domain errors from the wrapped HTTP handler."""
        try:
            return await handler(request)
        except CatalogConflict as error:
            return web.json_response({"error": str(error)}, status=409)
        except (CatalogError, ValueError, TypeError, KeyError, UnidentifiedImageError,
                Image.DecompressionBombError) as error:
            return web.json_response({"error": str(error)}, status=400)
        except OSError:
            logging.exception("Image catalog storage error")
            return web.json_response({"error": "Storage operation failed. Check the server log."}, status=500)
    return wrapped


routes = PromptServer.instance.routes


@routes.get(API_PREFIX + "/catalogs")
@route_errors
async def list_catalogs(request):
    """Return catalog summaries for the frontend selector."""
    return web.json_response(await asyncio.to_thread(STORE.list))


@routes.post(API_PREFIX + "/catalogs")
@route_errors
async def create_catalog(request):
    """Create a catalog from the submitted JSON name and schema."""
    body = await request.json()
    if not isinstance(body, dict):
        raise CatalogError("Catalog request must be a JSON object.")
    catalog = await asyncio.to_thread(STORE.create, body.get("name"), body.get("schema"))
    return web.json_response(catalog, status=201)


@routes.get(API_PREFIX + "/catalogs/{catalog_id}")
@route_errors
async def get_catalog(request):
    """Return full metadata for one catalog."""
    return web.json_response(await asyncio.to_thread(STORE.get, request.match_info["catalog_id"]))


@routes.patch(API_PREFIX + "/catalogs/{catalog_id}/schema")
@route_errors
async def update_catalog_schema(request):
    """Apply a revision-checked schema update."""
    body = await request.json()
    if not isinstance(body, dict):
        raise CatalogError("Catalog request must be a JSON object.")
    data = await asyncio.to_thread(STORE.update_schema, request.match_info["catalog_id"],
                                   body.get("schema"), body.get("expected_revision"))
    return web.json_response(data)


@routes.get(API_PREFIX + "/catalogs/{catalog_id}/export")
@route_errors
async def download_catalog(request):
    """Stream an exported catalog archive to the browser."""
    stream, name = await asyncio.to_thread(export_catalog, STORE, request.match_info["catalog_id"])
    try:
        response = web.StreamResponse(headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f"attachment; filename=\"image-catalog.zip\"; filename*=UTF-8''{quote(name, safe='')}.zip",
        })
        await response.prepare(request)
        while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
            await response.write(chunk)
        await response.write_eof()
        return response
    finally:
        stream.close()


def validate_imported_image(path):
    """Verify an extracted archive member is a valid PNG."""
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise CatalogError("Catalog archives must contain PNG images.")
            image.verify()
    except (OSError, SyntaxError) as error:
        raise CatalogError("Catalog archive contains an invalid PNG image.") from error


@routes.post(API_PREFIX + "/import")
@route_errors
async def upload_catalog_archive(request):
    """Receive and import a bounded ZIP upload."""
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "archive" or not part.filename:
        raise CatalogError("A catalog ZIP file is required.")
    with tempfile.TemporaryFile(mode="w+b") as stream:
        size = 0
        while chunk := await part.read_chunk(1024 * 1024):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise CatalogError("Catalog ZIP uploads cannot exceed 512 MiB.")
            await asyncio.to_thread(stream.write, chunk)
        stream.seek(0)
        data = await asyncio.to_thread(import_catalog, STORE, stream, validate_imported_image)
    return web.json_response(data, status=201)


@routes.delete(API_PREFIX + "/catalogs/{catalog_id}")
@route_errors
async def delete_catalog(request):
    """Delete a catalog after explicit identifier confirmation."""
    body = await request.json()
    if not isinstance(body, dict):
        raise CatalogError("Catalog request must be a JSON object.")
    if body.get("confirm") != request.match_info["catalog_id"]:
        raise CatalogError("Catalog deletion requires confirmation.")
    await asyncio.to_thread(STORE.delete, request.match_info["catalog_id"])
    return web.json_response({"deleted": True})


@routes.get(API_PREFIX + "/catalogs/{catalog_id}/images/{entry_id}")
@route_errors
async def preview_image(request):
    """Serve one stored catalog image as a PNG preview."""
    path = await asyncio.to_thread(STORE.image_path, request.match_info["catalog_id"], request.match_info["entry_id"])
    return web.FileResponse(path, headers={"Content-Type": "image/png", "X-Content-Type-Options": "nosniff"})


def stage_image(catalog_id, token, content, filename):
    """Decode, orient, and normalize before staging so storage is always PNG."""
    with Image.open(BytesIO(content)) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in source.info else "RGB")
        target = BytesIO()
        image.save(target, format="PNG")
    STORE.stage(catalog_id, token, target.getvalue(), filename)


@routes.post(API_PREFIX + "/catalogs/{catalog_id}/uploads/{token}")
@route_errors
async def upload_image(request):
    """Receive and normalize one bounded image upload."""
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "image" or not part.filename:
        raise CatalogError("An image file is required.")
    content = bytearray()
    while chunk := await part.read_chunk():
        content.extend(chunk)
        if len(content) > MAX_UPLOAD_BYTES:
            raise CatalogError("Image uploads cannot exceed 64 MiB.")
    await asyncio.to_thread(stage_image, request.match_info["catalog_id"], request.match_info["token"], content, part.filename)
    return web.json_response({"token": request.match_info["token"]})


NODE_CLASS_MAPPINGS = {"ArtemKo7vImageCatalogIndexed": ArtemKo7vImageCatalogIndexed}
NODE_DISPLAY_NAME_MAPPINGS = {"ArtemKo7vImageCatalogIndexed": "Image Catalog Indexed"}
