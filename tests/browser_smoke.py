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
                    page.on("dialog", lambda dialog: dialog.accept())
                    await expect(page.get_by_label("Index after queue", exact=True)).to_be_visible()
                    assert await page.evaluate("window.catalogNode.widgets.slice(0, 2).map(widget => widget.name)") == ["image_index", "index_after_queue"]
                    await expect(page.locator(".ic-image-block")).to_have_count(0)
                    await expect(page.locator(".ic-selector-row").get_by_role("button", name="Refresh", exact=True)).to_be_visible()
                    await expect(page.get_by_role("button", name="Import Catalog", exact=True)).to_be_visible()
                    await page.get_by_placeholder("Catalog name").fill("Browser example")
                    for index, (name, kind) in enumerate([("caption", "Text"), ("seed", "Integer"), ("enabled", "Boolean")]):
                        await page.get_by_placeholder("Property name").nth(index).fill(name)
                        await page.locator(".ic-row select").nth(index).select_option(kind)
                    await page.get_by_role("button", name="Create Now", exact=True).click()
                    await expect(page.locator(".ic-image-block")).to_be_visible()
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (0/0)")
                    await expect(page.get_by_role("button", name="Import Catalog", exact=True)).to_have_count(0)
                    assert await page.locator(".ic-catalog-actions button").all_text_contents() == ["Edit Catalog", "Export Catalog", "Reload Catalog", "Delete Catalog"]
                    assert await page.locator(".ic-image-actions").evaluate("actions => actions.previousElementSibling.tagName") == "HR"
                    assert await page.locator(".ic-delete-actions").evaluate("row => getComputedStyle(row).justifyContent") == "flex-end"
                    png = BytesIO()
                    Image.new("RGB", (320, 200), (55, 110, 190)).save(png, format="PNG")
                    async def add_file(name, caption, seed):
                        await page.get_by_label("Add image", exact=True).set_input_files({"name": name, "mimeType": "image/png", "buffer": png.getvalue()})
                        await page.get_by_label("caption", exact=True).fill(caption)
                        await page.get_by_label("seed", exact=True).fill(str(seed))
                    await add_file("first.png", "First line\nSecond line", 123)
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (1/1)")
                    await expect(page.get_by_role("button", name="Previous image", exact=True)).to_have_count(0)
                    await expect(page.get_by_role("button", name="Next image", exact=True)).to_have_count(0)
                    await page.get_by_label("enabled", exact=True).check()
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await page.wait_for_function("window.lastOutputs?.[1] === 'First line\\nSecond line'")
                    assert await page.evaluate("window.lastOutputs.slice(0, 4)") == [[1, 200, 320, 3], "First line\nSecond line", 123, True]
                    catalog_id = backend.STORE.list()["catalogs"][0]["id"]
                    path = backend.STORE.root / catalog_id / "catalog.json"
                    original_json = path.read_bytes()
                    assert backend.STORE.get(catalog_id)["entries"] == []
                    await page.get_by_role("button", name="Add Image", exact=True).click()
                    await add_file("second.png", "Second image", 222)
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (2/2)")
                    await expect(page.get_by_role("button", name="Next image", exact=True)).to_have_count(0)
                    await page.get_by_role("button", name="Previous image", exact=True).click()
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (1/2)")
                    await expect(page.get_by_label("caption", exact=True)).to_have_value("First line\nSecond line")
                    await expect(page.get_by_role("button", name="Previous image", exact=True)).to_have_count(0)
                    await page.get_by_role("button", name="Next image", exact=True).click()
                    await expect(page.get_by_label("caption", exact=True)).to_have_value("Second image")
                    await page.get_by_role("button", name="Add Image", exact=True).click()
                    await add_file("discard.png", "Discard", 0)
                    await page.get_by_role("button", name="Delete", exact=True).click()
                    assert await page.evaluate("window.catalogNode.imageCatalog.state.newImages.length") == 2
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (1/2)")
                    await page.get_by_role("button", name="Add Image", exact=True).click()
                    await add_file("third.png", "Third image", 333)
                    await page.get_by_label("Index after queue", exact=True).select_option("increment")
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await page.wait_for_function("window.lastOutputs[1] === 'Third image'")
                    assert path.read_bytes() == original_json
                    assert await page.evaluate("window.catalogNode.widgets[0].value") == 0
                    await page.get_by_role("button", name="Save Changes", exact=True).click()
                    await expect(page.locator(".ic-status")).to_have_text("All catalog changes saved.")
                    entries = backend.STORE.get(catalog_id)["entries"]
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (3/3)")
                    await page.get_by_role("button", name="Previous image", exact=True).click()
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (2/3)")
                    for name in ("Previous image", "Next image"):
                        arrow = page.get_by_role("button", name=name, exact=True)
                        await expect(arrow).to_be_visible()
                        bounds = await arrow.bounding_box()
                        preview = await page.locator("img.ic-preview").bounding_box()
                        assert abs(bounds["y"] + bounds["height"] / 2 - preview["y"] - preview["height"] / 2) < 1
                    assert await page.evaluate("window.catalogNode.widgets[0].value") == 1
                    await page.get_by_role("button", name="Next image", exact=True).click()
                    await expect(page.get_by_role("button", name="Next image", exact=True)).to_have_count(0)
                    assert [entry["values"]["seed"] for entry in entries] == [123, 222, 333]
                    assert await page.evaluate("window.catalogNode.imageCatalog.state.newImages.length") == 0
                    await page.get_by_label("Image", exact=True).select_option(entries[0]["id"])
                    await page.get_by_label("caption", exact=True).fill("Workflow-only caption")
                    await page.get_by_role("button", name="Restore workflow", exact=True).click()
                    await expect(page.get_by_label("caption", exact=True)).to_have_value("Workflow-only caption")
                    await expect(page.get_by_label("Index after queue", exact=True)).to_have_value("increment")
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await page.wait_for_function("window.lastOutputs[1] === 'Workflow-only caption'")
                    assert backend.STORE.get(catalog_id)["entries"][0]["values"]["caption"] == "First line\nSecond line"
                    await page.get_by_label("Image", exact=True).select_option(entries[0]["id"])
                    await page.get_by_label("Hide", exact=True).check()
                    await expect(page.get_by_label("caption", exact=True)).to_be_disabled()
                    await page.get_by_label("image_index", exact=True).fill("0")
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await page.wait_for_function("window.lastOutputs[1] === 'Second image'")
                    assert not backend.STORE.get(catalog_id)["entries"][0]["hidden"]
                    await page.get_by_label("Image", exact=True).select_option(entries[1]["id"])
                    await page.get_by_role("button", name="Delete", exact=True).click()
                    assert (path.parent / entries[1]["file"]).exists()
                    assert len(backend.STORE.get(catalog_id)["entries"]) == 3
                    assert await page.evaluate("window.catalogNode.imageCatalog.workingCatalog().entries.length") == 2
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (2/2)")
                    await page.get_by_role("button", name="Export Catalog", exact=True).click()
                    await expect(page.get_by_role("dialog")).to_be_visible()
                    await page.get_by_role("dialog").get_by_role("button", name="Cancel", exact=True).click()
                    await page.get_by_role("button", name="Export Catalog", exact=True).click()
                    async with page.expect_download() as downloaded:
                        await page.get_by_role("button", name="Export Saved Version", exact=True).click()
                    download = await downloaded.value
                    import zipfile
                    with zipfile.ZipFile(await download.path()) as archive:
                        assert archive.read("catalog.json") == path.read_bytes()
                        assert len(archive.namelist()) == 4
                    assert await page.evaluate("window.catalogNode.imageCatalog.hasUnsavedChanges()")
                    await page.get_by_role("button", name="Export Catalog", exact=True).click()
                    async with page.expect_download() as downloaded:
                        await page.get_by_role("button", name="Save and Export", exact=True).click()
                    download = await downloaded.value
                    with zipfile.ZipFile(await download.path()) as archive:
                        assert archive.read("catalog.json") == path.read_bytes()
                        assert len(archive.namelist()) == 3
                    assert not (path.parent / entries[1]["file"]).exists()
                    assert backend.STORE.get(catalog_id)["entries"][0]["hidden"]
                    assert not await page.evaluate("window.catalogNode.imageCatalog.hasUnsavedChanges()")
                    await page.get_by_role("button", name="Reload Catalog", exact=True).click()
                    await expect(page.locator(".ic-image-selector > span")).to_have_text("Image (2/2)")
                    await page.evaluate("window.catalogNode.outputs[3].links = [77]")
                    await page.get_by_role("button", name="Edit Catalog", exact=True).click()
                    await expect(page.locator(".ic-image-block")).to_be_visible()
                    await expect(page.locator(".ic-catalog-block .ic-schema-field")).to_have_count(3)
                    await page.locator(".ic-schema-field").nth(0).get_by_label("Hidden", exact=True).check()
                    await page.locator(".ic-schema-field").nth(1).get_by_role("button", name="Remove property", exact=True).click()
                    await page.get_by_role("button", name="Add property", exact=True).click()
                    await page.get_by_label("Property 3 name", exact=True).fill("rating")
                    await page.get_by_label("Property 3 type", exact=True).select_option("Integer")
                    await page.get_by_role("button", name="Apply Fields", exact=True).click()
                    await expect(page.get_by_label("rating", exact=True)).to_have_value("0")
                    await expect(page.get_by_label("caption", exact=True)).to_have_count(0)
                    assert await page.evaluate("window.catalogNode.outputs.map(output => output.name)") == ["IMAGE", "caption", "enabled", "rating"]
                    assert await page.evaluate("window.catalogNode.outputs[2].links") == [77]
                    assert "seed" in backend.STORE.get(catalog_id)["entries"][0]["values"]
                    await page.get_by_role("button", name="Restore workflow", exact=True).click()
                    await expect(page.get_by_label("rating", exact=True)).to_have_value("0")
                    await page.get_by_role("button", name="Queue workflow", exact=True).click()
                    await page.wait_for_function("window.lastOutputs[3] === 0")
                    assert "seed" in backend.STORE.get(catalog_id)["entries"][0]["values"]
                    await page.get_by_role("button", name="Save Changes", exact=True).click()
                    await expect(page.locator(".ic-status")).to_have_text("All catalog changes saved.")
                    assert all("seed" not in entry["values"] and entry["values"]["rating"] == 0 for entry in backend.STORE.get(catalog_id)["entries"])
                    async with page.expect_download() as downloaded:
                        await page.get_by_role("button", name="Export Catalog", exact=True).click()
                    archive_path = await (await downloaded.value).path()
                    await page.get_by_label("Catalog", exact=True).select_option("")
                    await expect(page.locator(".ic-image-block")).to_have_count(0)
                    await page.get_by_label("Import catalog ZIP", exact=True).set_input_files(archive_path)
                    await expect(page.locator(".ic-status")).to_have_text("Catalog imported.")
                    assert len(backend.STORE.list()["catalogs"]) == 2
                    imported_id = await page.evaluate("window.catalogNode.imageCatalog.state.catalogId")
                    assert imported_id != catalog_id
                    screenshots = ROOT / "test-results"
                    screenshots.mkdir(exist_ok=True)
                    await page.screenshot(path=str(screenshots / "catalog-browser-smoke.png"), full_page=True)
                    await page.get_by_role("button", name="Delete Catalog", exact=True).click()
                    await expect(page.get_by_role("button", name="Create Now", exact=True)).to_be_visible()
                    await page.get_by_label("Catalog", exact=True).select_option(catalog_id)
                    await page.get_by_role("button", name="Delete Catalog", exact=True).click()
                    await expect(page.get_by_role("button", name="Create Now", exact=True)).to_be_visible()
                    assert backend.STORE.list()["catalogs"] == []
                    assert errors == [], errors
                    print("Browser smoke passed: separate blocks, local workflow state, explicit save/delete, draft uploads, schema editing, export choices, ZIP import.")
                finally:
                    await browser.close()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
