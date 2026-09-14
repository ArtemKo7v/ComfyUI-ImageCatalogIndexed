import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { defaultValues, executionEntries, identifier, migrateValues, nextIndex, outputDefinitions, wrapIndex } from "./catalog_model.js";
import { button, element, installStyles, label, renderCatalog, renderCatalogEditor, renderCreate, select } from "./catalog_ui.js";

const NODE_CLASS = "ArtemKo7vImageCatalogIndexed";
const PREFIX = "/artemko7v/image-catalog";
const controllers = new Map();

/** Send a catalog API request and return decoded successful JSON. */
async function request(path, options = {}) {
  // Every UI action receives the API's own error message through this helper.
  const response = await api.fetchApi(PREFIX + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Catalog request failed (${response.status}).`);
  return data;
}

/** Build JSON request options for the catalog API. */
function jsonRequest(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

/** Own UI, local state, and queue behavior for one node. */
export class CatalogController {
/** Attach the catalog editor and widgets to a ComfyUI node. */
  constructor(node) {
    this.node = node;
    this.id = identifier();
    this.catalog = null;
    this.catalogs = [];
    // Local files and upload promises cannot be serialized into workflow JSON.
    this.files = new Map();
    this.uploads = new Map();
    this.queuedEdits = new Map();
    this.loadSequence = 0;
    this.state = this.freshState();
    this.root = element("div", "image-catalog");
    for (const event of ["pointerdown", "keydown", "wheel"]) {
      this.root.addEventListener(event, (value) => value.stopPropagation());
    }
    this.domWidget = node.addDOMWidget("catalog_editor", "image_catalog", this.root, {
      serialize: false, hideOnZoom: true,
      getMinHeight: () => 420,
    });
    this.stateWidget = node.widgets.find((widget) => widget.name === "catalog_state");
    this.stateWidget.serializeValue = () => JSON.stringify(this.snapshot());
    this.indexWidget = node.widgets.find((widget) => widget.name === "image_index");
    this.indexModeWidget = node.addWidget("combo", "index_after_queue", this.state.indexMode, (mode) => {
      this.state.indexMode = mode;
      this.persist();
    }, { values: ["fixed", "increment", "decrement", "randomize"], serialize: false, socketless: true });
    this.indexModeWidget.label = "Index after queue";
    node.widgets.splice(node.widgets.indexOf(this.indexModeWidget), 1);
    node.widgets.splice(node.widgets.indexOf(this.indexWidget) + 1, 0, this.indexModeWidget);
    this.indexWidget.linkedWidgets = [...(this.indexWidget.linkedWidgets || []), this.indexModeWidget];
    Object.defineProperty(this.indexModeWidget, "disabled", { get: () => this.connected });
    const previous = this.indexWidget.callback;
    this.indexWidget.callback = (...args) => {
      previous?.apply(this.indexWidget, args);
      if (this.catalog && !this.state.adding) this.selectIndex();
      this.persist();
    };
    controllers.set(this.id, this);
    this.render();
    this.run(() => this.refreshList());
  }

/** Create the default serializable controller state. */
  freshState() {
    return { catalogId: "", schema: [], catalogName: "", selectedId: "", edits: {}, saveChanges: false,
      indexMode: this.indexModeWidget?.value || "fixed", adding: false, newImages: [], draftToken: "", newSelection: true, operationId: identifier(),
      createName: "", createSchema: [{ name: "", type: "String" }], catalogEdit: null };
  }

/** Report whether the image index is graph-controlled. */
  get connected() {
    return this.node.inputs?.some((input) => input.name === "image_index" && input.link != null) || false;
  }

/** Return the currently selected draft image. */
  get newImage() {
    return this.state.newImages.find((image) => image.token === this.state.draftToken);
  }

/** Persist editor state to the workflow and hidden execution widget. */
  persist() {
    // Keep node properties and the hidden backend widget on the same snapshot.
    this.node.properties ||= {};
    this.node.properties.imageCatalog = structuredClone(this.state);
    this.stateWidget.value = JSON.stringify(this.snapshot());
    this.node.setDirtyCanvas(true, true);
  }

/** Record a new local change with a fresh operation identifier. */
  touch() {
    // A new ID prevents a retry of an older prompt from acknowledging newer work.
    this.state.operationId = identifier();
    this.persist();
  }

/** Create the serializable state submitted with a queued prompt. */
  snapshot() {
    // Empty draft rows are a UI aid; only filename-bearing drafts can execute.
    return { catalog_id: this.state.catalogId, schema: this.state.schema, edits: this.state.edits,
      schema_revision: this.catalog?.schema_revision ?? 0,
      save_changes: this.state.saveChanges, operation_id: this.state.operationId, instance_id: this.id,
      index_connected: this.connected, index_mode: this.state.indexMode,
      new_images: this.state.newImages.filter((image) => image.filename),
      new_selection: this.state.adding && this.state.newSelection ? this.newImage?.token : null };
  }

/** Restore saved workflow state and reload server catalog data. */
  async restore() {
    // Accept the earlier single-draft workflow format before reloading catalog data.
    const saved = this.node.properties?.imageCatalog;
    if (saved) this.state = { ...this.freshState(), ...structuredClone(saved) };
    if (saved?.newImage && !saved.newImages) {
      this.state.newImages = [saved.newImage];
      this.state.draftToken = saved.newImage.token;
    }
    delete this.state.newImage;
    this.indexModeWidget.value = this.state.indexMode;
    this.syncOutputs();
    this.render();
    await this.refreshList();
    if (this.state.catalogId) await this.loadCatalog(this.state.catalogId, true);
  }

/** Run a UI action and present any thrown error. */
  async run(action) {
    try { await action(); }
    catch (error) {
      this.status(error.message || String(error), true);
      console.error("Image Catalog Indexed:", error);
    }
  }

/** Update the editor status message and its severity. */
  status(message, isError = false) {
    this.message = message;
    this.isError = isError;
    if (this.statusElement) {
      this.statusElement.textContent = message;
      this.statusElement.classList.toggle("is-error", isError);
    }
  }

/** Build a preview URL for a stored catalog image. */
  imageUrl(entryId) {
    return api.apiURL(`${PREFIX}/catalogs/${this.state.catalogId}/images/${entryId}`);
  }

/** Reload catalog summaries for the selector. */
  async refreshList() {
    const result = await request("/catalogs");
    this.catalogs = result.catalogs;
    this.render();
    if (result.errors.length) this.status(result.errors.map((item) => `${item.id}: ${item.error}`).join("\n"), true);
  }

/** Load a catalog and reconcile local editor state. */
  async loadCatalog(catalogId, preserve = false) {
    const sequence = ++this.loadSequence;
    if (!catalogId) {
      this.releasePreview();
      this.pendingNewToken = null;
      this.state = this.freshState();
      this.catalog = null;
      this.syncOutputs();
      this.persist();
      this.render();
      return;
    }
    const catalog = await request(`/catalogs/${catalogId}`);
    if (sequence !== this.loadSequence || this.disposed) return;
    if (!preserve) {
      this.releasePreview();
      this.pendingNewToken = null;
      this.state = this.freshState();
      this.indexWidget.value = 0;
    }
    this.catalog = catalog;
    this.state.catalogId = catalog.id;
    this.state.catalogName = catalog.name;
    this.migrateDraftValues(catalog.schema);
    this.state.schema = catalog.schema;
    this.state.newImages = this.state.newImages.filter((image) => !catalog.entries.some((entry) => entry.id === image.token));
    if (!this.newImage) this.state.draftToken = this.state.newImages[0]?.token || "";
    this.state.adding = this.state.adding && Boolean(this.newImage);
    if (!catalog.entries.length && !this.state.newImages.length) this.startAdding(false);
    else if (!catalog.entries.some((entry) => entry.id === this.state.selectedId)) this.selectIndex(false);
    this.syncOutputs();
    this.persist();
    this.message = "";
    this.render();
  }

/** Create a catalog and refresh every node selector. */
  async createCatalog() {
    const catalog = await request("/catalogs", jsonRequest("POST", {
      name: this.state.createName,
      schema: this.state.createSchema.filter((field) => field.name.trim()).map((field) => ({ ...field, name: field.name.trim() })),
    }));
    await this.loadCatalog(catalog.id);
    await Promise.all([...controllers.values()].map((controller) => controller.refreshList()));
  }

/** Migrate unsaved drafts and edits to a changed schema. */
  migrateDraftValues(schema) {
    for (const draft of this.state.newImages) draft.values = migrateValues(schema, draft.values);
    for (const edit of Object.values(this.state.edits)) edit.values = migrateValues(schema, edit.values);
  }

/** Open a revision-aware schema editor. */
  async editCatalog() {
    if (!this.catalog) return;
    const catalogId = this.state.catalogId;
    const catalog = await request(`/catalogs/${catalogId}`);
    if (this.state.catalogId !== catalogId) return;
    this.state.catalogEdit = { catalogId, revision: catalog.schema_revision ?? 0,
      original: structuredClone(catalog.schema),
      fields: catalog.schema.map((field) => ({ ...field, existing: true })) };
    this.persist();
    this.render();
  }

/** Save schema changes and update related node instances. */
  async saveCatalogSchema() {
    const editor = this.state.catalogEdit;
    const schema = editor.fields.filter((field) => field.name.trim()).map((field) => ({
      name: field.name.trim(), type: field.type, ...(field.hidden ? { hidden: true } : {}),
    }));
    const removed = editor.original.filter((field) => !schema.some((next) => next.name === field.name));
    if (removed.length && !window.confirm(`Remove properties ${removed.map((field) => `"${field.name}"`).join(", ")} and their values from every image?`)) return;
    const catalog = await request(`/catalogs/${editor.catalogId}/schema`, jsonRequest("PATCH", {
      schema, expected_revision: editor.revision,
    }));
    for (const controller of controllers.values()) {
      if (controller.state.catalogId !== catalog.id) continue;
      const previousRevision = controller.catalog?.schema_revision ?? 0;
      if (previousRevision === catalog.schema_revision) continue;
      // Rebase only edits whose record was changed solely by this schema update.
      for (const [id, edit] of Object.entries(controller.state.edits)) {
        const saved = catalog.entries.find((entry) => entry.id === id);
        if (previousRevision === editor.revision && saved?.revision === edit.revision + 1) edit.revision = saved.revision;
      }
      controller.migrateDraftValues(catalog.schema);
      controller.catalog = catalog;
      controller.state.schema = catalog.schema;
      controller.syncOutputs();
      controller.touch();
      controller.render();
    }
    if (this.state.catalogEdit === editor) {
      this.state.catalogEdit = null;
      this.persist();
      this.render();
      this.status("Catalog fields saved.");
    }
  }

/** Download the active catalog as a ZIP archive. */
  async exportCatalog() {
    const catalogId = this.state.catalogId;
    const catalogName = this.state.catalogName;
    const response = await api.fetchApi(`${PREFIX}/catalogs/${catalogId}/export`);
    if (!response.ok) throw new Error((await response.json()).error || "Catalog export failed.");
    const url = URL.createObjectURL(await response.blob());
    const download = element("a");
    download.href = url;
    download.download = `${catalogName || "image-catalog"}.zip`;
    document.body.append(download);
    download.click();
    download.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    this.status("Saved catalog exported as ZIP.");
  }

/** Import an archive and select the new catalog. */
  async importCatalog(file) {
    if (!file) return;
    if (file.size > 512 * 1024 * 1024) throw new Error("Catalog ZIP uploads cannot exceed 512 MiB.");
    const body = new FormData();
    body.append("archive", file);
    this.status("Importing catalog...");
    const catalog = await request("/import", { method: "POST", body });
    await this.loadCatalog(catalog.id);
    await Promise.all([...controllers.values()].map((controller) => controller.refreshList()));
    this.status("Catalog imported.");
  }

/** Keep node output slots aligned with the current catalog schema. */
  syncOutputs() {
    // Preserve matching links and remove only output slots whose identity changed.
    const desired = outputDefinitions(this.state.schema);
    for (let index = this.node.outputs.length - 1; index >= 0; index--) {
      const current = this.node.outputs[index];
      if (!desired.some((output) => output.name === current.name && output.type === current.type)) this.node.removeOutput(index);
    }
    desired.forEach((output, index) => {
      const current = this.node.outputs[index];
      if (!current) this.node.addOutput(output.name, output.type);
      else if (current.name !== output.name || current.type !== output.type) {
        this.node.disconnectOutput(index);
        current.name = output.name;
        current.type = output.type;
      }
    });
  }

/** Select the record at the visible image index. */
  selectIndex(render = true) {
    const entries = this.catalog?.entries.filter((entry) => !entry.hidden) || [];
    const index = wrapIndex(Number(this.indexWidget.value) || 0, entries.length);
    this.indexWidget.value = index;
    this.state.selectedId = entries[index]?.id || this.catalog?.entries[0]?.id || "";
    if (render) this.render();
  }

/** Select an existing catalog entry. */
  selectEntry(id) {
    this.releasePreview();
    this.state.adding = false;
    this.state.selectedId = id;
    const index = this.catalog.entries.filter((entry) => !entry.hidden).findIndex((entry) => entry.id === id);
    if (index >= 0 && !this.connected) this.indexWidget.value = index;
    this.persist();
    this.render();
  }

/** Open or create an image draft. */
  startAdding(render = true) {
    if (this.pendingNewToken) return;
    this.releasePreview();
    this.state.adding = true;
    this.state.newSelection = true;
    const empty = this.state.newImages.find((image) => !image.filename);
    const draft = empty || { token: identifier(), filename: "", values: defaultValues(this.state.schema) };
    if (!empty) this.state.newImages.push(draft);
    this.state.draftToken = draft.token;
    this.touch();
    if (render) this.render();
  }

/** Select an unsaved image draft. */
  selectDraft(token) {
    this.state.draftToken = token;
    this.state.adding = true;
    this.state.newSelection = true;
    this.releasePreview();
    const file = this.files.get(token);
    if (file) this.previewUrl = URL.createObjectURL(file);
    this.persist();
    this.render();
  }

/** Remove the selected draft and its local file state. */
  removeDraft() {
    if (this.pendingNewToken) return;
    const token = this.state.draftToken;
    this.state.newImages = this.state.newImages.filter((image) => image.token !== token);
    this.files.delete(token);
    this.uploads.delete(token);
    this.releasePreview();
    this.touch();
    if (this.state.newImages.length) this.selectDraft(this.state.newImages[0].token);
    else if (!this.catalog.entries.length) this.startAdding();
    else {
      this.state.adding = false;
      this.state.draftToken = "";
      this.selectIndex();
      this.persist();
    }
  }

/** Return the local mutable edit overlay for an entry. */
  editEntry(entry) {
    this.state.edits[entry.id] ||= { values: { ...entry.values }, hidden: entry.hidden, delete: false, revision: entry.revision };
    return this.state.edits[entry.id];
  }

/** Release the object URL for a local image preview. */
  releasePreview() {
    if (this.previewUrl) URL.revokeObjectURL(this.previewUrl);
    this.previewUrl = null;
  }

/** Attach a local file to the active draft. */
  async chooseFile(file) {
    if (!file) return;
    if (file.size > 64 * 1024 * 1024) throw new Error("Image uploads cannot exceed 64 MiB.");
    this.releasePreview();
    const token = identifier();
    this.files.set(token, file);
    const draft = this.newImage;
    this.files.delete(draft.token);
    this.uploads.delete(draft.token);
    draft.token = token;
    draft.filename = file.name;
    this.state.draftToken = token;
    this.state.newSelection = true;
    this.previewUrl = URL.createObjectURL(file);
    this.touch();
    this.render();
  }

/** Render the complete catalog editor and its actions. */
  render() {
    if (this.disposed) return;
    this.root.replaceChildren();
    const options = [["", "Create a new Image Catalog"], ...this.catalogs.map((item) => [item.id, item.name])];
    if (this.state.catalogId && !this.catalogs.some((item) => item.id === this.state.catalogId)) {
      options.push([this.state.catalogId, this.state.catalogName || "Unavailable catalog"]);
    }
    this.root.append(label("Catalog", select(options, this.state.catalogId, (id) => this.run(() => this.loadCatalog(id)))));
    this.statusElement = element("div", "ic-status" + (this.isError ? " is-error" : ""), this.message || "");
    this.root.append(this.statusElement);
    if (!this.state.catalogId) renderCreate(this);
    else if (this.catalog && this.state.catalogEdit) renderCatalogEditor(this);
    else if (this.catalog) renderCatalog(this);
    else this.root.append(element("p", "ic-help", "Loading catalog..."));
    this.root.append(element("hr", "ic-divider"));
    const actions = element("div", "ic-actions ic-catalog-actions");
    actions.append(button("Refresh catalogs", () => this.run(() => this.refreshList())));
    const archive = element("input");
    archive.type = "file";
    archive.accept = ".zip,application/zip";
    archive.hidden = true;
    archive.setAttribute("aria-label", "Import catalog ZIP");
    archive.addEventListener("change", () => {
      const file = archive.files[0];
      archive.value = "";
      this.run(() => this.importCatalog(file));
    });
    actions.append(button("Import Catalog", () => archive.click()), archive);
    if (this.state.catalogId) {
      actions.append(button("Edit Catalog", () => this.run(() => this.editCatalog())),
        button("Export Catalog", () => this.run(() => this.exportCatalog())));
      actions.append(button("Reload catalog", () => this.run(async () => {
        if ((Object.keys(this.state.edits).length || this.state.newImages.length) &&
          !window.confirm("Discard pending edits and reload this catalog?")) return;
        await this.loadCatalog(this.state.catalogId);
      })));
      const remove = button("Delete catalog", () => this.run(async () => {
        const { catalogId, catalogName } = this.state;
        if (!window.confirm(`Delete catalog "${catalogName}" and all its images? This cannot be undone.`)) return;
        await request(`/catalogs/${catalogId}`, jsonRequest("DELETE", { confirm: catalogId }));
        for (const controller of controllers.values()) {
          if (controller.state.catalogId === catalogId) await controller.loadCatalog("");
          await controller.refreshList();
        }
      }));
      remove.className = "ic-danger";
      actions.append(remove);
    }
    this.root.append(actions);
  }

/** Validate queued state and stage each local image upload. */
  async prepareQueue(state) {
    // Validate browser-only values and stage every local file before queuing.
    if (!this.catalog || this.catalog.id !== state.catalog_id) throw new Error("Wait for the image catalog to load before running.");
    if (!state.new_images.length && !this.catalog.entries.length) throw new Error("Select at least one image before running.");
    for (const values of [...Object.values(state.edits).map((edit) => edit.values), ...state.new_images.map((image) => image.values)]) {
      for (const field of state.schema) {
        if (field.type === "Integer" && !Number.isSafeInteger(values[field.name])) {
          throw new Error(`Enter a valid integer for '${field.name}'.`);
        }
      }
    }
    for (const image of state.new_images) {
      const { token } = image;
      const file = this.files.get(token);
      if (!file) throw new Error(`Select '${image.filename}' again before running. Local files cannot be restored from workflow JSON.`);
    }
    for (const { token } of state.new_images) {
      const file = this.files.get(token);
      if (!this.uploads.has(token)) {
        const body = new FormData();
        body.append("image", file);
        const upload = request(`/catalogs/${state.catalog_id}/uploads/${token}`, { method: "POST", body });
        this.uploads.set(token, upload);
        upload.catch(() => this.uploads.delete(token));
      }
      await this.uploads.get(token);
    }
    this.queuedEdits.set(state.operation_id, structuredClone(state.edits));
  }

/** Update local state after prompt acceptance. */
  onQueued(snapshot, index) {
    // Advance the local index with the same visible-entry rules as the backend.
    if (this.disposed || snapshot.catalog_id !== this.state.catalogId) return;
    if (snapshot.new_images.some((image) => this.state.newImages.some((draft) => draft.token === image.token))) {
      this.pendingNewToken = snapshot.new_images[0].token;
      this.render();
    }
    if (snapshot.index_connected) return;
    const entries = executionEntries(this.catalog, snapshot.edits, snapshot.save_changes, snapshot.new_images);
    const selectedNew = entries.findIndex((entry) => entry.id === snapshot.new_selection);
    const selected = selectedNew >= 0 ? selectedNew : index;
    this.indexWidget.value = nextIndex(selected, entries.length, snapshot.index_mode);
    this.state.newSelection = false;
    if (!this.state.adding) {
      this.state.selectedId = entries[this.indexWidget.value]?.id || this.state.selectedId;
      this.render();
    }
    this.persist();
  }

/** Reconcile execution results with local drafts and edits. */
  onExecuted(result) {
    // Keep newer local edits when a result belongs to another node instance.
    if (result.catalog.id !== this.state.catalogId) return;
    if ((result.catalog.schema_revision ?? 0) < (this.catalog?.schema_revision ?? 0)) return;
    this.catalog = result.catalog;
    const acknowledged = result.operation_id === this.state.operationId;
    if (acknowledged && result.saved) this.state.edits = {};
    else if (result.saved) {
      const submitted = this.queuedEdits.get(result.operation_id) || {};
      for (const [id, edit] of Object.entries(this.state.edits)) {
        const previous = submitted[id];
        if (!previous) continue;
        if (JSON.stringify(edit) === JSON.stringify(previous)) delete this.state.edits[id];
        else {
          const saved = result.catalog.entries.find((entry) => entry.id === id);
          if (saved && edit.revision === previous.revision && saved.revision === previous.revision + 1) edit.revision = saved.revision;
        }
      }
    }
    this.queuedEdits.delete(result.operation_id);
    const addedIds = new Set(result.added_ids || (result.added_id ? [result.added_id] : []));
    for (const draft of this.state.newImages.filter((image) => addedIds.has(image.token))) {
      const added = result.catalog.entries.find((entry) => entry.id === draft.token);
      if (!acknowledged && added && JSON.stringify(added.values) !== JSON.stringify(draft.values)) {
        this.state.edits[added.id] = { values: { ...draft.values }, hidden: false, delete: false, revision: added.revision };
        this.state.saveChanges = true;
      }
    }
    this.state.newImages = this.state.newImages.filter((image) => !addedIds.has(image.token));
    if (addedIds.has(this.state.draftToken)) {
      this.releasePreview();
      this.state.draftToken = this.state.newImages[0]?.token || "";
      this.state.adding = Boolean(this.newImage);
      const file = this.files.get(this.state.draftToken);
      if (file) this.previewUrl = URL.createObjectURL(file);
    }
    if (addedIds.has(this.pendingNewToken)) this.pendingNewToken = null;
    if (!this.state.adding) {
      if (this.connected && result.selected_id) this.state.selectedId = result.selected_id;
      else this.selectIndex(false);
    }
    this.persist();
    this.render();
    this.status(result.warnings?.length ? result.warnings.join("\n") :
      result.selected_id ? "Catalog execution completed." : "No visible images. Downstream nodes were skipped.", Boolean(result.warnings?.length));
  }

/** Release DOM and in-memory resources for a removed node. */
  dispose() {
    this.disposed = true;
    this.loadSequence++;
    this.releasePreview();
    this.files.clear();
    controllers.delete(this.id);
    this.root.remove();
  }
}

/** Install pre-queue upload staging for catalog nodes. */
function installQueueHook() {
  // Stage catalog files before ComfyUI accepts the prompt, avoiding partial runs.
  const original = api.queuePrompt;
  api.queuePrompt = async function (number, prompt, ...rest) {
    const queued = [];
    for (const definition of Object.values(prompt.output || {})) {
      if (definition.class_type !== NODE_CLASS) continue;
      const state = JSON.parse(definition.inputs.catalog_state);
      const controller = controllers.get(state.instance_id);
      if (!controller) throw new Error("Image catalog editor is unavailable. Reload the workflow.");
      try { await controller.prepareQueue(state); }
      catch (error) { controller.status(error.message, true); throw error; }
      queued.push({ controller, state, index: definition.inputs.image_index });
    }
    const result = await original.call(this, number, prompt, ...rest);
    if (result.prompt_id && !Object.keys(result.node_errors || {}).length) {
      for (const item of queued) item.controller.onQueued(item.state, item.index);
    }
    return result;
  };
}

app.registerExtension({
  name: "artemko7v.ImageCatalogIndexed",
/** Register the hidden catalog-state widget. */
  getCustomWidgets() {
    return {
/** Create the socketless hidden state widget. */
      IMAGE_CATALOG_STATE(node, name) {
        const widget = node.addWidget("text", name, "{}", () => {}, { serialize: true });
        widget.type = "image_catalog_hidden";
        widget.computeSize = () => [0, -4];
        widget.draw = () => {};
        widget.options.socketless = true;
        return { widget };
      },
    };
  },
/** Attach catalog behavior to this node definition. */
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_CLASS) return;
    nodeData.input.required.catalog_state = ["IMAGE_CATALOG_STATE", { socketless: true }];
    // Wrap existing ComfyUI hooks rather than replacing their behavior.
    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated?.apply(this, arguments);
      installStyles();
      this.imageCatalog = new CatalogController(this);
      this.imageCatalog.syncOutputs();
      this.setSize([Math.max(this.size[0], 360), 650]);
      return result;
    };
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure?.apply(this, arguments);
      this.imageCatalog.run(() => this.imageCatalog.restore());
      return result;
    };
    const onSerialize = nodeType.prototype.onSerialize;
    nodeType.prototype.onSerialize = function (data) {
      onSerialize?.apply(this, arguments);
      data.properties ||= {};
      data.properties.imageCatalog = structuredClone(this.imageCatalog.state);
    };
    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      const results = message.catalog_result;
      if (results?.length) this.imageCatalog.onExecuted(results[results.length - 1]);
    };
    const onRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
      this.imageCatalog?.dispose();
      return onRemoved?.apply(this, arguments);
    };
  },
/** Install global queue and execution event integration. */
  async setup() {
    installQueueHook();
    for (const eventName of ["execution_error", "execution_interrupted"]) {
      // Failures unlock queued drafts but keep all local changes for a retry.
      api.addEventListener?.(eventName, (event) => {
        for (const controller of controllers.values()) {
          if (controller.pendingNewToken || String(controller.node.id) === String(event.detail?.node_id)) {
            controller.pendingNewToken = null;
            controller.render();
            controller.status(event.detail?.exception_message || "Execution stopped. Pending changes were kept for the next run.", true);
          }
        }
      });
    }
  },
});
