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
4. Choose a local image and enter its property values. Use **Add next image** to
   prepare more images before running, then queue the workflow to save them all.

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

With **Save changes** off, edited values are used for the corresponding record's
outputs but are not written to the catalog. Hide and delete flags are not applied.
With **Save changes** on, all pending record edits in that node are saved when it
executes. Edits are retained while switching records within the same catalog.

**Hide** makes the preview and fields translucent and the fields read-only. Saved
hidden records are skipped by index selection, but remain in the dropdown. To
restore one, select it, clear **Hide**, enable **Save changes**, and run.

**Delete** also adds a red background. On a run with **Save changes** enabled,
the record and its image file are removed. Hide/delete controls remain editable
so a pending action can be cancelled. Saving these actions happens before output
selection; the index then selects from the remaining visible records. If none
remain, the action is saved and downstream nodes are skipped.

**Add image** starts a new draft. After choosing a file and entering its properties,
click **Add next image** to prepare another. The image dropdown includes every
pending draft, so you can return to an earlier image and edit its properties.
**Remove draft** removes only the displayed draft. Selecting an existing catalog
record keeps pending drafts in the list. An empty draft without a selected file is
not uploaded.

All prepared files are uploaded when the workflow is queued, and all new records
are committed together when this node executes, regardless of **Save changes**.
No records are added while filling out the editor. If a file copy or JSON write
fails, the entire addition is rolled back and can be retried. An unconnected index
outputs the displayed new image on its first run; otherwise `image_index` selects
the output. Subsequent batch items follow the index mode. The node still outputs
one image per execution. Uploads are limited to 64 MiB per file and converted to
PNG. Repeating a queued addition does not duplicate records.

**Delete catalog** asks for confirmation and permanently deletes the catalog JSON,
its images, and pending uploads belonging to it. There is no undo. Other catalogs
are not affected.

**Refresh catalogs** updates the catalog list. **Reload catalog** discards pending
local edits after confirmation and reloads the stored records. Switching catalogs
starts a fresh editor. Changing the schema of the displayed outputs disconnects
links whose slot name or type no longer matches.

## Catalog fields

**Edit Catalog** opens a separate editor for the selected catalog's schema.
Use **Add property** to append a field, **Remove property** to remove one, and
**Hidden** to hide a field from image forms. Up to 32 fields are supported,
including hidden fields. Existing field names and types are read-only; create a
new field when a different name or type is needed.

**Save Catalog** applies the schema immediately, without running the workflow.
New fields receive their type's default value in every saved record and pending
draft. Removing a field asks for confirmation and permanently removes its values
from all records. **Cancel** discards the schema edits.

Hidden fields retain their stored values and named outputs. Clear **Hidden** in
the editor to show them again. Removing a field removes its output; connections
to the remaining fields are preserved when their slots shift. Other nodes in the
same browser using that catalog update their schema too. Stale schema editors and
queued prompts are rejected if the catalog schema changed.

## Export and import

Catalog actions are grouped below a horizontal divider under the image form.
**Export Catalog** downloads a ZIP containing `catalog.json` and the catalog's PNG
files at the archive root. This includes hidden records, hidden field values, the
schema, original display filenames, and image ordering. Export uses saved data;
pending property edits and local image drafts must be saved first to include them.

**Import Catalog** is also available before selecting or creating a catalog.
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

Uploaded staging files are removed after a successful addition. Abandoned uploads
expire after seven days and are cleaned during later uploads. If a workflow fails
before this node executes, no new record is committed. Changes already committed
by this node remain saved if a downstream node subsequently fails.

Workflow JSON preserves the selected catalog, schema/output layout, index mode,
selected record, pending image drafts, and property edits. It does not embed catalog files or
image bytes. Back up the storage directory separately. An unsaved local file must
be selected again for each draft after restoring the workflow. Already saved images load directly
from the server. Loading a workflow after its pending upload was committed resolves
the existing image instead of adding it again.

Existing catalogs also work through ComfyUI's API with a serialized `catalog_state`.
Automatic index advancement and local-file uploads are browser features; API
clients supply indices and upload files explicitly. The node is an output node,
so saving works even if its outputs are not connected.

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
