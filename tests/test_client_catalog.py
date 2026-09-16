"""Client snapshots execute read-only and save as complete transactions."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from catalog_store import CatalogConflict, CatalogError, CatalogStore


class ClientCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CatalogStore(Path(temporary.name) / "catalogs")
        self.catalog = self.store.create("Client", [{"name": "caption", "type": "Text"}])
        self.path = self.store.root / self.catalog["id"] / "catalog.json"

    def draft(self, catalog, caption):
        token = uuid.uuid4().hex
        self.store.stage(catalog["id"], token, b"image", caption + ".png")
        entry = {"id": token, "file": token + ".png", "filename": caption + ".png",
                 "values": {"caption": caption}, "hidden": False, "revision": 1}
        catalog["entries"].append(entry)
        return entry

    def save(self, catalog, operation=None):
        return self.store.save_client(catalog["id"], catalog, catalog.get("revision", 0), operation or uuid.uuid4().hex)["catalog"]

    def execute(self, catalog, index=0, **state):
        return self.store.read_client(catalog["id"], index, {"client_catalog": catalog, **state}, lambda path: path.read_bytes())

    def test_multiple_drafts_execute_without_commit_and_save_without_execution(self):
        first = self.draft(self.catalog, "First")
        second = self.draft(self.catalog, "Second")
        before = self.path.read_bytes()
        self.assertEqual(self.execute(self.catalog, 0)["values"], ["First"])
        self.assertEqual(self.execute(self.catalog, 0, new_selection=second["id"])["values"], ["Second"])
        self.assertEqual(self.execute(self.catalog, 0, new_selection=second["id"], index_connected=True)["values"], ["First"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / first["file"]).exists())
        saved = self.save(self.catalog)
        self.assertEqual(len(saved["entries"]), 2)
        self.assertTrue((self.path.parent / first["file"]).exists())
        self.assertEqual(list((self.store.root / ".staging").iterdir()), [])

    def test_local_hides_deletes_schema_and_values_only_commit_on_save(self):
        first = self.draft(self.catalog, "First")
        second = self.draft(self.catalog, "Second")
        self.draft(self.catalog, "Third")
        local = self.save(self.catalog)
        before = self.path.read_bytes()
        local["entries"].pop(0)
        local["entries"][0]["hidden"] = True
        local["schema"] = [{"name": "rating", "type": "Integer"}]
        for entry in local["entries"]:
            entry["values"] = {"rating": 77}
        self.assertEqual(self.execute(local, 999)["values"], [77])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue((self.path.parent / first["file"]).exists())
        saved = self.save(local)
        self.assertEqual(saved["schema_revision"], 1)
        self.assertFalse((self.path.parent / first["file"]).exists())
        self.assertTrue((self.path.parent / second["file"]).exists())
        local = copy.deepcopy(saved)
        local["entries"] = []
        self.assertIsNone(self.execute(local)["image"])
        self.save(local)
        self.assertEqual(list(self.path.parent.glob("*.png")), [])

    def test_save_conflicts_and_retries_are_safe(self):
        self.draft(self.catalog, "First")
        operation = uuid.uuid4().hex
        saved = self.save(self.catalog, operation)
        self.assertEqual(self.save(self.catalog, operation), saved)
        with self.assertRaises(CatalogConflict):
            self.save(self.catalog)
        local = copy.deepcopy(saved)
        local["entries"][0]["values"]["caption"] = "Local"
        remote = copy.deepcopy(saved)
        remote["entries"][0]["values"]["caption"] = "Remote"
        self.save(remote)
        self.assertEqual(self.execute(local)["values"], ["Local"])
        with self.assertRaises(CatalogConflict):
            self.save(local)

    def test_failed_save_rolls_back_files_and_keeps_uploads(self):
        entry = self.draft(self.catalog, "First")
        before = self.path.read_bytes()
        with patch.object(self.store, "_write", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.save(self.catalog)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / entry["file"]).exists())
        self.assertEqual(self.execute(self.catalog)["values"], ["First"])
        self.save(self.catalog)

    def test_cross_catalog_upload_and_invalid_types_are_rejected(self):
        entry = self.draft(self.catalog, "First")
        other = self.store.create("Other", self.catalog["schema"])
        other["entries"] = [entry]
        for action in (self.execute, self.save):
            with self.assertRaises(CatalogError):
                action(other)
        self.catalog["schema"][0]["type"] = "Integer"
        entry["values"]["caption"] = 1
        with self.assertRaises(CatalogError):
            self.save(self.catalog)

    def test_replacement_preserves_record_and_commits_only_on_save(self):
        self.draft(self.catalog, "First")
        self.draft(self.catalog, "Second")
        local = self.save(self.catalog)
        original = copy.deepcopy(local)
        before = self.path.read_bytes()
        entry = local["entries"][0]
        token = uuid.uuid4().hex
        self.store.stage(local["id"], token, b"replacement bytes", "replacement.png")
        entry.update(file=token + ".png", filename="replacement.png")
        result = self.execute(local)
        self.assertEqual(result["image"], b"replacement bytes")
        self.assertEqual(result["values"], ["First"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.execute(original)["image"], b"image")
        operation = uuid.uuid4().hex
        saved = self.save(local, operation)
        self.assertEqual([item["id"] for item in saved["entries"]], [item["id"] for item in original["entries"]])
        self.assertEqual(saved["entries"][0]["revision"], 2)
        self.assertEqual(self.execute(saved)["image"], b"replacement bytes")
        self.assertFalse((self.path.parent / original["entries"][0]["file"]).exists())
        self.assertEqual(self.save(local, operation), saved)
        with self.assertRaises(CatalogConflict):
            self.save(original)

    def test_failed_replacement_keeps_original_file_and_allows_retry(self):
        self.draft(self.catalog, "First")
        local = self.save(self.catalog)
        old_file = local["entries"][0]["file"]
        token = uuid.uuid4().hex
        self.store.stage(local["id"], token, b"replacement", "replacement.png")
        local["entries"][0]["file"] = token + ".png"
        before = self.path.read_bytes()
        with patch.object(self.store, "_write", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.save(local)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue((self.path.parent / old_file).exists())
        self.assertFalse((self.path.parent / (token + ".png")).exists())
        self.assertEqual(self.execute(local)["image"], b"replacement")
        self.save(local)
        self.assertFalse((self.path.parent / old_file).exists())

    def test_replacement_rejects_missing_foreign_and_unsafe_files(self):
        self.draft(self.catalog, "First")
        saved = self.save(self.catalog)
        other = self.store.create("Other", saved["schema"])
        foreign = self.draft(other, "Foreign")
        before = self.path.read_bytes()
        for filename in ("../outside.png", foreign["file"], uuid.uuid4().hex + ".png"):
            local = copy.deepcopy(saved)
            local["entries"][0]["file"] = filename
            for action in (self.execute, self.save):
                with self.assertRaises(CatalogError):
                    action(local)
            self.assertEqual(self.path.read_bytes(), before)
