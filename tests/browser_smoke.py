"""Exercise the real catalog UI and HTTP handlers with a minimal ComfyUI host."""

import asyncio
from io import BytesIO
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from aiohttp import web
from PIL import Image
from playwright.async_api import async_playwright, expect

from support import ROOT, load_backend


async def main():
    with tempfile.TemporaryDirectory() as temporary:
        backend, substitutes = load_backend(Path(temporary))
        application = web.Application()
        application.add_routes(backend.routes)
        async def index(request):
            return web.FileResponse(ROOT / "tests/browser/index.html")
        async def queue(request):
            body = await request.json()
            inputs = body["output"]["1"]["inputs"]
            try:
                with patch.dict("sys.modules", substitutes):
                    result = await asyncio.to_thread(backend.ArtemKo7vImageCatalogIndexed().execute, **inputs)
                values = result["result"]
                properties = [value if isinstance(value, (str, int, bool)) else None for value in values[1:]]
                return web.json_response({"ui": result["ui"], "outputs": [list(values[0].shape) if hasattr(values[0], "shape") else None, *properties]})
            except Exception as error:
                return web.json_response({"error": str(error)}, status=400)
        application.router.add_get("/", index)
        application.router.add_post("/test/queue", queue)
        application.router.add_static("/scripts/", ROOT / "tests/browser")
        application.router.add_static("/extensions/catalog/", ROOT / "js")
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    channel=os.environ.get("PLAYWRIGHT_CHANNEL") or None, headless=True
                )
                try:
                    page = await browser.new_page(viewport={"width": 1000, "height": 1100})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(f"http://127.0.0.1:{port}/")
                    await expect(page.get_by_label("Index after queue", exact=True)).to_be_visible()
                    assert await page.evaluate("window.catalogNode.widgets.slice(0, 2).map(widget => widget.name)") == ["image_index", "index_after_queue"]
                    assert await page.locator(".image-catalog").get_by_label("Index after queue", exact=True).count() == 0
                    await page.get_by_placeholder("Catalog name").fill("Browser example")
                    await page.get_by_placeholder("Property name").nth(0).fill("caption")
                    await page.locator(".ic-row select").nth(0).select_option("Text")
                    await page.get_by_placeholder("Property name").nth(1).fill("seed")
                    await page.locator(".ic-row select").nth(1).select_option("Integer")
                    await page.get_by_placeholder("Property name").nth(2).fill("enabled")
                    await page.locator(".ic-row select").nth(2).select_option("Boolean")
                    await page.get_by_role("button", name="Create Now", exact=True).click()
                    await expect(page.get_by_label("Add image", exact=True)).to_be_visible()
                    png = BytesIO()
                    Image.new("RGB", (320, 200), (55, 110, 190)).save(png, format="PNG")
                    await page.get_by_label("Add image", exact=True).set_input_files({"name": "first.png", "mimeType": "image/png", "buffer": png.getvalue()})
                    await page.get_by_label("caption", exact=True).fill("First line\nSecond line")
                    await page.get_by_label("seed", exact=True).fill("123")
                    await page.get_by_label("enabled", exact=True).check()
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await expect(page.get_by_label("Save changes", exact=True)).to_be_visible()
                    assert await page.evaluate("window.lastOutputs.slice(0, 4)") == [[1, 200, 320, 3], "First line\nSecond line", 123, True]
                    await expect(page.locator(".ic-preview")).to_be_visible()
                    assert await page.locator(".ic-preview").evaluate("image => image.complete && image.naturalWidth === 320")
                    await page.get_by_label("caption", exact=True).fill("Unsaved caption")
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await page.wait_for_function("window.lastOutputs[1] === 'Unsaved caption'")
                    assert backend.STORE.list()["catalogs"][0]["count"] == 1
                    catalog_id = backend.STORE.list()["catalogs"][0]["id"]
                    assert backend.STORE.get(catalog_id)["entries"][0]["values"]["caption"] == "First line\nSecond line"
                    await page.get_by_label("Save changes", exact=True).check()
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await page.wait_for_function("Object.keys(window.catalogNode.imageCatalog.state.edits).length === 0")
                    assert backend.STORE.get(catalog_id)["entries"][0]["values"]["caption"] == "Unsaved caption"
                    await page.get_by_role("button", name="Restore workflow", exact=True).click()
                    await expect(page.get_by_label("caption", exact=True)).to_have_value("Unsaved caption")
                    assert await page.evaluate("window.catalogNode.outputs.map(output => output.type)") == ["IMAGE", "STRING", "INT", "BOOLEAN"]
                    await page.get_by_label("Hide", exact=True).check()
                    await expect(page.get_by_label("caption", exact=True)).to_be_disabled()
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await expect(page.locator(".ic-status")).to_contain_text("No visible images")
                    await page.get_by_label("Hide", exact=True).uncheck()
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await expect(page.locator(".ic-status")).to_contain_text("completed")
                    await page.get_by_role("button", name="Add image", exact=True).click()
                    await page.get_by_label("Add image", exact=True).set_input_files({"name": "second.png", "mimeType": "image/png", "buffer": png.getvalue()})
                    await page.get_by_label("caption", exact=True).fill("Second image")
                    await page.get_by_label("seed", exact=True).fill("222")
                    await page.get_by_role("button", name="Add next image", exact=True).click()
                    await page.get_by_label("Add image", exact=True).set_input_files({"name": "discard.png", "mimeType": "image/png", "buffer": png.getvalue()})
                    await page.get_by_role("button", name="Remove draft", exact=True).click()
                    await expect(page.get_by_label("caption", exact=True)).to_have_value("Second image")
                    await page.get_by_role("button", name="Add next image", exact=True).click()
                    await page.get_by_label("Add image", exact=True).set_input_files({"name": "third.png", "mimeType": "image/png", "buffer": png.getvalue()})
                    await page.get_by_label("caption", exact=True).fill("Third image")
                    await page.get_by_label("seed", exact=True).fill("333")
                    draft_tokens = await page.evaluate("window.catalogNode.imageCatalog.state.newImages.map(image => image.token)")
                    await page.get_by_label("Image", exact=True).select_option("new:" + draft_tokens[0])
                    await expect(page.get_by_label("caption", exact=True)).to_have_value("Second image")
                    await expect(page.get_by_label("seed", exact=True)).to_have_value("222")
                    await page.get_by_label("Image", exact=True).select_option("new:" + draft_tokens[1])
                    await expect(page.get_by_label("caption", exact=True)).to_have_value("Third image")
                    assert backend.STORE.list()["catalogs"][0]["count"] == 1
                    assert not list((backend.STORE.root / ".staging").iterdir())
                    await page.get_by_label("Index after queue", exact=True).select_option("increment")
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await expect(page.get_by_label("Save changes", exact=True)).to_be_visible()
                    assert backend.STORE.list()["catalogs"][0]["count"] == 3
                    entries = backend.STORE.get(catalog_id)["entries"]
                    assert [(entry["filename"], entry["values"]["seed"]) for entry in entries[1:]] == [("second.png", 222), ("third.png", 333)]
                    assert await page.evaluate("window.lastOutputs[1]") == "Third image"
                    assert await page.evaluate("window.catalogNode.imageCatalog.state.newImages.length") == 0
                    assert await page.evaluate("window.catalogNode.widgets[0].value") == 0
                    await page.get_by_role("button", name="Restore workflow", exact=True).click()
                    await expect(page.get_by_label("Index after queue", exact=True)).to_have_value("increment")
                    await expect(page.get_by_label("caption", exact=True)).to_be_visible()
                    page.on("dialog", lambda dialog: dialog.accept())
                    assert await page.locator(".ic-catalog-actions").evaluate("actions => actions.previousElementSibling.tagName") == "HR"
                    await page.evaluate("window.catalogNode.outputs[3].links = [77]")
                    await page.get_by_role("button", name="Edit Catalog", exact=True).click()
                    await page.locator(".ic-schema-field").nth(0).get_by_label("Hidden", exact=True).check()
                    await page.locator(".ic-schema-field").nth(1).get_by_role("button", name="Remove property", exact=True).click()
                    await page.get_by_role("button", name="Add property", exact=True).click()
                    await page.get_by_label("Property 3 name", exact=True).fill("rating")
                    await page.get_by_label("Property 3 type", exact=True).select_option("Integer")
                    await page.get_by_role("button", name="Save Catalog", exact=True).click()
                    await expect(page.get_by_label("rating", exact=True)).to_have_value("0")
                    await expect(page.get_by_label("caption", exact=True)).to_have_count(0)
                    await expect(page.get_by_label("seed", exact=True)).to_have_count(0)
                    assert await page.evaluate("window.catalogNode.outputs.map(output => output.name)") == ["IMAGE", "caption", "enabled", "rating"]
                    assert await page.evaluate("window.catalogNode.outputs[2].links") == [77]
                    updated = backend.STORE.get(catalog_id)
                    assert all("seed" not in entry["values"] and entry["values"]["rating"] == 0 for entry in updated["entries"])
                    await page.get_by_role("button", name="Edit Catalog", exact=True).click()
                    await page.locator(".ic-schema-field").nth(0).get_by_label("Hidden", exact=True).uncheck()
                    await page.get_by_role("button", name="Save Catalog", exact=True).click()
                    await expect(page.get_by_label("caption", exact=True)).to_be_visible()
                    async with page.expect_download() as download_event:
                        await page.get_by_role("button", name="Export Catalog", exact=True).click()
                    download = await download_event.value
                    archive_path = await download.path()
                    await page.get_by_label("Import catalog ZIP", exact=True).set_input_files(archive_path)
                    await expect(page.locator(".ic-status")).to_have_text("Catalog imported.")
                    assert len(backend.STORE.list()["catalogs"]) == 2
                    imported_id = await page.evaluate("window.catalogNode.imageCatalog.state.catalogId")
                    assert imported_id != catalog_id
                    imported = backend.STORE.get(imported_id)
                    original = backend.STORE.get(catalog_id)
                    assert imported["schema"] == original["schema"]
                    assert [entry["values"] for entry in imported["entries"]] == [entry["values"] for entry in original["entries"]]
                    screenshots = ROOT / "test-results"
                    screenshots.mkdir(exist_ok=True)
                    await page.screenshot(path=str(screenshots / "catalog-browser-smoke.png"), full_page=True)
                    await page.get_by_role("button", name="Delete catalog", exact=True).click()
                    await expect(page.get_by_role("button", name="Create Now", exact=True)).to_be_visible()
                    assert len(backend.STORE.list()["catalogs"]) == 1
                    await page.get_by_label("Catalog", exact=True).select_option(catalog_id)
                    await page.get_by_role("button", name="Delete catalog", exact=True).click()
                    await expect(page.get_by_role("button", name="Create Now", exact=True)).to_be_visible()
                    assert backend.STORE.list()["catalogs"] == []
                    await expect(page.get_by_label("Index after queue", exact=True)).to_have_value("increment")
                    assert errors == [], errors
                    print("Browser smoke passed: drafts, typed values, restore, schema add/remove/hide, preserved outputs, ZIP export/import, action divider, deletion.")
                finally:
                    await browser.close()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
