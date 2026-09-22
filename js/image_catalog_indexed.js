import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { buildClientCatalog, defaultValues, identifier, migrateValues, nextIndex, outputDefinitions, wrapIndex } from "./catalog_model.js";
import { button, element, exportChoice, installStyles, label, renderCatalog, renderCatalogEditor, renderCreate, select } from "./catalog_ui.js";

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
    return { catalogId: "", catalogData: null, schema: [], catalogName: "", selectedId: "", edits: {},
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
    this.state.catalogData = this.catalog;
    this.node.properties.imageCatalog = this.serializableState();
    this.stateWidget.value = JSON.stringify(this.snapshot());
    this.node.setDirtyCanvas(true, true);
  }

/** Return workflow-safe state without browser-only or uncloneable values. */
  serializableState() {
    const { catalogData, ...state } = this.state;
    const workflowState = JSON.parse(JSON.stringify(state));
    try {
      workflowState.catalogData = JSON.parse(JSON.stringify(catalogData));
    } catch {
      // The saved catalog is optional: the server can reload it by ID or name.
      workflowState.catalogData = null;
    }
    return workflowState;
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
    const imageIndex = Number(this.indexWidget.value);
    return {
      catalog_id: this.state.catalogId,
      catalog_name: this.state.catalogName,
      selected_id: this.state.selectedId,
      image_index: Number.isSafeInteger(imageIndex) && imageIndex >= 0 ? imageIndex : 0,
      schema: this.state.schema,
      edits: this.state.edits,
      schema_revision: this.catalog?.schema_revision ?? 0,
      client_catalog: this.workingCatalog(), operation_id: this.state.operationId, instance_id: this.id,
      index_connected: this.connected, index_mode: this.state.indexMode,
      new_images: this.state.newImages.filter((image) => image.filename),
      replacement_images: Object.values(this.state.edits).filter((edit) => !edit.delete && edit.replacement).map((edit) => edit.replacement),
      new_selection: this.state.adding && this.state.newSelection ? this.newImage?.token : null };
  }

/** Build the complete client catalog without changing the saved baseline. */
  workingCatalog() {
    return buildClientCatalog(this.catalog, this.state.schema, this.state.edits, this.state.newImages);
  }

/** Compare persisted content, ignoring transient editor selection and blank drafts. */
  hasUnsavedChanges() {
    if (!this.catalog) return false;
    const content = (catalog) => JSON.stringify({ schema: catalog.schema, entries: catalog.entries });
    return content(this.workingCatalog()) !== content(this.catalog);
  }

/** Read the serialized hidden widget when node properties are unavailable. */
  workflowSnapshot() {
    try {
      const value = this.stateWidget.value;
      const snapshot = typeof value === "string" ? JSON.parse(value) : value;
      return snapshot && typeof snapshot === "object" ? snapshot : null;
    } catch {
      return null;
    }
  }

  /** Restore the workflow's local catalog without replacing it with server data. */
  async restore() {
    // Node properties preserve complete local work. The hidden widget is a
    // portable fallback for frontends that only restore widget values.
    const saved = this.node.properties?.imageCatalog;
    const snapshot = this.workflowSnapshot();
    if (saved) this.state = { ...this.freshState(), ...saved };
    else if (snapshot?.catalog_id) {
      this.state = {
        ...this.freshState(),
        catalogId: snapshot.catalog_id,
        catalogName: snapshot.catalog_name || "",
        schema: Array.isArray(snapshot.schema) ? snapshot.schema : [],
        selectedId: snapshot.selected_id || "",
        indexMode: snapshot.index_mode || this.state.indexMode,
      };
    }
    if (saved?.newImage && !saved.newImages) {
      this.state.newImages = [saved.newImage];
      this.state.draftToken = saved.newImage.token;
    }
    delete this.state.newImage;
    this.catalog = this.state.catalogData || null;
    if (Number.isSafeInteger(snapshot?.image_index) && snapshot.image_index >= 0) {
      this.indexWidget.value = snapshot.image_index;
    }
    this.indexModeWidget.value = this.state.indexMode;
    this.syncOutputs();
    this.render();
    await this.refreshList();

    // Resolve by ID first, then by saved name for copied catalog storage. An
    // unavailable catalog deliberately restores as an unselected node.
    const selectedCatalog = this.catalogs.find((item) => item.id === this.state.catalogId)
      || this.catalogs.find((item) => item.name.toLowerCase() === this.state.catalogName.toLowerCase());
    if (!selectedCatalog) {
      if (this.state.catalogId || this.state.catalogName) await this.loadCatalog("");
      else this.persist();
      return;
    }
    if (!this.catalog || selectedCatalog.id !== this.state.catalogId) {
      await this.loadCatalog(selectedCatalog.id, true);
      return;
    }
    this.state.catalogId = selectedCatalog.id;
    this.state.catalogName = selectedCatalog.name;
    this.persist();
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
    const replacement = this.state.edits[entryId]?.replacement;
    if (replacement) return this.localPreview(replacement.token);
    const entry = this.catalog?.entries.find((image) => image.id === entryId);
    return api.apiURL(`${PREFIX}/catalogs/${this.state.catalogId}/images/${entryId}?v=${entry?.file || ""}`);
  }

