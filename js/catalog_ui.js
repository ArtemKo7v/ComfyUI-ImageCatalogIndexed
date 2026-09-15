import { MAX_PROPERTIES, PROPERTY_TYPES } from "./catalog_model.js";

/** Create a DOM element with optional class and text. */
export function element(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

/** Create a button with a click handler. */
export function button(text, action) {
  const control = element("button", "", text);
  control.type = "button";
  control.addEventListener("click", action);
  return control;
}

/** Create a select control from value-label option pairs. */
export function select(options, value, onChange) {
  const control = element("select");
  for (const [key, text] of options) {
    const option = element("option", "", text);
    option.value = key;
    control.append(option);
  }
  control.value = value;
  control.addEventListener("change", () => onChange(control.value));
  return control;
}

/** Wrap a control in an accessible labelled field. */
export function label(text, control) {
  const row = element("label", "ic-field");
  control.setAttribute("aria-label", text);
  row.append(element("span", "", text), control);
  return row;
}

/** Create a labelled checkbox with a boolean callback. */
export function checkbox(text, value, onChange) {
  const control = element("input");
  control.type = "checkbox";
  control.checked = value;
  control.addEventListener("change", () => onChange(control.checked));
  return label(text, control);
}

/** Install the shared catalog-editor stylesheet once per page. */
export function installStyles() {
  // Styles are shared by all node instances; install them once.
  if (document.getElementById("image-catalog-styles")) return;
  const style = element("style");
  style.id = "image-catalog-styles";
  style.textContent = `
    .image-catalog { box-sizing: border-box; padding: 10px; color: var(--input-text, #ddd);
      background: var(--comfy-menu-bg, #252525); font: 13px sans-serif; overflow: auto; }
    .image-catalog *, .image-catalog *::before { box-sizing: border-box; }
    .image-catalog input:not([type=checkbox]), .image-catalog select, .image-catalog textarea {
      width: 100%; min-width: 0; padding: 6px; border: 1px solid var(--border-color, #555);
      border-radius: 4px; color: inherit; background: var(--comfy-input-bg, #333); }
    .image-catalog button { cursor: pointer; padding: 7px 10px; border: 1px solid #666;
      border-radius: 4px; color: inherit; background: var(--comfy-input-bg, #383838); }
    .image-catalog button:disabled { opacity: .5; cursor: default; }
    .image-catalog .ic-field { display: flex; flex-direction: column; gap: 4px; margin-bottom: 9px; }
    .image-catalog .ic-field:has(input[type=checkbox]) { flex-direction: row; align-items: center; justify-content: space-between; }
    .image-catalog .ic-row { display: flex; gap: 6px; margin: 8px 0; }
    .image-catalog .ic-row > input { flex: 1; }
    .image-catalog .ic-row > select { width: 100px; flex: 0 0 100px; }
    .image-catalog .ic-preview { display: block; width: 100%; height: 190px; object-fit: contain; background: #181818; margin: 8px 0; }
    .image-catalog .ic-record { padding: 6px; border-radius: 5px; }
    .image-catalog .ic-record.is-hidden { opacity: .45; }
    .image-catalog .ic-record.is-deleted { background: #702626; opacity: .5; }
    .image-catalog .ic-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
    .image-catalog .ic-danger { border-color: #b65e5e; color: #fff; background: #832e2e; }
    .image-catalog .ic-block { border: 1px solid var(--border-color, #555); border-radius: 6px; padding: 10px; }
    .image-catalog .ic-image-block { margin-top: 12px; }
    .image-catalog .ic-selector-row { display: flex; align-items: end; gap: 8px; }
    .image-catalog .ic-selector-row > .ic-field { flex: 1; min-width: 0; margin: 0; }
    .image-catalog .ic-delete-actions { justify-content: flex-end; }
    .ic-export-dialog { color: #ddd; background: #252525; border: 1px solid #666; border-radius: 8px; max-width: 440px; padding: 20px; }
    .ic-export-dialog::backdrop { background: #0008; }
    .ic-export-dialog button { margin: 8px 8px 0 0; padding: 8px; cursor: pointer; }
    .image-catalog .ic-status { white-space: pre-wrap; margin: 8px 0; color: #b8c3ce; }
    .image-catalog .ic-status.is-error { color: #ffaaaa; }
    .image-catalog textarea { resize: vertical; min-height: 70px; }
    .image-catalog .ic-help { color: #aaa; font-size: 12px; margin: 8px 0; }
    .image-catalog .ic-divider { border: 0; border-top: 1px solid var(--border-color, #555); margin: 16px 0 10px; }
    .image-catalog .ic-schema-field { border: 1px solid var(--border-color, #555); border-radius: 5px; padding: 8px; margin: 8px 0; }
  `;
  document.head.append(style);
}

/** Render catalog creation controls and the initial schema rows. */
export function renderCreate(controller, root = controller.root) {
  // Keep one blank schema row ready until the property limit is reached.
  const { state } = controller;
  const name = element("input");
  name.placeholder = "Catalog name";
  name.maxLength = 120;
  name.value = state.createName;
  name.addEventListener("input", () => { state.createName = name.value; controller.persist(); });
  root.append(label("Catalog name", name));
  const rows = element("div");
  const renderRow = (field, index) => {
    const row = element("div", "ic-row");
    const input = element("input");
    input.placeholder = "Property name";
    input.maxLength = 80;
    input.value = field.name;
    input.addEventListener("input", () => {
      field.name = input.value;
      if (input.value.trim() && index === state.createSchema.length - 1 && index + 1 < MAX_PROPERTIES) {
        const next = { name: "", type: "String" };
        state.createSchema.push(next);
        renderRow(next, index + 1);
      }
      controller.persist();
    });
    row.append(input, select(Object.keys(PROPERTY_TYPES).map((type) => [type, type]), field.type,
      (type) => { field.type = type; controller.persist(); }));
    rows.append(row);
  };
  state.createSchema.forEach(renderRow);
  root.append(rows, button("Create Now", (event) => controller.run(async () => {
    event.target.disabled = true;
    try { await controller.createCatalog(); }
    finally { event.target.disabled = false; }
  })));
}

/** Render the selected catalog record or image draft. */
export function renderCatalog(controller, root = controller.root) {
  const { state } = controller;
  const catalog = controller.workingCatalog();
  const storedEntries = catalog.entries.filter((entry) => !state.newImages.some((draft) => draft.token === entry.id));
  const entry = catalog.entries.find((item) => item.id === state.selectedId);
  if (catalog.entries.length || state.newImages.length) {
    const options = storedEntries.map((item) => [item.id, `${item.filename}${item.hidden ? " (hidden)" : ""} [${item.id.slice(0, 6)}]`]);
    options.push(...state.newImages.map((image, index) => [`new:${image.token}`, `New ${index + 1}: ${image.filename || "Select a file"}`]));
    const imageSelect = select(options, state.adding ? `new:${state.draftToken}` : state.selectedId,
      (id) => id.startsWith("new:") ? controller.selectDraft(id.slice(4)) : controller.selectEntry(id));
    root.append(label("Image", imageSelect));
  }
  if (state.newImages.some((image) => image.filename)) {
    root.append(element("p", "ic-help", `${state.newImages.filter((image) => image.filename).length} new image(s). Workflow execution uses local changes; Save Changes persists them.`));
  }
  if (controller.connected) root.append(element("p", "ic-help", "The connected image_index selects the workflow output. Edits belong to the displayed record."));
  if (state.adding) {
    const file = element("input");
    file.type = "file";
    file.accept = "image/*";
    file.disabled = Boolean(controller.pendingNewToken);
    file.addEventListener("change", () => controller.run(() => controller.chooseFile(file.files[0])));
    root.append(label("Add image", file));
    if (controller.newImage?.filename) root.append(element("p", "ic-help", controller.newImage.filename));
    if (controller.newImage?.filename && !controller.files.has(controller.newImage.token)) {
      root.append(element("p", "ic-help", "Select the local file again after restoring a workflow."));
    }
  }
  // Existing entries use a local overlay, so unsaved edits do not mutate
  // the fetched catalog object.
  const edit = entry ? state.edits[entry.id] : null;
  const hidden = !state.adding && (edit?.hidden ?? entry?.hidden);
  const deleted = !state.adding && edit?.delete;
  const record = element("div", "ic-record" + (deleted ? " is-deleted" : hidden ? " is-hidden" : ""));
  const source = state.adding ? controller.previewUrl : entry ? controller.imageUrl(entry.id) : null;
  if (source) {
    const preview = element("img", "ic-preview");
    preview.src = source;
    preview.alt = state.adding ? controller.newImage.filename : entry.filename;
    preview.addEventListener("error", () => controller.status("Image preview is unavailable.", true));
    record.append(preview);
  }
  const values = state.adding ? controller.newImage?.values : edit?.values || entry?.values;
  if (values) {
    for (const field of state.schema.filter((field) => !field.hidden)) {
      const input = element(field.type === "Text" ? "textarea" : "input");
      if (field.type === "Boolean") {
        input.type = "checkbox";
        input.checked = values[field.name];
      } else {
        if (field.type === "Integer") {
          input.type = "number"; input.step = "1";
          input.min = String(-Number.MAX_SAFE_INTEGER); input.max = String(Number.MAX_SAFE_INTEGER);
        }
        input.value = values[field.name];
      }
      input.disabled = Boolean(hidden || deleted || (state.adding && controller.pendingNewToken));
      input.addEventListener("input", () => {
        const target = state.adding ? controller.newImage.values : controller.editEntry(entry).values;
        target[field.name] = field.type === "Boolean" ? input.checked : field.type === "Integer" ? input.valueAsNumber : input.value;
        controller.touch();
      });
      record.append(label(field.name, input));
    }
  }
  root.append(record);
  if (!state.adding && entry) {
    root.append(checkbox("Hide", Boolean(hidden), (value) => { controller.editEntry(entry).hidden = value; controller.touch(); controller.render(); }));
  }
  const deleteActions = element("div", "ic-actions ic-delete-actions");
  const remove = button("Delete", () => controller.deleteImage());
  remove.className = "ic-danger";
  remove.disabled = !entry && !controller.newImage;
  deleteActions.append(remove);
  const actions = element("div", "ic-actions ic-image-actions");
  actions.append(button("Add Image", () => controller.startAdding()),
    button("Save Changes", () => controller.run(() => controller.saveChanges())));
  root.append(deleteActions, element("hr", "ic-divider"), actions);
  if (!catalog.entries.some((item) => !item.hidden) && !state.adding) {
    root.append(element("p", "ic-help", "No visible images. Unhide a record or add an image. Downstream nodes will be skipped."));
  }
}

/** Render the schema editor for the active catalog. */
export function renderCatalogEditor(controller, root = controller.root) {
  // Existing names and types are immutable because values and output sockets
  // already depend on them; only new fields remain editable.
  const { state } = controller;
  const editor = state.catalogEdit;
  root.append(element("p", "", "Edit Catalog"), element("p", "ic-help",
    "Hidden properties keep their values and outputs. Apply Fields updates the local catalog. Save Changes persists all fields and images to the server."));
  editor.fields.forEach((field, index) => {
    const card = element("div", "ic-schema-field");
    const row = element("div", "ic-row");
    const name = element("input");
    name.placeholder = "Property name";
    name.setAttribute("aria-label", `Property ${index + 1} name`);
    name.maxLength = 80;
    name.value = field.name;
    name.disabled = field.existing;
    name.addEventListener("input", () => { field.name = name.value; controller.persist(); });
    const type = select(Object.keys(PROPERTY_TYPES).map((kind) => [kind, kind]), field.type,
      (value) => { field.type = value; controller.persist(); });
    type.setAttribute("aria-label", `Property ${index + 1} type`);
    type.disabled = field.existing;
    row.append(name, type);
    card.append(row, checkbox("Hidden", Boolean(field.hidden), (value) => { field.hidden = value; controller.persist(); }),
      button("Remove property", () => { editor.fields.splice(index, 1); controller.persist(); controller.render(); }));
    root.append(card);
  });
  const add = button("Add property", () => {
    editor.fields.push({ name: "", type: "String", existing: false });
    controller.persist();
    controller.render();
  });
  add.disabled = editor.fields.length >= MAX_PROPERTIES;
  const save = button("Apply Fields", (event) => controller.run(async () => {
    event.target.disabled = true;
    try { await controller.saveCatalogSchema(); }
    finally { event.target.disabled = false; }
  }));
  const actions = element("div", "ic-actions");
  actions.append(add, save, button("Cancel", () => {
    state.catalogEdit = null;
    controller.persist();
    controller.render();
  }));
  root.append(actions);
}

/** Ask which persisted version to export without conflating discard and cancel. */
export function exportChoice() {
  return new Promise((resolve) => {
    const dialog = element("dialog", "ic-export-dialog");
    dialog.setAttribute("aria-label", "Export unsaved catalog");
    dialog.append(element("p", "", "This catalog has unsaved changes. Save all changes for all images before exporting?"));
    for (const [value, title] of [["save", "Save and Export"], ["saved", "Export Saved Version"], ["cancel", "Cancel"]]) {
      dialog.append(button(title, () => dialog.close(value)));
    }
    dialog.addEventListener("close", () => { resolve(dialog.returnValue || "cancel"); dialog.remove(); }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
}
