# ComfyUI-ImageCatalogIndexed

A ComfyUI custom node for persistent image catalogs with a typed property schema.
Select an image by index and output its `IMAGE` tensor and named property values.

## Installation

Place this repository in `ComfyUI/custom_nodes/ComfyUI-ImageCatalogIndexed`, restart
ComfyUI, and refresh the browser. Add **Image Catalog Indexed** from `image/catalog`.
Python 3.10 or newer is required. The runtime uses ComfyUI's existing PyTorch,
NumPy, Pillow, and aiohttp dependencies; no model downloads are needed.
There are no additional runtime packages to install, so this node does not need a
`requirements.txt`. `requirements-dev.txt` is only for development and CI.

## Create a catalog

1. Leave **Create a new Image Catalog** selected and enter a **Catalog name**.
2. Enter property names and choose their types. Filling the last row adds another
   row, up to 32 properties. Empty rows are ignored; a schema may have no properties.
3. Click **Create Now**. The catalog becomes active in add-image mode.
4. Choose a local image and enter its property values. Use **Add Image** to prepare
   more images. **Save Changes** saves all images and properties without running
   the workflow; running the workflow alone never saves the catalog.

Catalog names must be unique, ignoring case. Property names must also be unique,
and `IMAGE` is reserved for the image output. Use **Edit Catalog** to manage fields
after creation.

| Property type | Editor | Output type | Default |
| --- | --- | --- | --- |
| String | Single line | `STRING` | Empty string |
| Text | Multiple lines | `STRING` | Empty string |
| Integer | Integer field | `INT` | `0` |
| Boolean | Checkbox | `BOOLEAN` | `false` |

Integers must be within JavaScript's safe integer range, from
`-9007199254740991` to `9007199254740991`.

## Select images and run

The image dropdown and preview select a record from the active catalog. Choosing
a visible record updates `image_index`. This is the only input that can be
connected to another node; images and property editors are not input sockets.

The selector heading shows **Image (N/NN)**: the displayed image's one-based
position and the total number of local catalog images, including hidden records
and drafts with selected files. Empty drafts are not counted and show position
zero. The counter updates when selecting, adding, deleting, or reloading images.
Arrow buttons on either side of the preview navigate this list without wrapping.
They are vertically centered; the previous arrow is absent on the first image,
the next arrow on the last, and both are absent for a single image.

The catalog editor expands with the node and fills the remaining height. It has
a minimum height of 420 px and scrolls internally when its contents do not fit.

`image_index` starts at zero and addresses the ordered list of **visible** records.
Out-of-range indices wrap around. **Index after queue** is a regular node widget
directly below `image_index`, like a seed control. It is available before a catalog
is selected and keeps its mode when switching catalogs. It controls the next run:

- `fixed`: keep the current index.
- `increment`: advance by one, wrapping from the last image to the first.
- `decrement`: go back by one, wrapping from the first image to the last.
- `randomize`: select a random visible index; repeats are allowed.

The browser advances the index after a prompt is successfully queued, including
each item in a batch. A later execution error does not roll back this advance.
When `image_index` is connected, its incoming value takes precedence and automatic
index control is disabled. After execution, the preview follows that result.
Manual edits always belong to the record shown when they were entered, even when
the connected index selects a different output image.

The first output is an RGB `IMAGE` tensor shaped `[1, height, width, 3]` with values
between zero and one. Following outputs have the property names, types, and order
from the schema. Alpha is retained in the stored PNG but is not a separate output.
For animated images, only the first frame is stored. EXIF orientation is applied.

## Edit, hide, and delete

The editor has two bordered blocks. The upper block contains **Catalog**, with
**Refresh** to its right, and the catalog actions. Creation and field-editing forms
open inside this block. The lower image block appears only when a catalog is
selected. It contains the image selector, preview, properties, and **Hide**.

Each node owns a local catalog snapshot stored in the workflow. All property,
visibility, field, and image-list changes affect workflow outputs immediately,
without writing the server JSON. Switching images retains all pending changes.

**Hide** makes the preview and fields translucent and read-only. Hidden records
are skipped by index selection but remain in the dropdown for restoration.
Clear **Hide** to include the record again.

The red **Delete** button on the right asks for confirmation and removes the
displayed image from the local catalog. The server record and image file remain
until **Save Changes**. Deleting a new draft removes only that draft.

Below the divider, **Add Image** and **Save Changes** are aligned to the left.
**Add Image** starts another draft; the image dropdown lets you switch between
drafts and stored records. Blank drafts without files are ignored.
**Save Changes** asks to save **all changes for all images and fields**, stages
new files, and commits the complete catalog through the API. It also permanently
removes files for deleted records. No workflow run is necessary.

Workflow queueing stages new files as needed for execution, but never adds them
to the saved catalog. An unconnected index outputs the displayed new image on its
first run; subsequent batch items follow the index mode. A connected index always
takes precedence. The node outputs one image per execution. If no visible images
remain, downstream nodes are skipped.

Uploads are limited to 64 MiB per file and converted to PNG. Save operations use
atomic JSON replacement and roll back new image copies if saving fails. A retry
does not duplicate images. Version checks reject a stale save from another node
or browser tab; local edits remain available. Copy your local edits or save the
workflow before using Reload to resolve a conflict.

**Refresh** updates only the available catalog list. **Reload Catalog** discards
local changes after confirmation and loads the server version. Switching catalogs
also asks before discarding local changes.

**Delete Catalog** remains a separate, immediate operation: after confirmation it
permanently deletes the catalog JSON, its images, and its staged uploads. Creation
and ZIP import also create server catalogs immediately.
Other catalogs are not affected.