/** Reuse one object URL for the displayed local replacement. */
  localPreview(token) {
    if (this.previewToken === token) return this.previewUrl;
    this.releasePreview();
    const file = this.files.get(token);
    if (file) {
      this.previewToken = token;
      this.previewUrl = URL.createObjectURL(file);
    }
    return this.previewUrl;
  }

/** Open the existing record's file picker without changing its values or file. */
  startReplacing() {
    this.replacingId = this.state.selectedId;
    this.render();
  }

/** Leave the replacement picker with the previously selected image intact. */
  cancelReplacing() {
    this.replacingId = null;
    this.render();
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
    this.replacingId = null;
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
    this.state.catalogEdit = { catalogId: this.state.catalogId,
      original: structuredClone(this.state.schema),
      fields: this.state.schema.map((field) => ({ ...field, existing: true })) };
    this.persist();
    this.render();
  }

/** Apply schema changes to this workflow's local catalog only. */
  async saveCatalogSchema() {
    const editor = this.state.catalogEdit;
    const schema = editor.fields.filter((field) => field.name.trim()).map((field) => ({
      name: field.name.trim(), type: field.type, ...(field.hidden ? { hidden: true } : {}),
    }));
    const names = schema.map((field) => field.name.toLowerCase());
    if (new Set(names).size !== names.length || names.includes("image")) throw new Error("Property names must be unique and cannot be IMAGE.");
    const removed = editor.original.filter((field) => !schema.some((next) => next.name === field.name));
    if (removed.length && !window.confirm(`Remove properties ${removed.map((field) => `"${field.name}"`).join(", ")} and their values from every image?`)) return;
    // Materialize overlays before dropping fields so re-adding a field gets defaults.
    for (const entry of this.catalog.entries) this.editEntry(entry);
    this.migrateDraftValues(schema);
    this.state.schema = schema;
    this.syncOutputs();
    this.touch();
    if (this.state.catalogEdit === editor) {
      this.state.catalogEdit = null;
      this.persist();
      this.render();
      this.status("Fields applied locally. Use Save Changes to save the entire catalog.");
    }
  }

