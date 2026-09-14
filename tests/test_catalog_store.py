from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from catalog_store import CatalogConflict, CatalogError, CatalogStore, validate_schema, validate_values


SCHEMA = [{"name": "title", "type": "String"}, {"name": "prompt", "type": "Text"},
          {"name": "seed", "type": "Integer"}, {"name": "enabled", "type": "Boolean"}]
VALUES = {"title": "First", "prompt": "Line one\nLine two", "seed": 42, "enabled": True}


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = CatalogStore(Path(self.temporary.name) / "catalogs")
        self.catalog = self.store.create("Example", SCHEMA)
        self.catalog_id = self.catalog["id"]

    def state(self, **options):
        return {"schema": SCHEMA, "operation_id": uuid.uuid4().hex, "edits": {}, **options}

    def run_state(self, state, index=0, loader=None):
        return self.store.execute(self.catalog_id, index, state, loader or (lambda path: path.read_bytes()))

    def add_image(self, values=None):
        token = uuid.uuid4().hex
        self.store.stage(self.catalog_id, token, b"test image bytes", "photo.png")
        state = self.state(new_image={"token": token, "values": values or VALUES})
        result = self.run_state(state)
        return result["catalog"]["entries"][-1], state

    def edit(self, entry, **changes):
        return {"values": entry["values"], "hidden": entry["hidden"], "delete": False,
                "revision": entry["revision"], **changes}

    def test_create_and_reload_typed_catalog(self):
        entry, _ = self.add_image()
        reopened = CatalogStore(self.store.root).get(self.catalog_id)
        self.assertEqual(reopened["entries"][0]["values"], VALUES)
        self.assertEqual(self.run_state(self.state())["values"], list(VALUES.values()))
        self.assertTrue(self.store.image_path(self.catalog_id, entry["id"]).exists())
        with self.assertRaises(CatalogConflict):
            self.store.create(" example ", SCHEMA)

    def test_add_without_save_is_idempotent(self):
        entry, state = self.add_image()
        self.run_state(state)
        self.assertEqual(len(self.store.get(self.catalog_id)["entries"]), 1)
        self.assertEqual(entry["revision"], 1)

    def test_unsaved_changes_only_affect_matching_output(self):
        first, _ = self.add_image()
        self.add_image({**VALUES, "title": "Second"})
        edit = self.edit(first, values={**VALUES, "title": "Draft"}, hidden=True, delete=True)
        state = self.state(edits={first["id"]: edit}, save_changes=False)
        self.assertEqual(self.run_state(state)["values"][0], "Draft")
        self.assertEqual(self.run_state(state, 1)["values"][0], "Second")
        self.assertEqual(self.store.get(self.catalog_id)["entries"][0]["values"]["title"], "First")
        self.assertFalse(self.store.get(self.catalog_id)["entries"][0]["hidden"])

    def test_hide_wrap_and_unhide(self):
        first, _ = self.add_image()
        second, _ = self.add_image({**VALUES, "title": "Second"})
        result = self.run_state(self.state(save_changes=True, edits={first["id"]: self.edit(first, hidden=True)}), 99)
        self.assertEqual(result["selected_id"], second["id"])
        hidden = result["catalog"]["entries"][0]
        result = self.run_state(self.state(save_changes=True, edits={first["id"]: self.edit(hidden, hidden=False)}))
        self.assertEqual(result["selected_id"], first["id"])

    def test_hide_last_commits_without_output(self):
        entry, _ = self.add_image()
        result = self.run_state(self.state(save_changes=True, edits={entry["id"]: self.edit(entry, hidden=True)}))
        self.assertIsNone(result["image"])
        self.assertTrue(self.store.get(self.catalog_id)["entries"][0]["hidden"])

    def test_delete_removes_file_and_replay_does_not_delete_next_image(self):
        first, _ = self.add_image()
        second, _ = self.add_image()
        path = self.store.image_path(self.catalog_id, first["id"])
        state = self.state(save_changes=True, edits={first["id"]: self.edit(first, delete=True)})
        self.run_state(state)
        result = self.run_state(state)
        self.assertFalse(path.exists())
        self.assertEqual([entry["id"] for entry in result["catalog"]["entries"]], [second["id"]])

    def test_concurrent_stale_edits_are_rejected(self):
        entry, _ = self.add_image()
        states = [self.state(save_changes=True, edits={entry["id"]: self.edit(entry, values={**VALUES, "title": title})})
                  for title in ("A", "B")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_state, state) for state in states]
        self.assertEqual(sum(isinstance(future.exception(), CatalogConflict) for future in futures), 1)
        self.assertEqual(self.store.get(self.catalog_id)["entries"][0]["revision"], 2)

    def test_decode_failure_does_not_save_edits(self):
        entry, _ = self.add_image()
        state = self.state(save_changes=True, edits={entry["id"]: self.edit(entry, values={**VALUES, "title": "Changed"})})
        def fail(path):
            raise OSError("Decode failed")
        with self.assertRaises(OSError):
            self.run_state(state, loader=fail)
        self.assertEqual(self.store.get(self.catalog_id)["entries"][0]["revision"], 1)

    def test_atomic_write_failure_preserves_original_and_removes_new_copy(self):
        entry, _ = self.add_image()
        token = uuid.uuid4().hex
        self.store.stage(self.catalog_id, token, b"new bytes", "next.png")
        state = self.state(save_changes=True, new_image={"token": token, "values": VALUES},
                           edits={entry["id"]: self.edit(entry, delete=True)})
        with patch("catalog_store.os.replace", side_effect=OSError("Disk failure")):
            with self.assertRaises(OSError):
                self.run_state(state)
        self.assertEqual(len(self.store.get(self.catalog_id)["entries"]), 1)
        self.assertTrue(self.store.image_path(self.catalog_id, entry["id"]).exists())
        self.assertFalse((self.store.root / self.catalog_id / (token + ".png")).exists())

    def test_delete_catalog_is_scoped(self):
        self.add_image()
        other = self.store.create("Other", [])
        own_token, other_token = uuid.uuid4().hex, uuid.uuid4().hex
        self.store.stage(self.catalog_id, own_token, b"pending", "pending.png")
        self.store.stage(other["id"], other_token, b"other", "other.png")
        self.store.delete(self.catalog_id)
        self.assertFalse((self.store.root / self.catalog_id).exists())
        self.assertEqual(self.store.get(other["id"])["name"], "Other")
        self.assertTrue(self.store.root.exists())
        self.assertFalse((self.store.root / ".staging" / (own_token + ".png")).exists())
        self.assertTrue((self.store.root / ".staging" / (other_token + ".png")).exists())

    def test_path_traversal_and_cross_catalog_upload_are_rejected(self):
        for identifier in ("../outside", "", "C:\\Windows", ".", "a" * 31):
            with self.assertRaises(CatalogError):
                self.store.get(identifier)
        other = self.store.create("Other", [])
        token = uuid.uuid4().hex
        self.store.stage(other["id"], token, b"bytes", "image.png")
        with self.assertRaises(CatalogError):
            self.run_state(self.state(new_image={"token": token, "values": VALUES}))

    def test_schema_mismatch_is_detected_before_changes(self):
        entry, _ = self.add_image()
        state = self.state(schema=[], save_changes=True, edits={entry["id"]: self.edit(entry, delete=True)})
        with self.assertRaises(CatalogConflict):
            self.run_state(state)
        self.assertTrue(self.store.image_path(self.catalog_id, entry["id"]).exists())

    def test_new_image_batch_uses_next_index_after_first_queue(self):
        first, _ = self.add_image()
        second, state = self.add_image()
        state["new_image"]["select"] = False
        self.assertEqual(self.run_state(state, 0)["selected_id"], first["id"])
        self.assertEqual(self.run_state(state, 1)["selected_id"], second["id"])

    def test_connected_index_selects_output_when_adding(self):
        first, _ = self.add_image()
        token = uuid.uuid4().hex
        self.store.stage(self.catalog_id, token, b"bytes", "new.png")
        state = self.state(index_connected=True, new_image={"token": token, "values": VALUES})
        self.assertEqual(self.run_state(state)["selected_id"], first["id"])
        self.assertEqual(len(self.store.get(self.catalog_id)["entries"]), 2)

    def stage_many(self):
        images = []
        for index in range(3):
            token = uuid.uuid4().hex
            self.store.stage(self.catalog_id, token, f"image {index}".encode(), f"photo-{index}.png")
            images.append({"token": token, "values": {**VALUES, "title": f"Image {index}", "seed": index}})
        return images

    def test_multiple_images_save_together_and_retry_does_not_duplicate(self):
        images = self.stage_many()
        self.assertEqual(self.store.get(self.catalog_id)["entries"], [])
        state = self.state(new_images=images, new_selection=images[1]["token"], save_changes=False)
        result = self.run_state(state)
        self.assertEqual(result["values"][0], "Image 1")
        self.assertEqual(result["added_ids"], [image["token"] for image in images])
        self.assertEqual([entry["values"]["seed"] for entry in result["catalog"]["entries"]], [0, 1, 2])
        self.run_state(state)
        self.assertEqual(len(self.store.get(self.catalog_id)["entries"]), 3)
        state["new_selection"] = None
        self.assertEqual(self.run_state(state, 2)["values"][0], "Image 2")
        self.assertEqual(list((self.store.root / ".staging").iterdir()), [])

    def test_multiple_images_missing_file_rolls_back_entire_addition(self):
        existing, _ = self.add_image()
        images = self.stage_many()
        missing = self.store.root / ".staging" / (images[-1]["token"] + ".png")
        missing.unlink()
        state = self.state(new_images=images, new_selection=images[0]["token"], save_changes=True,
                           edits={existing["id"]: self.edit(existing, delete=True)})
        with self.assertRaises(FileNotFoundError):
            self.run_state(state)
        self.assertEqual([entry["id"] for entry in self.store.get(self.catalog_id)["entries"]], [existing["id"]])
        self.assertTrue(self.store.image_path(self.catalog_id, existing["id"]).exists())
        for image in images:
            self.assertFalse((self.store.root / self.catalog_id / (image["token"] + ".png")).exists())

    def test_multiple_images_json_failure_can_be_retried(self):
        images = self.stage_many()
        state = self.state(new_images=images)
        with patch("catalog_store.os.replace", side_effect=OSError("Disk failure")):
            with self.assertRaises(OSError):
                self.run_state(state)
        self.assertEqual(self.store.get(self.catalog_id)["entries"], [])
        self.assertEqual(list((self.store.root / self.catalog_id).glob("*.png")), [])
        self.assertEqual(len(self.run_state(state)["catalog"]["entries"]), 3)

    def test_multiple_images_respect_connected_index(self):
        images = self.stage_many()
        result = self.run_state(self.state(new_images=images, new_selection=images[0]["token"], index_connected=True), 2)
        self.assertEqual(result["values"][0], "Image 2")
        self.assertEqual(len(result["catalog"]["entries"]), 3)

    def test_property_validation(self):
        for schema in ([{"name": "IMAGE", "type": "String"}], SCHEMA * 9,
                       [{"name": "X", "type": "String"}, {"name": "x", "type": "Text"}]):
            with self.assertRaises(CatalogError):
                validate_schema(schema)
        for changes in ({"seed": True}, {"seed": 1.5}, {"seed": 2**53}, {"enabled": "false"}, {"unknown": "x"}):
            with self.assertRaises(CatalogError):
                validate_values(SCHEMA, {**VALUES, **changes})
        self.assertEqual(validate_values(SCHEMA, {}), {"title": "", "prompt": "", "seed": 0, "enabled": False})

    def test_schema_add_remove_and_hide_preserve_other_values(self):
        entry, old_state = self.add_image()
        schema = [{**SCHEMA[0], "hidden": True}, SCHEMA[2], SCHEMA[3], {"name": "notes", "type": "Text"}]
        updated = self.store.update_schema(self.catalog_id, schema, 0)
        values = updated["entries"][0]["values"]
        self.assertEqual(values, {"title": "First", "seed": 42, "enabled": True, "notes": ""})
        self.assertEqual(updated["entries"][0]["revision"], entry["revision"] + 1)
        self.assertEqual(updated["schema_revision"], 1)
        output = self.run_state(self.state(schema=schema))
        self.assertEqual(output["values"], ["First", 42, True, ""])
        with self.assertRaises(CatalogConflict):
            self.run_state(old_state)
        with self.assertRaises(CatalogConflict):
            self.store.update_schema(self.catalog_id, SCHEMA, 0)

    def test_schema_failure_is_atomic_and_existing_types_cannot_change(self):
        self.add_image()
        before = self.store.get(self.catalog_id)
        with patch("catalog_store.os.replace", side_effect=OSError("Disk failure")):
            with self.assertRaises(OSError):
                self.store.update_schema(self.catalog_id, SCHEMA[:-1], 0)
        self.assertEqual(self.store.get(self.catalog_id), before)
        with self.assertRaises(CatalogError):
            self.store.update_schema(self.catalog_id, [{"name": "seed", "type": "Text"}], 0)

    def test_schema_revision_rejects_old_prompt_after_hide_then_unhide(self):
        self.add_image()
        self.store.update_schema(self.catalog_id, [{**SCHEMA[0], "hidden": True}, *SCHEMA[1:]], 0)
        self.store.update_schema(self.catalog_id, SCHEMA, 1)
        with self.assertRaises(CatalogConflict):
            self.run_state(self.state(schema_revision=0))


if __name__ == "__main__":
    unittest.main()
