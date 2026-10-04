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

    async def import_bytes(self, content, catalog_id=None, revision=None, field_policy=None):
        form = FormData()
        form.add_field("archive", content, filename="catalog.zip", content_type="application/zip")
        path = "/import"
        if catalog_id is not None:
            if revision is None:
                revision = self.backend.STORE.get(catalog_id)["revision"]
            path = f"/catalogs/{catalog_id}/import?expected_revision={revision}"
        if field_policy is not None:
            path += f"&field_policy={field_policy}"
        return await self.client.post(self.prefix + path, data=form)

    def archive_bytes(self, store, catalog_id):
        stream, _ = self.backend.export_catalog(store, catalog_id)
        try:
            return stream.read()
        finally:
            stream.close()

    def add_record(self, store, catalog_id, caption, hidden=False):
        data = store.get(catalog_id)
        token = uuid.uuid4().hex
        png = BytesIO()
        Image.new("RGB", (11, 9), "red").save(png, format="PNG")
        store.stage(catalog_id, token, png.getvalue(), "same-name.png")
        data["entries"].append({"id": token, "file": token + ".png", "filename": "same-name.png",
                                "values": {"caption": caption}, "hidden": hidden, "revision": 1})
        return store.save_client(catalog_id, data, data["revision"], uuid.uuid4().hex)["catalog"]

    async def test_import_additions_between_instances_is_bidirectional_and_idempotent(self):
        original, token = await self.populate()
        store = self.backend.STORE
        remote = type(store)(Path(self.temporary.name) / "second-instance")
        clone = self.backend.import_catalog(remote, BytesIO(self.archive_bytes(store, original["id"])),
                                            self.backend.validate_imported_image)
        self.assertEqual(clone["sync_id"], original["id"])
        self.assertNotEqual(clone["entries"][0]["id"], token)
        self.assertEqual(clone["entries"][0]["sync_id"], token)
        # Replacing a common image and editing its values/visibility on the remote
        # must not overwrite the local record during addition-only import.
        replacement = uuid.uuid4().hex
        png = BytesIO()
        Image.new("RGB", (2, 3), "green").save(png, format="PNG")
        remote.stage(clone["id"], replacement, png.getvalue(), "replacement.png")
        clone["entries"][0].update(file=replacement + ".png", filename="replacement.png",
                                    values={"caption": "remote edit"}, hidden=True)
        # Old workflow snapshots may omit the newly introduced identity fields.
        clone.pop("sync_id")
        clone["entries"][0].pop("sync_id")
        clone = remote.save_client(clone["id"], clone, clone["revision"], uuid.uuid4().hex)["catalog"]
        self.assertEqual(clone["sync_id"], original["id"])
        self.assertEqual(clone["entries"][0]["sync_id"], token)
        clone = self.add_record(remote, clone["id"], "remote first", hidden=True)
        clone = self.add_record(remote, clone["id"], "remote second")
        before = self.add_record(store, original["id"], "local only")
        old_png = store.image_path(original["id"], token).read_bytes()
        content = self.archive_bytes(remote, clone["id"])
        response = await self.import_bytes(content, original["id"])
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertEqual((result["added"], result["skipped"]), (2, 1))
        merged = result["catalog"]
        self.assertEqual(merged["entries"][:2], before["entries"])
        self.assertEqual([e["values"]["caption"] for e in merged["entries"]],
                         ["Preserved text", "local only", "remote first", "remote second"])
        self.assertTrue(merged["entries"][2]["hidden"])
        self.assertEqual(merged["revision"], before["revision"] + 1)
        self.assertEqual(merged["schema_revision"], before["schema_revision"])
        self.assertEqual(store.image_path(original["id"], token).read_bytes(), old_png)
        self.assertEqual(store.image_path(original["id"], merged["entries"][2]["id"]).read_bytes(),
                         remote.image_path(clone["id"], clone["entries"][1]["id"]).read_bytes())
        path = store.root / original["id"] / "catalog.json"
        saved = path.read_bytes()
        repeated = await self.import_bytes(content, original["id"])
        self.assertEqual(repeated.status, 200)
        replay = await repeated.json()
        self.assertEqual((replay["added"], replay["skipped"]), (0, 3))
        self.assertEqual(path.read_bytes(), saved)
        back = self.backend.import_catalog(remote, BytesIO(self.archive_bytes(store, original["id"])),
                                           self.backend.validate_imported_image, clone["id"], clone["revision"])
        self.assertEqual((back["added"], back["skipped"]), (1, 3))
        self.assertEqual(back["catalog"]["entries"][:3], clone["entries"])
        stale_save = await self.client.post(f"{self.prefix}/catalogs/{original['id']}/save", json={
            "catalog": before, "expected_revision": before["revision"], "operation_id": uuid.uuid4().hex})
        self.assertEqual(stale_save.status, 409)

    async def test_import_additions_retains_records_deleted_remotely(self):
        original, token = await self.populate()
        clone = await (await self.import_bytes(self.archive_bytes(self.backend.STORE, original["id"]))).json()
        clone["entries"] = []
        clone = self.backend.STORE.save_client(clone["id"], clone, clone["revision"], uuid.uuid4().hex)["catalog"]
        clone = self.add_record(self.backend.STORE, clone["id"], "added after deletion")
        response = await self.import_bytes(self.archive_bytes(self.backend.STORE, clone["id"]), original["id"])
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["catalog"]["entries"][0]["id"], token)
        self.assertTrue(self.backend.STORE.image_path(original["id"], token).is_file())

    async def test_import_additions_failure_rolls_back_and_retry_succeeds(self):
        original, _ = await self.populate()
        store = self.backend.STORE
        clone = await (await self.import_bytes(self.archive_bytes(store, original["id"]))).json()
        self.add_record(store, clone["id"], "first")
        self.add_record(store, clone["id"], "second")
        content = self.archive_bytes(store, clone["id"])
        directory = store.root / original["id"]
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with patch.object(store, "_write", side_effect=OSError("Disk full")):
            with self.assertLogs(level="ERROR"):
                response = await self.import_bytes(content, original["id"])
        self.assertEqual(response.status, 500)
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)
        self.assertEqual(list(store.root.glob(".import-*")), [])
        response = await self.import_bytes(content, original["id"])
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["added"], 2)

    async def test_import_additions_rejects_stale_unrelated_and_incompatible_archives(self):
        original, _ = await self.populate()
        store = self.backend.STORE
        before = store.get(original["id"])
        content = self.archive_bytes(store, original["id"])
        response = await self.import_bytes(content, original["id"], revision=0)
        self.assertEqual(response.status, 409)
        response = await self.import_bytes(content, original["id"], revision="bad")
        self.assertEqual(response.status, 400)
        other = store.create("Unrelated", original["schema"])
        response = await self.import_bytes(self.archive_bytes(store, other["id"]), original["id"])
        self.assertEqual(response.status, 400)
        self.assertIn("different catalog", (await response.json())["error"])
        clone = await (await self.import_bytes(content)).json()
        store.update_schema(clone["id"], [], clone["schema_revision"])
        store.update_schema(clone["id"], [{"name": "caption", "type": "String"}], store.get(clone["id"])["schema_revision"])
        for policy in (None, "ignore", "extend"):
            response = await self.import_bytes(self.archive_bytes(store, clone["id"]), original["id"], field_policy=policy)
            self.assertEqual(response.status, 400)
            self.assertIn("fields differ", (await response.json())["error"])
        self.assertEqual(store.get(original["id"]), before)

    async def test_import_additions_checks_all_images_and_sync_identifiers(self):
        original, _ = await self.populate()
        store = self.backend.STORE
        clone = await (await self.import_bytes(self.archive_bytes(store, original["id"]))).json()
        clone = self.add_record(store, clone["id"], "new")
        before = store.get(original["id"])
        directory = store.root / original["id"]
        before_files = set(p.name for p in directory.iterdir())
        for mode in ("bad existing image", "bad new image", "duplicate sync id", "invalid sync id"):
            data = json.loads(json.dumps(clone))
            if mode == "duplicate sync id":
                data["entries"][1]["sync_id"] = data["entries"][0]["sync_id"]
            elif mode == "invalid sync id":
                data["sync_id"] = "../invalid"
            stream = BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("catalog.json", json.dumps(data))
                for index, entry in enumerate(data["entries"]):
                    bad = mode == ("bad existing image" if index == 0 else "bad new image")
                    archive.writestr(entry["file"], b"invalid" if bad else store.image_path(clone["id"], entry["id"]).read_bytes())
            response = await self.import_bytes(stream.getvalue(), original["id"])
            self.assertEqual(response.status, 400, mode)
            self.assertEqual(store.get(original["id"]), before)
            self.assertEqual(set(p.name for p in directory.iterdir()), before_files)
        self.assertEqual(list(store.root.glob(".import-*")), [])

    async def archive_with_new_fields(self, add_image=True):
        original, _ = await self.populate()
        store = self.backend.STORE
        clone = await (await self.import_bytes(self.archive_bytes(store, original["id"]))).json()
        schema = [{"name": "score", "type": "Integer", "hidden": True},
                  {"name": "caption", "type": "Text"},
                  {"name": "enabled", "type": "Boolean"},
                  {"name": "label", "type": "String"}]
        store.update_schema(clone["id"], schema, clone["schema_revision"])
        if add_image:
            self.add_record(store, clone["id"], "new caption")
        clone = store.get(clone["id"])
        for entry in clone["entries"]:
            entry["values"].update(score=42, enabled=True, label="remote value")
        clone = store.save_client(clone["id"], clone, clone["revision"], uuid.uuid4().hex)["catalog"]
        return original["id"], self.archive_bytes(store, clone["id"])

    async def test_new_archive_fields_require_choice_without_modifying_catalog(self):
        catalog_id, content = await self.archive_with_new_fields()
        store = self.backend.STORE
        directory = store.root / catalog_id
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        response = await self.import_bytes(content, catalog_id)
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertTrue(result["needs_field_choice"])
        self.assertEqual([field["name"] for field in result["new_fields"]], ["score", "enabled", "label"])
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)
        self.assertEqual(list(store.root.glob(".import-*")), [])
        response = await self.import_bytes(content, catalog_id, field_policy="invalid")
        self.assertEqual(response.status, 400)
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)

    async def test_import_ignores_new_archive_fields_when_requested(self):
        catalog_id, content = await self.archive_with_new_fields()
        store = self.backend.STORE
        before = store.get(catalog_id)
        response = await self.import_bytes(content, catalog_id, field_policy="ignore")
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertEqual(result["fields_added"], 0)
        self.assertEqual(result["catalog"]["schema"], before["schema"])
        self.assertEqual(result["catalog"]["schema_revision"], before["schema_revision"])
        self.assertEqual(result["catalog"]["entries"][0], before["entries"][0])
        self.assertEqual(result["catalog"]["entries"][1]["values"], {"caption": "new caption"})

    async def test_import_extends_schema_and_fills_existing_images_with_defaults(self):
        catalog_id, content = await self.archive_with_new_fields()
        store = self.backend.STORE
        before = store.get(catalog_id)
        response = await self.import_bytes(content, catalog_id, field_policy="extend")
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        after = result["catalog"]
        self.assertEqual((result["added"], result["skipped"], result["fields_added"]), (1, 1, 3))
        self.assertEqual([f["name"] for f in after["schema"]], ["caption", "score", "enabled", "label"])
        self.assertTrue(after["schema"][1]["hidden"])
        self.assertEqual(after["schema_revision"], before["schema_revision"] + 1)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["entries"][0]["revision"], before["entries"][0]["revision"] + 1)
        self.assertEqual(after["entries"][0]["values"], {"caption": "Preserved text", "score": 0, "enabled": False, "label": ""})
        self.assertEqual(after["entries"][1]["values"], {"caption": "new caption", "score": 42, "enabled": True, "label": "remote value"})
        repeated = await self.import_bytes(content, catalog_id)
        self.assertEqual(repeated.status, 200, await repeated.text())
        self.assertEqual((await repeated.json())["catalog"], after)

    async def test_import_schema_extension_without_new_images_and_stale_choice(self):
        catalog_id, content = await self.archive_with_new_fields(add_image=False)
        store = self.backend.STORE
        before = store.get(catalog_id)
        await self.import_bytes(content, catalog_id)
        self.add_record(store, catalog_id, "local addition")
        response = await self.import_bytes(content, catalog_id, revision=before["revision"], field_policy="extend")
        self.assertEqual(response.status, 409)
        self.assertEqual(store.get(catalog_id)["schema"], before["schema"])
        response = await self.import_bytes(content, catalog_id, field_policy="extend")
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertEqual((result["added"], result["fields_added"]), (0, 3))
        self.assertEqual(len(result["catalog"]["entries"]), 2)
        self.assertTrue(all(e["values"]["score"] == 0 for e in result["catalog"]["entries"]))

    async def test_schema_extension_failure_rolls_back_images_and_metadata(self):
        catalog_id, content = await self.archive_with_new_fields()
        store = self.backend.STORE
        directory = store.root / catalog_id
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with patch.object(store, "_write", side_effect=OSError("Disk full")):
            with self.assertLogs(level="ERROR"):
                response = await self.import_bytes(content, catalog_id, field_policy="extend")
        self.assertEqual(response.status, 500)
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)

    async def test_missing_archive_fields_require_choice_and_default_new_images(self):
        original, _ = await self.populate()
        store = self.backend.STORE
        clone = await (await self.import_bytes(self.archive_bytes(store, original["id"]))).json()
        clone = self.add_record(store, clone["id"], "new caption")
        schema = original["schema"] + [{"name": "score", "type": "Integer", "hidden": True},
                                       {"name": "enabled", "type": "Boolean"},
                                       {"name": "label", "type": "String"},
                                       {"name": "notes", "type": "Text"}]
        before = store.update_schema(original["id"], schema, store.get(original["id"])["schema_revision"])
        before["entries"][0]["values"].update(score=88, enabled=True, label="local label", notes="local text")
        before = store.save_client(original["id"], before, before["revision"], uuid.uuid4().hex)["catalog"]
        content = self.archive_bytes(store, clone["id"])
        directory = store.root / original["id"]
        files_before = {p.name: p.read_bytes() for p in directory.iterdir()}
        response = await self.import_bytes(content, original["id"])
        self.assertEqual(response.status, 200, await response.text())
        choice = await response.json()
        self.assertTrue(choice["needs_field_choice"])
        self.assertEqual(choice["new_fields"], [])
        self.assertEqual([f["name"] for f in choice["missing_fields"]], ["score", "enabled", "label", "notes"])
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, files_before)
        response = await self.import_bytes(content, original["id"], field_policy="ignore")
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        after = result["catalog"]
        self.assertEqual((result["added"], result["skipped"], result["fields_added"]), (1, 1, 0))
        self.assertEqual(after["schema"], before["schema"])
        self.assertEqual(after["schema_revision"], before["schema_revision"])
        self.assertEqual(after["entries"][0], before["entries"][0])
        self.assertEqual(after["entries"][1]["values"], {"caption": "new caption", "score": 0, "enabled": False, "label": "", "notes": ""})
        repeated = await self.import_bytes(content, original["id"], field_policy="ignore")
        self.assertEqual((await repeated.json())["catalog"], after)

    async def test_missing_and_new_archive_fields_can_be_imported_together(self):
        catalog_id, content = await self.archive_with_new_fields()
        store = self.backend.STORE
        before = store.get(catalog_id)
        before = store.update_schema(catalog_id, before["schema"] + [{"name": "local_only", "type": "Integer"}], before["schema_revision"])
        response = await self.import_bytes(content, catalog_id)
        self.assertEqual(response.status, 200, await response.text())
        choice = await response.json()
        self.assertEqual([f["name"] for f in choice["new_fields"]], ["score", "enabled", "label"])
        self.assertEqual([f["name"] for f in choice["missing_fields"]], ["local_only"])
        self.assertEqual(store.get(catalog_id), before)
        response = await self.import_bytes(content, catalog_id, field_policy="extend")
        self.assertEqual(response.status, 200, await response.text())
        after = (await response.json())["catalog"]
        self.assertEqual([f["name"] for f in after["schema"]], ["caption", "local_only", "score", "enabled", "label"])
        self.assertEqual(after["entries"][1]["values"], {"caption": "new caption", "local_only": 0, "score": 42, "enabled": True, "label": "remote value"})

    async def test_mixed_field_extension_limit_can_be_resolved_by_ignoring_new_fields(self):
        catalog_id, content = await self.archive_with_new_fields()
        store = self.backend.STORE
        before = store.get(catalog_id)
        schema = before["schema"] + [{"name": f"local_{i}", "type": "Integer"} for i in range(29)]
        before = store.update_schema(catalog_id, schema, before["schema_revision"])
        response = await self.import_bytes(content, catalog_id, field_policy="extend")
        self.assertEqual(response.status, 400, await response.text())
        self.assertIn("32 properties", (await response.json())["error"])
        self.assertEqual(store.get(catalog_id), before)
        response = await self.import_bytes(content, catalog_id, field_policy="ignore")
        self.assertEqual(response.status, 200, await response.text())
        after = (await response.json())["catalog"]
        self.assertEqual(after["schema"], before["schema"])
        self.assertEqual(after["entries"][0], before["entries"][0])
        self.assertEqual(after["entries"][1]["values"], {"caption": "new caption", **{f"local_{i}": 0 for i in range(29)}})

    async def test_empty_archive_schema_can_import_with_all_defaults(self):
        original, _ = await self.populate()
        store = self.backend.STORE
        clone = await (await self.import_bytes(self.archive_bytes(store, original["id"]))).json()
        clone = self.add_record(store, clone["id"], "new caption")
        clone = store.update_schema(clone["id"], [], clone["schema_revision"])
        content = self.archive_bytes(store, clone["id"])
        response = await self.import_bytes(content, original["id"])
        self.assertTrue((await response.json())["needs_field_choice"])
        response = await self.import_bytes(content, original["id"], field_policy="ignore")
        self.assertEqual(response.status, 200, await response.text())
        after = (await response.json())["catalog"]
        self.assertEqual(after["entries"][0]["values"], {"caption": "Preserved text"})
        self.assertEqual(after["entries"][1]["values"], {"caption": ""})

    async def test_legacy_archive_uses_original_ids_for_synchronization(self):
        original, _ = await self.populate()
        store = self.backend.STORE
        legacy = store.get(original["id"])
        legacy.pop("sync_id")
        for entry in legacy["entries"]:
            entry.pop("sync_id")
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("catalog.json", json.dumps(legacy))
            for entry in legacy["entries"]:
                archive.writestr(entry["file"], store.image_path(original["id"], entry["id"]).read_bytes())
        clone = await (await self.import_bytes(stream.getvalue())).json()
        self.add_record(store, clone["id"], "new")
        response = await self.import_bytes(self.archive_bytes(store, clone["id"]), original["id"])
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["added"], 1)

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