## Catalog fields

**Edit Catalog** opens a separate editor for the selected catalog's schema.
Use **Add property** to append a field, **Remove property** to remove one, and
**Hidden** to hide a field from image forms. Up to 32 fields are supported,
including hidden fields. Existing field names and types are read-only; create a
new field when a different name or type is needed.

**Apply Fields** updates only this node's local schema; **Save Changes** persists
it together with all image edits. New fields receive their type's default value
in every record and pending draft. Removing a field asks for confirmation and
removes its local values; **Cancel** discards edits not yet applied.

Hidden fields retain their values and named outputs. Clear **Hidden** in the
editor to show them again. Removing a field removes its output; connections to
remaining fields are preserved when their slots shift. Other nodes retain their
own workflow snapshots rather than receiving unsolicited schema changes.

Finish field editing with **Apply Fields** or **Cancel** before saving or exporting.

## Export and import

**Export Catalog** downloads a ZIP containing the server's exact `catalog.json`
and its saved PNG files at the archive root, including hidden records and fields.
If there are local changes, a dialog offers **Save and Export**, **Export Saved
Version**, or **Cancel**. Exporting the saved version leaves local changes intact.
Saving first commits all local changes, including deleted images, before export.

**Import Catalog** is available when **Create a new Image Catalog** is selected.
Choose an exported ZIP to import its data as a separate catalog. The imported
catalog becomes active. Identifiers are regenerated and existing catalogs are
never overwritten. Name collisions receive an ` (imported)` suffix, numbered when
necessary. A failed import does not publish a partial catalog.

Import accepts ZIP files up to 512 MiB, with up to 10,000 images, 16 MiB of JSON,
and 2 GiB of total unpacked data. Files must match the JSON manifest and contain
valid PNG images. Nested paths, symbolic links, duplicate members, encrypted
archives, and missing image files are rejected. These limits apply to import;
very large exports may need to be split into smaller catalogs before importing.

## Storage and workflow persistence

Files live under the configured ComfyUI user directory:

```text
user/artemko7v_image_catalog-indexed/
  <catalog-id>/
    catalog.json
    <image-id>.png
  .staging/
    <upload-token>.json
    <upload-token>.png
```

They do not appear in the normal **Load Image** list. Original filenames are
display labels; generated identifiers keep storage paths independent of names.
Catalog JSON contains the schema, ordered entries, record revisions, and recent
operation identifiers. Writes replace JSON atomically. Concurrent stale edits are
rejected with a message to reload, rather than silently overwriting newer values.
One ComfyUI process should own a storage directory.

Uploaded staging files are removed after a successful save. Abandoned uploads
expire after seven days and are cleaned during later uploads. Workflow execution,
including downstream failures, never changes the saved catalog.

Workflow JSON preserves the full catalog metadata snapshot, schema/output layout,
index mode, selected record, pending drafts, and property edits. Restoring a
workflow does not replace that snapshot with the server's latest JSON. It does
not embed image bytes: back up storage separately. Unsaved local files must be
selected again after restoring a workflow; saved images load from the server.
If another editor has deleted a referenced server image, the workflow cannot
recover its bytes.

API clients can execute a serialized `catalog_state` containing `catalog_id` and
`client_catalog` (the complete metadata snapshot). They must stage new files
explicitly and supply their indices. Use `POST /artemko7v/image-catalog/catalogs/{id}/save`
with `catalog`, `expected_revision`, and a unique 32-character lowercase hexadecimal
`operation_id` to save. Legacy workflow snapshots remain executable, but no longer
save on execution. The node remains an output node so it can execute without
connected outputs.

## Development checks

Storage and index tests need only Python and Node.js 22 or newer:

```text
python -m unittest discover -s tests -v
node --test tests/catalog_model.test.mjs
```

HTTP integration tests run when Pillow, NumPy, and aiohttp are installed; otherwise
they are skipped. Install all test dependencies to run the complete suite. The
browser smoke test uses Playwright's Chromium by default:

```text
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m unittest discover -s tests -v
node --test tests/catalog_model.test.mjs
python tests/browser_smoke.py
```

On Linux, use `python -m playwright install --with-deps chromium` to also install
browser system dependencies. To use an already installed Microsoft Edge instead,
set `PLAYWRIGHT_CHANNEL=msedge` in the environment. The smoke screenshot is saved
to `test-results/catalog-browser-smoke.png`.

GitHub Actions runs Python tests on 3.10, 3.12, and 3.13, JavaScript tests on Node.js
22, and the Chromium smoke test. CI installs all integration dependencies.
Registry publishing runs the same checks first and publishes only from `main`.
It is triggered by changes to `pyproject.toml` or manually through Actions and
requires the `COMFY_REGISTRY_TOKEN` repository secret. Increment `project.version`
before publishing a new registry version.

The browser test uses temporary catalogs, the real extension JavaScript and HTTP
handlers, and a small ComfyUI host substitute. It checks index control placement,
multiple drafts, deferred uploads, previews, typed outputs, saving, restoration,
hiding, schema editing, ZIP export/import, and deletion. PyTorch is substituted
with NumPy in this harness. It does **not** replace a smoke test in an actual
ComfyUI installation, including graph links and the frontend's node renderer.

The integration follows the upstream [node properties](https://docs.comfy.org/custom-nodes/backend/server_overview)
and [JavaScript extension APIs](https://docs.comfy.org/custom-nodes/js/javascript_objects_and_hijacking).