/** Download the active catalog as a ZIP archive. */
  async exportCatalog() {
    if (this.state.catalogEdit) throw new Error("Apply or cancel field editing before exporting.");
    if (this.hasUnsavedChanges()) {
      const choice = await exportChoice();
      if (choice === "cancel") return;
      if (choice === "save") await this.saveChanges(false);
    }
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

/** Save every local image and field change as one revision-checked transaction. */
  async saveChanges(confirm = true) {
    if (this.busy) throw new Error("A catalog save is already in progress.");
    if (this.state.catalogEdit) throw new Error("Apply or cancel field editing before saving.");
    if (confirm && !window.confirm("Save all changes for all images and catalog fields? Deleted images will be permanently removed from the server.")) return;
    const snapshot = structuredClone(this.snapshot());
    this.busy = true;
    this.root.inert = true;
    this.status("Saving all catalog changes...");
    try {
      await this.prepareQueue(snapshot);
      const result = await request(`/catalogs/${snapshot.catalog_id}/save`, jsonRequest("POST", {
        catalog: snapshot.client_catalog, expected_revision: this.catalog.revision ?? 0, operation_id: snapshot.operation_id,
      }));
      if (this.disposed || this.state.catalogId !== snapshot.catalog_id) return;
      const selectedId = this.state.adding ? this.state.draftToken : this.state.selectedId;
      this.catalog = result.catalog;
      this.state.edits = {};
      this.state.newImages = this.state.newImages.filter((draft) => !draft.filename);
      this.files.clear();
      this.uploads.clear();
      if (this.catalog.entries.some((entry) => entry.id === selectedId)) this.selectEntry(selectedId);
      else if (!this.newImage) {
        this.state.adding = false;
        this.selectIndex(false);
      }
      this.touch();
      this.render();
      this.status(result.warnings.length ? result.warnings.join("\n") : "All catalog changes saved.", Boolean(result.warnings.length));
    } finally {
      this.busy = false;
      this.root.inert = false;
    }
  }

/** Remove an image locally; its server file is retained until Save Changes. */
  deleteImage() {
    if (!window.confirm("Delete this image from the local catalog? The server file will be removed only when you save all changes.")) return;
    if (this.state.adding) return this.removeDraft();
    const entry = this.catalog.entries.find((item) => item.id === this.state.selectedId);
    if (!entry) return;
    this.editEntry(entry).delete = true;
    this.selectIndex(false);
    this.touch();
    this.render();
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
    this.replacingId = null;
    const catalog = this.workingCatalog();
    const entries = catalog?.entries.filter((entry) => !entry.hidden) || [];
    const index = wrapIndex(Number(this.indexWidget.value) || 0, entries.length);
    this.indexWidget.value = index;
    this.state.selectedId = entries[index]?.id || catalog?.entries[0]?.id || "";
    const draft = this.state.newImages.find((image) => image.token === this.state.selectedId);
    if (draft) {
      this.state.adding = true;
      this.state.draftToken = draft.token;
      this.releasePreview();
      const file = this.files.get(draft.token);
      if (file) this.previewUrl = URL.createObjectURL(file);
    }
    if (render) this.render();
  }

/** Select an existing catalog entry. */
  selectEntry(id) {
    this.replacingId = null;
    this.releasePreview();
    this.state.adding = false;
    this.state.selectedId = id;
    const index = this.workingCatalog().entries.filter((entry) => !entry.hidden).findIndex((entry) => entry.id === id);
    if (index >= 0 && !this.connected) this.indexWidget.value = index;
    this.persist();
    this.render();
  }

/** Open or create an image draft. */
  startAdding(render = true) {
    this.replacingId = null;
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
    this.replacingId = null;
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
    this.state.edits[entry.id] ||= { values: migrateValues(this.state.schema, entry.values), hidden: entry.hidden, delete: false, revision: entry.revision };
    return this.state.edits[entry.id];
  }

/** Release the object URL for a local image preview. */
  releasePreview() {
    if (this.previewUrl) URL.revokeObjectURL(this.previewUrl);
    this.previewUrl = null;
    this.previewToken = null;
  }

/** Attach a local file to a new draft or replace an existing record's image. */
  async chooseFile(file) {
    if (!file) return;
    if (file.size > 64 * 1024 * 1024) throw new Error("Image uploads cannot exceed 64 MiB.");
    if (!this.state.adding) {
      const entry = this.catalog.entries.find((image) => image.id === this.replacingId);
      if (!entry || this.replacingId !== this.state.selectedId) return;
      const edit = this.editEntry(entry);
      if (edit.hidden || edit.delete) return;
      if (edit.replacement) {
        this.files.delete(edit.replacement.token);
        this.uploads.delete(edit.replacement.token);
      }
      const token = identifier();
      this.files.set(token, file);
      edit.replacement = { token, filename: file.name };
      this.replacingId = null;
      this.touch();
      this.render();
      return;
    }
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
    const catalogBlock = element("section", "ic-block ic-catalog-block");
    const selectorRow = element("div", "ic-selector-row");
    selectorRow.append(label("Catalog", select(options, this.state.catalogId, (id) => this.run(async () => {
      if ((this.hasUnsavedChanges() || this.state.catalogEdit) && !window.confirm("Discard local changes and switch catalogs?")) {
        this.render();
        return;
      }
      await this.loadCatalog(id);
    }))), button("Refresh", () => this.run(() => this.refreshList())));
    catalogBlock.append(selectorRow);
    this.statusElement = element("div", "ic-status" + (this.isError ? " is-error" : ""), this.message || "");
    const actions = element("div", "ic-actions ic-catalog-actions");
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
    if (!this.state.catalogId) actions.append(button("Import Catalog", () => archive.click()), archive);
    if (this.state.catalogId) {
      actions.append(button("Edit Catalog", () => this.run(() => this.editCatalog())),
        button("Export Catalog", () => this.run(() => this.exportCatalog())));
      actions.append(button("Reload Catalog", () => this.run(async () => {
        if ((this.hasUnsavedChanges() || this.state.catalogEdit) &&
          !window.confirm("Discard pending edits and reload this catalog?")) return;
        await this.loadCatalog(this.state.catalogId);
      })));
      const remove = button("Delete Catalog", () => this.run(async () => {
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
    catalogBlock.append(actions);
    if (!this.state.catalogId || this.state.catalogEdit) catalogBlock.append(element("hr", "ic-divider"));
    if (!this.state.catalogId) renderCreate(this, catalogBlock);
    else if (this.catalog && this.state.catalogEdit) renderCatalogEditor(this, catalogBlock);
    this.root.append(catalogBlock, this.statusElement);
    if (this.catalog) {
      const imageBlock = element("section", "ic-block ic-image-block");
      renderCatalog(this, imageBlock);
      this.root.append(imageBlock);
    }
  }

/** Validate queued state and stage each local image upload. */
  async prepareQueue(state) {
    // Validate browser-only values and stage every local file before queuing.
    if (!this.catalog || this.catalog.id !== state.catalog_id) throw new Error("Wait for the image catalog to load before running.");
    for (const { values } of state.client_catalog.entries) {
      for (const field of state.schema) {
        if (field.type === "Integer" && !Number.isSafeInteger(values[field.name])) {
          throw new Error(`Enter a valid integer for '${field.name}'.`);
        }
      }
    }
    const pendingImages = [...state.new_images, ...(state.replacement_images || [])];
    for (const image of pendingImages) {
      const { token } = image;
      const file = this.files.get(token);
      if (!file) throw new Error(`Select '${image.filename}' again before running or saving. Local files cannot be restored from workflow JSON.`);
    }
    for (const { token } of pendingImages) {
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
  }

/** Update local state after prompt acceptance. */
  onQueued(snapshot, index) {
    // Advance the local index with the same visible-entry rules as the backend.
    if (this.disposed || snapshot.catalog_id !== this.state.catalogId) return;
    if (snapshot.index_connected) return;
    const entries = snapshot.client_catalog.entries.filter((entry) => !entry.hidden);
    const selectedNew = entries.findIndex((entry) => entry.id === snapshot.new_selection);
    const selected = selectedNew >= 0 ? selectedNew : index;
    this.indexWidget.value = nextIndex(selected, entries.length, snapshot.index_mode);
    this.state.newSelection = false;
    if (!this.state.adding) {
      this.selectIndex(false);
      this.render();
    }
    this.persist();
  }

/** Report read-only execution without replacing the local catalog. */
  onExecuted(result) {
    if (result.catalog_id && result.catalog_id !== this.state.catalogId) return;
    if (this.connected && result.selected_id) {
      const draft = this.state.newImages.find((image) => image.token === result.selected_id);
      if (draft) this.selectDraft(draft.token);
      else if (this.workingCatalog()?.entries.some((entry) => entry.id === result.selected_id)) this.selectEntry(result.selected_id);
    }
    this.status(result.selected_id ? (this.hasUnsavedChanges() ? "Workflow completed. Local changes have not been saved." : "Workflow completed.") :
      "No visible images. Downstream nodes were skipped.");
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
      data.properties.imageCatalog = this.imageCatalog.serializableState();
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
