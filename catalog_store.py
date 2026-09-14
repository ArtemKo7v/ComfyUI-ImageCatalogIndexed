"""Catalog persistence, validation, and execution transactions."""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid

MAX_PROPERTIES = 32
MAX_INTEGER = 9_007_199_254_740_991
PROPERTY_TYPES = {"String": "STRING", "Text": "STRING", "Integer": "INT", "Boolean": "BOOLEAN"}
DEFAULTS = {"String": "", "Text": "", "Integer": 0, "Boolean": False}
ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class CatalogError(ValueError):
    """An invalid catalog request or a missing resource."""


class CatalogConflict(CatalogError):
    """An edit is based on an outdated record."""


def validate_id(value):
    """Validate catalog, image, upload, and operation identifiers."""
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise CatalogError("Invalid catalog or image identifier.")
    return value


def validate_schema(schema):
    """Normalize a schema so every access path uses the same names, types,
    and optional visibility flag."""
    if not isinstance(schema, list) or len(schema) > MAX_PROPERTIES:
        raise CatalogError("A catalog supports up to 32 properties.")
    result, names = [], set()
    for field in schema:
        if not isinstance(field, dict):
            raise CatalogError("Invalid property definition.")
        name, kind = field.get("name"), field.get("type")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise CatalogError("Property names must contain 1 to 80 characters.")
        name = name.strip()
        if name.casefold() in names or name.casefold() == "image":
            raise CatalogError("Property names must be unique and cannot be IMAGE.")
        if not isinstance(kind, str) or kind not in PROPERTY_TYPES:
            raise CatalogError("Unsupported property type.")
        if type(field.get("hidden", False)) is not bool:
            raise CatalogError("Property visibility must be a boolean.")
        names.add(name.casefold())
        result.append({"name": name, "type": kind, **({"hidden": True} if field.get("hidden") else {})})
    return result


def validate_values(schema, values):
    """Fill omitted fields with defaults and reject stale keys so entries
    remain aligned with the catalog schema."""
    if not isinstance(values, dict):
        raise CatalogError("Property values must be an object.")
    if set(values) - {field["name"] for field in schema}:
        raise CatalogError("Unknown property in image values.")
    result = {}
    for field in schema:
        name, kind = field["name"], field["type"]
        value = values.get(name, DEFAULTS[kind])
        valid = (
            (kind in ("String", "Text") and isinstance(value, str))
            or (kind == "Integer" and type(value) is int and abs(value) <= MAX_INTEGER)
            or (kind == "Boolean" and type(value) is bool)
        )
        if not valid:
            raise CatalogError(f"Invalid {kind} value for property '{name}'.")
        result[name] = value
    return result


def visible_entries(catalog):
    """Return only records that participate in index-based selection."""
    return [entry for entry in catalog["entries"] if not entry["hidden"]]


def validate_catalog(data, catalog_id=None):
    """Validate on-disk and portable catalog metadata using the same rules."""
    try:
        if not isinstance(data, dict) or data["version"] != 1:
            raise CatalogError("Unsupported catalog format.")
        validate_id(data["id"])
        if catalog_id is not None and data["id"] != catalog_id:
            raise CatalogError("Catalog identifier does not match its directory.")
        if not isinstance(data["name"], str) or not 1 <= len(data["name"].strip()) <= 120:
            raise CatalogError("Invalid catalog name.")
        if not isinstance(data["entries"], list):
            raise CatalogError("Invalid catalog entries.")
        data["schema"] = validate_schema(data["schema"])
        revision = data.setdefault("schema_revision", 0)
        if type(revision) is not int or revision < 0:
            raise CatalogError("Invalid schema revision.")
        # Imported and on-disk records share these integrity checks.
        seen = set()
        for entry in data["entries"]:
            validate_id(entry["id"])
            if entry["id"] in seen or entry["file"] != entry["id"] + ".png":
                raise CatalogError("Invalid image record.")
            seen.add(entry["id"])
            if not isinstance(entry["filename"], str) or not 1 <= len(entry["filename"]) <= 255:
                raise CatalogError("Invalid image filename.")
            entry["values"] = validate_values(data["schema"], entry["values"])
            if type(entry["hidden"]) is not bool or type(entry["revision"]) is not int or entry["revision"] < 1:
                raise CatalogError("Invalid image record state.")
        if not isinstance(data.setdefault("applied_operations", []), list):
            raise CatalogError("Invalid operation history.")
    except (KeyError, TypeError) as error:
        raise CatalogError("Invalid catalog structure.") from error
    return data


