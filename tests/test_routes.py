from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import warnings
import zipfile

try:
    from aiohttp import FormData, web
    from aiohttp.test_utils import TestClient, TestServer
    from PIL import Image
    import numpy
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

from support import load_backend


@unittest.skipUnless(AVAILABLE, "Install Pillow, numpy and aiohttp for HTTP integration tests.")
class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.backend, self.substitutes = load_backend(Path(self.temporary.name))
        application = web.Application()
        application.add_routes(self.backend.routes)
        self.client = TestClient(TestServer(application))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.prefix = self.backend.API_PREFIX

    async def create(self):
        response = await self.client.post(self.prefix + "/catalogs", json={"name": "Test", "schema": [{"name": "caption", "type": "Text"}]})
        self.assertEqual(response.status, 201)
        return await response.json()

    async def test_upload_execute_preview_and_confirmed_delete(self):
        catalog = await self.create()
        token = uuid.uuid4().hex
        png = BytesIO()
        Image.new("RGBA", (7, 5), (255, 0, 0, 128)).save(png, format="PNG")
        form = FormData()
        form.add_field("image", png.getvalue(), filename="example.png", content_type="image/png")
        response = await self.client.post(f"{self.prefix}/catalogs/{catalog['id']}/uploads/{token}", data=form)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.backend.STORE.get(catalog["id"])["entries"], [])
        state = {"catalog_id": catalog["id"], "schema": catalog["schema"], "operation_id": uuid.uuid4().hex,
                 "new_image": {"token": token, "values": {"caption": "A\nB"}}}
        result = self.backend.ArtemKo7vImageCatalogIndexed().execute(0, json.dumps(state))
        self.assertEqual(result["result"][0].shape, (1, 5, 7, 3))
        self.assertEqual(result["result"][1], "A\nB")
        self.assertEqual(len(result["result"]), 33)
        self.assertAlmostEqual(float(result["result"][0][0, 0, 0, 0]), 1.0)
        self.assertEqual(self.backend.STORE.get(catalog["id"])["entries"], [])
        local = {**catalog, "entries": [{"id": token, "file": token + ".png", "filename": "example.png",
                                        "values": {"caption": "A\nB"}, "hidden": False, "revision": 1}]}
        saved = await self.client.post(f"{self.prefix}/catalogs/{catalog['id']}/save", json={
            "catalog": local, "expected_revision": 0, "operation_id": uuid.uuid4().hex})
        self.assertEqual(saved.status, 200, await saved.text())
        preview = await self.client.get(f"{self.prefix}/catalogs/{catalog['id']}/images/{token}")
        self.assertEqual(preview.status, 200)
        self.assertEqual(Image.open(BytesIO(await preview.read())).size, (7, 5))
        refused = await self.client.delete(f"{self.prefix}/catalogs/{catalog['id']}", json={})
        self.assertEqual(refused.status, 400)
        deleted = await self.client.delete(f"{self.prefix}/catalogs/{catalog['id']}", json={"confirm": catalog["id"]})
        self.assertEqual(deleted.status, 200)
        self.assertEqual(self.backend.STORE.list()["catalogs"], [])

    async def test_invalid_image_never_creates_a_record(self):
        catalog = await self.create()
        form = FormData()
        form.add_field("image", b"not an image", filename="bad.png", content_type="image/png")
        response = await self.client.post(f"{self.prefix}/catalogs/{catalog['id']}/uploads/{uuid.uuid4().hex}", data=form)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.backend.STORE.get(catalog["id"])["entries"], [])

    async def test_empty_catalog_blocks_downstream_without_error(self):
        catalog = await self.create()
        state = {"catalog_id": catalog["id"], "schema": catalog["schema"], "operation_id": uuid.uuid4().hex}
        with patch.dict("sys.modules", self.substitutes):
            result = self.backend.ArtemKo7vImageCatalogIndexed().execute(0, json.dumps(state))
        self.assertIsNone(result["ui"]["catalog_result"][0]["selected_id"])
        self.assertEqual(len(result["result"]), 33)
        self.assertIsInstance(result["result"][0], self.substitutes["comfy_execution.graph"].ExecutionBlocker)

    async def populate(self):
        catalog = await self.create()
        png = BytesIO()
        Image.new("RGB", (8, 6), "blue").save(png, format="PNG")
        token = uuid.uuid4().hex
        self.backend.STORE.stage(catalog["id"], token, png.getvalue(), "example.png")
        state = {"catalog_id": catalog["id"], "schema": catalog["schema"], "operation_id": uuid.uuid4().hex,
                 "new_images": [{"token": token, "values": {"caption": "Preserved text"}}]}
        catalog["entries"] = [{"id": token, "file": token + ".png", "filename": "example.png",
                               "values": {"caption": "Preserved text"}, "hidden": False, "revision": 1}]
        saved = await self.client.post(f"{self.prefix}/catalogs/{catalog['id']}/save", json={
            "catalog": catalog, "expected_revision": 0, "operation_id": uuid.uuid4().hex})
        self.assertEqual(saved.status, 200, await saved.text())
        return catalog, token

    async def import_bytes(self, content):
        form = FormData()
        form.add_field("archive", content, filename="catalog.zip", content_type="application/zip")
        return await self.client.post(self.prefix + "/import", data=form)

    async def test_replaced_image_preview_and_archive_roundtrip(self):
        catalog, entry_id = await self.populate()
        local = self.backend.STORE.get(catalog["id"])
        token = uuid.uuid4().hex
        png = BytesIO()
        Image.new("RGB", (12, 9), "green").save(png, format="PNG")
        form = FormData()
        form.add_field("image", png.getvalue(), filename="replacement.png", content_type="image/png")
        uploaded = await self.client.post(f"{self.prefix}/catalogs/{catalog['id']}/uploads/{token}", data=form)
        self.assertEqual(uploaded.status, 200)
        local["entries"][0].update(file=token + ".png", filename="replacement.png")
        saved = await self.client.post(f"{self.prefix}/catalogs/{catalog['id']}/save", json={
            "catalog": local, "expected_revision": local["revision"], "operation_id": uuid.uuid4().hex})
        self.assertEqual(saved.status, 200, await saved.text())
        preview = await self.client.get(f"{self.prefix}/catalogs/{catalog['id']}/images/{entry_id}")
        self.assertEqual(Image.open(BytesIO(await preview.read())).size, (12, 9))
        exported = await self.client.get(f"{self.prefix}/catalogs/{catalog['id']}/export")
        content = await exported.read()
        with zipfile.ZipFile(BytesIO(content)) as archive:
            self.assertEqual(set(archive.namelist()), {"catalog.json", token + ".png"})
        imported = await self.import_bytes(content)
        self.assertEqual(imported.status, 201, await imported.text())
        clone = await imported.json()
        self.assertEqual(clone["entries"][0]["values"], {"caption": "Preserved text"})
        with Image.open(self.backend.STORE.image_path(clone["id"], clone["entries"][0]["id"])) as image:
            self.assertEqual(image.size, (12, 9))

    async def test_edit_schema_export_and_import_roundtrip(self):
        catalog, token = await self.populate()
        schema = [{"name": "caption", "type": "Text", "hidden": True}, {"name": "score", "type": "Integer"}]
        response = await self.client.patch(f"{self.prefix}/catalogs/{catalog['id']}/schema",
                                           json={"schema": schema, "expected_revision": 0})
        self.assertEqual(response.status, 200)
        updated = await response.json()
        self.assertEqual(updated["entries"][0]["values"], {"caption": "Preserved text", "score": 0})
        exported = await self.client.get(f"{self.prefix}/catalogs/{catalog['id']}/export")
        self.assertEqual(exported.status, 200)
        self.assertEqual(exported.content_type, "application/zip")
        content = await exported.read()
        with zipfile.ZipFile(BytesIO(content)) as archive:
            self.assertEqual(set(archive.namelist()), {"catalog.json", token + ".png"})
            self.assertEqual(archive.read("catalog.json"), (self.backend.STORE.root / catalog["id"] / "catalog.json").read_bytes())
        imported = await self.import_bytes(content)
        self.assertEqual(imported.status, 201, await imported.text())
        clone = await imported.json()
        self.assertNotEqual(clone["id"], catalog["id"])
        self.assertEqual(clone["name"], "Test (imported)")
        self.assertEqual(clone["schema"], schema)
        self.assertEqual(clone["entries"][0]["values"], updated["entries"][0]["values"])
        self.assertNotEqual(clone["entries"][0]["id"], token)
        self.assertEqual(self.backend.STORE.image_path(clone["id"], clone["entries"][0]["id"]).read_bytes(),
                         self.backend.STORE.image_path(catalog["id"], token).read_bytes())
        repeated = await self.import_bytes(content)
        self.assertEqual((await repeated.json())["name"], "Test (imported 2)")
        stale = await self.client.patch(f"{self.prefix}/catalogs/{catalog['id']}/schema",
                                        json={"schema": [], "expected_revision": 0})
        self.assertEqual(stale.status, 409)

    async def test_import_rejects_invalid_archives_without_partial_catalog(self):
        catalog, token = await self.populate()
        data = self.backend.STORE.get(catalog["id"])
        valid_png = self.backend.STORE.image_path(catalog["id"], token).read_bytes()
        for bad_member in ("../escape.png", "/absolute.png", "folder/image.png", "C:\\image.png"):
            stream = BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("catalog.json", json.dumps(data))
                archive.writestr(bad_member, valid_png)
            response = await self.import_bytes(stream.getvalue())
            self.assertEqual(response.status, 400)
        for image_bytes in (None, b"invalid PNG"):
            stream = BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("catalog.json", json.dumps(data))
                if image_bytes is not None:
                    archive.writestr(token + ".png", image_bytes)
            response = await self.import_bytes(stream.getvalue())
            self.assertEqual(response.status, 400)
        response = await self.import_bytes(b"not a zip")
        self.assertEqual(response.status, 400)
        self.assertEqual(len(self.backend.STORE.list()["catalogs"]), 1)
        self.assertEqual(list(self.backend.STORE.root.glob(".import-*")), [])

    async def test_empty_catalog_archive_roundtrip(self):
        catalog = await self.create()
        exported = await self.client.get(f"{self.prefix}/catalogs/{catalog['id']}/export")
        imported = await self.import_bytes(await exported.read())
        self.assertEqual(imported.status, 201)
        self.assertEqual((await imported.json())["entries"], [])

    async def test_archive_duplicate_links_and_size_limits(self):
        catalog, token = await self.populate()
        data = self.backend.STORE.get(catalog["id"])
        png = self.backend.STORE.image_path(catalog["id"], token).read_bytes()
        for mode in ("duplicate", "symlink", "valid"):
            stream = BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("catalog.json", json.dumps(data))
                member = zipfile.ZipInfo(token + ".png")
                if mode == "symlink":
                    member.create_system = 3
                    member.external_attr = 0o120777 << 16
                archive.writestr(member, png)
                if mode == "duplicate":
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        archive.writestr("catalog.json", json.dumps(data))
            if mode == "valid":
                with patch.dict(self.backend.import_catalog.__globals__, {"MAX_UNPACKED_BYTES": 1}):
                    response = await self.import_bytes(stream.getvalue())
                self.assertEqual(response.status, 400)
                with patch.object(self.backend, "MAX_ARCHIVE_BYTES", 1):
                    response = await self.import_bytes(stream.getvalue())
            else:
                response = await self.import_bytes(stream.getvalue())
            self.assertEqual(response.status, 400)
        self.assertEqual(len(self.backend.STORE.list()["catalogs"]), 1)


if __name__ == "__main__":
    unittest.main()
