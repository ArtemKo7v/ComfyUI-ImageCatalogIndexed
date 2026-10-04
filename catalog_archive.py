"""Portable ZIP catalogs with bounded, explicitly named file extraction."""

import copy
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
import zipfile

from .catalog_store import CatalogConflict, CatalogError, validate_catalog, validate_schema, validate_values

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_001
MEMBER_PATTERN = re.compile(r"(?:catalog\.json|[0-9a-f]{32}\.png)\Z")


def export_catalog(store, catalog_id):
    """Return a seekable temporary ZIP stream and a display name."""
    stream = tempfile.TemporaryFile(mode="w+b")
    try:
        with store.lock:
            data = store.get(catalog_id)
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.write(store._child(store._directory(catalog_id), "catalog.json"), "catalog.json")
                for entry in data["entries"]:
                    path = store._child(store._directory(catalog_id), entry["file"])
                    archive.write(path, entry["file"])
        stream.seek(0)
        return stream, data["name"]
    except Exception:
        stream.close()
        raise


def import_catalog(store, stream, validate_image, catalog_id=None, expected_revision=None, field_policy=None):
    """Validate an archive, then create a catalog or append missing records."""
    try:
        if field_policy not in (None, "ignore", "extend"):
            raise CatalogError("Unsupported import field policy.")
        return _import_catalog(store, stream, validate_image, catalog_id, expected_revision, field_policy)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as error:
        raise CatalogError("Invalid or unsupported catalog ZIP archive.") from error


def _import_catalog(store, stream, validate_image, catalog_id, expected_revision, field_policy):
    """Implement validation and atomic publication for archive imports."""
    with zipfile.ZipFile(stream) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_FILES:
            raise CatalogError("Archive contains too many files (maximum 10,000 images).")
        # Only accept the flat, named layout; reject paths, duplicates, links,
        # and encrypted members before extracting anything.
        names = set()
        for member in members:
            if not MEMBER_PATTERN.fullmatch(member.filename) or member.filename in names:
                raise CatalogError("Archive contains an invalid or duplicate file path.")
            if member.flag_bits & 1 or stat.S_ISLNK(member.external_attr >> 16):
                raise CatalogError("Encrypted files and symbolic links are not supported.")
            names.add(member.filename)
        if "catalog.json" not in names:
            raise CatalogError("Archive must contain catalog.json at its root.")
        if sum(member.file_size for member in members) > MAX_UNPACKED_BYTES:
            raise CatalogError("Unpacked archive exceeds 2 GiB.")
        if archive.getinfo("catalog.json").file_size > MAX_JSON_BYTES:
            raise CatalogError("Catalog JSON exceeds 16 MiB.")
        # Metadata and file names must agree exactly before image extraction.
        data = validate_catalog(json.loads(archive.read("catalog.json")))
        expected = {"catalog.json", *(entry["file"] for entry in data["entries"])}
        if names != expected:
            raise CatalogError("Archive images do not match the catalog JSON.")
        data = copy.deepcopy(data)
        data["id"] = uuid.uuid4().hex
        data["schema_revision"] = 0
        data["revision"] = 0
        data["applied_operations"] = []
        store.root.mkdir(parents=True, exist_ok=True)
        # Assemble and verify every image privately before publishing a catalog
        # or appending records to an existing one.
        with tempfile.TemporaryDirectory(prefix=".import-", dir=store.root) as temporary:
            directory = Path(temporary)
            for entry in data["entries"]:
                source_name = entry["file"]
                entry["id"] = uuid.uuid4().hex
                entry["file"] = entry["id"] + ".png"
                entry["revision"] = 1
                target = directory / entry["file"]
                with archive.open(source_name) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                validate_image(target)
            with store.lock:
                if catalog_id is not None:
                    return _append_catalog(store, catalog_id, expected_revision, data, directory, field_policy)
                # Imported names gain a deterministic suffix instead of replacing
                # an existing catalog.
                existing = {item["name"].casefold() for item in store.list()["catalogs"]}
                base = data["name"].strip()
                name, index = base, 1
                while name.casefold() in existing:
                    suffix = " (imported)" if index == 1 else f" (imported {index})"
                    name = base[:120 - len(suffix)] + suffix
                    index += 1
                data["name"] = name
                store._write(directory / "catalog.json", data)
                directory.rename(store._directory(data["id"]))
        return data


def _append_catalog(store, catalog_id, expected_revision, incoming, temporary, field_policy):
    """Publish additions under the store lock, rolling back files on failure."""
    data = store.get(catalog_id)
    if type(expected_revision) is not int or expected_revision != data["revision"]:
        raise CatalogConflict("Catalog changed on the server. Reload it before importing additions.")
    if incoming["sync_id"] != data["sync_id"]:
        raise CatalogError("This archive belongs to a different catalog. Import it as a new catalog instead.")
    incoming_fields = {field["name"]: field["type"] for field in incoming["schema"]}
    local_fields = {field["name"]: field["type"] for field in data["schema"]}
    if any(incoming_fields[name] != local_fields[name] for name in incoming_fields.keys() & local_fields.keys()):
        raise CatalogError("Catalog fields differ: shared property types must match before importing additions.")
    new_fields = [field for field in incoming["schema"] if field["name"] not in local_fields]
    missing_fields = [field for field in data["schema"] if field["name"] not in incoming_fields]
    if (new_fields or missing_fields) and field_policy is None:
        return {"needs_field_choice": True, "new_fields": new_fields, "missing_fields": missing_fields}
    extend = bool(new_fields) and field_policy == "extend"
    if extend:
        data["schema"] = validate_schema(data["schema"] + new_fields)
        for entry in data["entries"]:
            entry["values"] = validate_values(data["schema"], entry["values"])
            entry["revision"] += 1
        data["schema_revision"] += 1
    known = {entry["sync_id"] for entry in data["entries"]}
    additions = [entry for entry in incoming["entries"] if entry["sync_id"] not in known]
    for entry in additions:
        entry["values"] = validate_values(data["schema"], {
            field["name"]: entry["values"][field["name"]] for field in data["schema"]
            if field["name"] in entry["values"]
        })
    if additions or extend:
        directory = store._directory(catalog_id)
        destinations = []
        try:
            for entry in additions:
                destination = store._child(directory, entry["file"])
                with (temporary / entry["file"]).open("rb") as source, destination.open("xb") as target:
                    destinations.append(destination)
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
            data["entries"].extend(additions)
            data["revision"] += 1
            data["applied_operations"] = []
            validate_catalog(data, catalog_id)
            store._write(store._child(directory, "catalog.json"), data)
        except Exception:
            for destination in destinations:
                destination.unlink(missing_ok=True)
            raise
    return {"catalog": data, "added": len(additions), "skipped": len(incoming["entries"]) - len(additions),
            "fields_added": len(new_fields) if extend else 0}
