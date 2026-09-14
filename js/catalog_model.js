export const PROPERTY_TYPES = { String: "STRING", Text: "STRING", Integer: "INT", Boolean: "BOOLEAN" };
export const MAX_PROPERTIES = 32;

/** Create a backend-compatible random identifier. */
export function identifier() {
  // Match the backend's 32-character lowercase hexadecimal identifier format.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

/** Return typed default values for a property schema. */
export function defaultValues(schema) {
  // Shared by draft creation and schema migration.
  return Object.fromEntries(schema.map(({ name, type }) => [name,
    type === "Boolean" ? false : type === "Integer" ? 0 : "",
  ]));
}

/** Wrap an index into the available entry range. */
export function wrapIndex(index, count) {
  return count ? ((Math.trunc(index) % count) + count) % count : 0;
}

/** Calculate the next visible index for the selected queue mode. */
export function nextIndex(index, count, mode, random = Math.random) {
  if (!count) return 0;
  if (mode === "randomize") return Math.floor(random() * count);
  return wrapIndex(index + (mode === "increment" ? 1 : mode === "decrement" ? -1 : 0), count);
}

/** Build the visible entry sequence used by an execution. */
export function executionEntries(catalog, edits, saveChanges, pendingNew) {
  // Mirror backend selection: only saved hide/delete edits affect visible indexes.
  const entries = catalog.entries.filter((entry) => {
    const edit = saveChanges ? edits[entry.id] : null;
    return !(edit ? edit.delete || edit.hidden : entry.hidden);
  });
  for (const image of Array.isArray(pendingNew) ? pendingNew : pendingNew ? [pendingNew] : []) {
    if (!entries.some((entry) => entry.id === image.token) && !catalog.entries.some((entry) => entry.id === image.token)) {
      entries.push({ id: image.token, filename: image.filename, values: image.values, hidden: false });
    }
  }
  return entries;
}

/** Map a schema to named ComfyUI output definitions. */
export function outputDefinitions(schema) {
  return [{ name: "IMAGE", type: "IMAGE" }, ...schema.map(({ name, type }) => ({ name, type: PROPERTY_TYPES[type] }))];
}

/** Retain matching values and default newly added fields. */
export function migrateValues(schema, values) {
  // Retain matching fields and provide defaults for newly added properties.
  const defaults = defaultValues(schema);
  return Object.fromEntries(schema.map(({ name }) => [name, Object.hasOwn(values, name) ? values[name] : defaults[name]]));
}