class CatalogStore:
    """Requests and node executions share a lock in the ComfyUI process."""

    def __init__(self, root):
        """Initialize process-local storage rooted at the supplied directory."""
        self.root = Path(root).resolve()
        self.lock = threading.RLock()

    def _directory(self, catalog_id):
        """Resolve an ID-based path without allowing it to escape storage."""
        path = self.root / validate_id(catalog_id)
        if path.is_symlink() or path.resolve().parent != self.root:
            raise CatalogError("Catalog path escapes the storage directory.")
        return path

    @staticmethod
    def _child(directory, name):
        """Resolve a direct child path within a trusted directory."""
        path = directory / name
        if path.is_symlink() or path.resolve().parent != directory.resolve():
            raise CatalogError("File path escapes the catalog directory.")
        return path

    def _write(self, path, data):
        """Atomically replace JSON with a fsynced sibling file."""
        descriptor, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _read(self, catalog_id):
        """Read and validate one catalog metadata file."""
        path = self._child(self._directory(catalog_id), "catalog.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise CatalogError("Catalog does not exist. Refresh the catalog list.") from error
        except (OSError, ValueError) as error:
            raise CatalogError("Catalog JSON cannot be read.") from error
        return validate_catalog(data, catalog_id)

    def get(self, catalog_id):
        """Return the current metadata for one catalog."""
        with self.lock:
            return self._read(catalog_id)

    def list(self):
        """Return valid catalog summaries and recoverable read errors."""
        with self.lock:
            if not self.root.exists():
                return {"catalogs": [], "errors": []}
            catalogs, errors = [], []
            # Keep valid catalogs available even when one catalog is malformed.
            for path in sorted(self.root.iterdir()):
                if not ID_PATTERN.fullmatch(path.name):
                    continue
                try:
                    data = self._read(path.name)
                    catalogs.append({"id": data["id"], "name": data["name"], "count": len(data["entries"])})
                except CatalogError as error:
                    errors.append({"id": path.name, "error": str(error)})
            catalogs.sort(key=lambda item: (item["name"].casefold(), item["id"]))
            return {"catalogs": catalogs, "errors": errors}

    def create(self, name, schema):
        """Create a named catalog with a validated initial schema."""
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise CatalogError("Catalog name must contain 1 to 120 characters.")
        schema = validate_schema(schema)
        with self.lock:
            if any(item["name"].casefold() == name.strip().casefold() for item in self.list()["catalogs"]):
                raise CatalogConflict("A catalog with this name already exists.")
            catalog_id = uuid.uuid4().hex
            data = {"version": 1, "id": catalog_id, "name": name.strip(), "schema": schema,
                    "entries": [], "applied_operations": []}
            directory = self._directory(catalog_id)
            directory.mkdir(parents=True)
            try:
                self._write(directory / "catalog.json", data)
            except Exception:
                directory.rmdir()
                raise
            return data

    def update_schema(self, catalog_id, schema, expected_revision):
        """Update a schema only if its optimistic revision still matches."""
        schema = validate_schema(schema)
        with self.lock:
            data = self._read(catalog_id)
            if type(expected_revision) is not int or data["schema_revision"] != expected_revision:
                raise CatalogConflict("Catalog fields changed in another editor. Reopen Edit Catalog.")
            # Types define stored values and output slots, so replacements must
            # be added as new properties rather than changing existing ones.
            existing = {field["name"]: field["type"] for field in data["schema"]}
            for field in schema:
                if field["name"] in existing and field["type"] != existing[field["name"]]:
                    raise CatalogError("Existing property types cannot be changed. Add a new property instead.")
            if data["schema"] == schema:
                return data
            # Migrate every record in the same transaction as the schema.
            for entry in data["entries"]:
                entry["values"] = validate_values(schema, {
                    field["name"]: entry["values"].get(field["name"], DEFAULTS[field["type"]]) for field in schema
                })
                entry["revision"] += 1
            data["schema"] = schema
            data["schema_revision"] += 1
            data["applied_operations"] = []
            self._write(self._child(self._directory(catalog_id), "catalog.json"), data)
            return data

    def delete(self, catalog_id):
        """Delete the catalog and its associated staged uploads."""
        with self.lock:
            self._read(catalog_id)
            directory = self._directory(catalog_id)
            for child in directory.iterdir():
                if child.is_symlink() or child.is_dir():
                    raise CatalogError("Catalog contains an unexpected directory or symbolic link.")
            uploads = []
            staging = self._child(self.root, ".staging")
            if staging.exists():
                for metadata in staging.glob("*.json"):
                    if not ID_PATTERN.fullmatch(metadata.stem):
                        continue
                    metadata = self._child(staging, metadata.name)
                    if json.loads(metadata.read_text(encoding="utf-8")).get("catalog_id") == catalog_id:
                        uploads.extend([self._child(staging, metadata.stem + ".png"), metadata])
            # Hide the directory atomically before removing its contents.
            tombstone = self._child(self.root, ".deleted-" + catalog_id)
            directory.rename(tombstone)
            shutil.rmtree(tombstone)
            for path in uploads:
                path.unlink(missing_ok=True)

    def image_path(self, catalog_id, entry_id):
        """Resolve the stored PNG path for an existing image entry."""
        with self.lock:
            data = self._read(catalog_id)
            entry = next((item for item in data["entries"] if item["id"] == entry_id), None)
            if entry is None:
                raise CatalogError("Image record does not exist.")
            return self._child(self._directory(catalog_id), entry["file"])

    def _staging(self, catalog_id):
        """Return the staging directory after verifying the catalog."""
        self._read(catalog_id)
        path = self._child(self.root, ".staging")
        path.mkdir(exist_ok=True)
        return path

    def stage(self, catalog_id, token, png_bytes, filename):
        """Store a validated PNG for a queued execution without adding a record."""
        validate_id(token)
        filename = str(filename).replace("\\", "/").rsplit("/", 1)[-1][:255] or "image.png"
        with self.lock:
            if any(entry["id"] == token for entry in self._read(catalog_id)["entries"]):
                return
            staging = self._staging(catalog_id)
            # Staging survives queueing but abandoned uploads expire.
            cutoff = time.time() - 7 * 24 * 60 * 60
            for old in staging.iterdir():
                if old.is_file() and not old.is_symlink() and old.stat().st_mtime < cutoff:
                    old.unlink()
            metadata = self._child(staging, token + ".json")
            image = self._child(staging, token + ".png")
            if metadata.exists():
                previous = json.loads(metadata.read_text(encoding="utf-8"))
                if previous["catalog_id"] != catalog_id:
                    raise CatalogConflict("Upload identifier belongs to another catalog.")
                return
            created = False
            try:
                with image.open("xb") as stream:
                    created = True
                    stream.write(png_bytes)
                self._write(metadata, {"catalog_id": catalog_id, "filename": filename})
            except Exception:
                if created:
                    image.unlink(missing_ok=True)
                raise

    def execute(self, catalog_id, image_index, state, load_image):
        """Resolve outputs and atomically commit edits after image decoding succeeds."""
        if type(image_index) is not int or image_index < 0:
            raise CatalogError("image_index must be a non-negative integer.")
        if not isinstance(state, dict):
            raise CatalogError("Invalid catalog state.")
        with self.lock:
            # Work on a copy until validation and decoding succeed, so failures
            # cannot partially alter the saved catalog.
            original = self._read(catalog_id)
            if (state.get("schema") != original["schema"]
                    or state.get("schema_revision", original["schema_revision"]) != original["schema_revision"]):
                raise CatalogConflict("Catalog schema changed. Select the catalog again before running.")
            data = copy.deepcopy(original)
            directory = self._directory(catalog_id)
            edits = state.get("edits", {})
            if not isinstance(edits, dict):
                raise CatalogError("Invalid record edits.")
            save = state.get("save_changes", False)
            if type(save) is not bool:
                raise CatalogError("Save changes must be a boolean.")
            # This ID makes a repeated queued prompt idempotent.
            operation = validate_id(state.get("operation_id"))
            replay = operation in data["applied_operations"]
            removed, changed = [], False
            # Saved hides and deletions affect the visible index before output
            # selection, so apply them first.
            for entry_id, edit in edits.items():
                validate_id(entry_id)
                if not isinstance(edit, dict):
                    raise CatalogError("Invalid record edit.")
                values = validate_values(data["schema"], edit.get("values", {}))
                if type(edit.get("hidden")) is not bool or type(edit.get("delete")) is not bool:
                    raise CatalogError("Hide and delete must be boolean values.")
                if replay and save:
                    continue
                entry = next((item for item in data["entries"] if item["id"] == entry_id), None)
                if entry is None:
                    raise CatalogConflict("An edited image was deleted. Refresh the catalog.")
                if save:
                    if entry["revision"] != edit.get("revision"):
                        raise CatalogConflict("An image was changed by another node. Refresh before saving.")
                    if edit["delete"]:
                        removed.append(self._child(directory, entry["file"]))
                        data["entries"].remove(entry)
                    else:
                        entry.update(values=values, hidden=edit["hidden"], revision=entry["revision"] + 1)
                    changed = True
            # Accept old API workflows that still contain a single new_image.
            legacy_image = state.get("new_image")
            new_images = state.get("new_images", [legacy_image] if legacy_image else [])
            if not isinstance(new_images, list):
                raise CatalogError("New images must be a list.")
            # A staged PNG becomes an entry only during execution. Its token is
            # both the staging key and the future entry ID.
            staged_paths, added_entries, tokens = {}, [], set()
            for new_image in new_images:
                if not isinstance(new_image, dict):
                    raise CatalogError("Invalid new image state.")
                token = validate_id(new_image.get("token"))
                if token in tokens:
                    raise CatalogError("New image identifiers must be unique.")
                tokens.add(token)
                values = validate_values(data["schema"], new_image.get("values", {}))
                added = next((entry for entry in data["entries"] if entry["id"] == token), None)
                if added is None and replay:
                    raise CatalogConflict("The previously added image has been deleted.")
                if added is None:
                    staging = self._staging(catalog_id)
                    try:
                        metadata = json.loads(self._child(staging, token + ".json").read_text(encoding="utf-8"))
                    except FileNotFoundError as error:
                        raise CatalogError("Upload expired or is missing. Select the image again.") from error
                    if metadata["catalog_id"] != catalog_id:
                        raise CatalogError("Upload belongs to another catalog.")
                    staged_paths[token] = self._child(staging, token + ".png")
                    added = {"id": token, "file": token + ".png", "filename": metadata["filename"],
                             "values": values, "hidden": False, "revision": 1}
                    data["entries"].append(added)
                    changed = True
                added_entries.append(added)
            visible = visible_entries(data)
            selection = state.get("new_selection")
            if legacy_image and legacy_image.get("select", True):
                selection = legacy_image.get("token")
            selected_new = next((entry for entry in added_entries if entry["id"] == selection and not entry["hidden"]), None)
            selected = selected_new if selected_new and not state.get("index_connected") else (
                visible[image_index % len(visible)] if visible else None
            )
            tensor, output_values, warnings = None, [], []
            if selected:
                source = staged_paths.get(selected["id"]) or self._child(directory, selected["file"])
                tensor = load_image(source)
                values = selected["values"]
                if not save and selected["id"] in edits:
                    values = validate_values(data["schema"], edits[selected["id"]]["values"])
                output_values = [values[field["name"]] for field in data["schema"]]
            if changed:
                # Copy images, publish metadata, then clean obsolete files. This
                # avoids live metadata that points to a removed image.
                data["applied_operations"] = (data["applied_operations"] + [operation])[-128:]
                destinations = []
                try:
                    for token, staged_path in staged_paths.items():
                        target_path = self._child(directory, token + ".png")
                        with staged_path.open("rb") as source, target_path.open("xb") as target:
                            destinations.append(target_path)
                            shutil.copyfileobj(source, target)
                            target.flush()
                            os.fsync(target.fileno())
                    self._write(self._child(directory, "catalog.json"), data)
                except Exception:
                    for destination in destinations:
                        destination.unlink(missing_ok=True)
                    raise
                # Commit metadata first to avoid live records pointing at deleted files.
                cleanup = removed[:]
                for staged_path in staged_paths.values():
                    cleanup.extend([staged_path, staged_path.with_suffix(".json")])
                for path in cleanup:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logging.exception("Could not remove an unreferenced catalog image: %s", path)
                        warnings.append("Some unreferenced files could not be removed. Check the server log.")
            return {"image": tensor, "values": output_values, "catalog": data,
                    "selected_id": selected["id"] if selected else None,
                    "operation_id": operation, "saved": save,
                    "added_id": added_entries[0]["id"] if added_entries else None,
                    "added_ids": [entry["id"] for entry in added_entries],
                    "warnings": sorted(set(warnings))}
