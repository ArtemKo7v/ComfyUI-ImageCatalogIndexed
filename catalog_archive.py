"""Portable ZIP catalogs with bounded, explicitly named file extraction."""

import copy
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
import zipfile

from .catalog_store import CatalogError, validate_catalog

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
            # Replay history is local execution state, not portable catalog data.
            data["applied_operations"] = []
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("catalog.json", json.dumps(data, ensure_ascii=False, indent=2))
                for entry in data["entries"]:
                    path = store._child(store._directory(catalog_id), entry["file"])
                    archive.write(path, entry["file"])
        stream.seek(0)
        return stream, data["name"]
    except Exception:
        stream.close()
        raise


def import_catalog(store, stream, validate_image):
    """Import as a fresh catalog, publishing it only after every file is checked."""
    try:
        return _import_catalog(store, stream, validate_image)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError) as error:
        raise CatalogError("Invalid or unsupported catalog ZIP archive.") from error


def _import_catalog(store, stream, validate_image):
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
        data["applied_operations"] = []
        store.root.mkdir(parents=True, exist_ok=True)
        # Assemble and verify the import privately; the final rename publishes
        # only a complete catalog.
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
