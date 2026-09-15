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

/** Materialize every local edit without mutating the saved baseline. */
export function buildClientCatalog(catalog, schema, edits, drafts) {
  if (!catalog) return null;
  const entries = catalog.entries.filter((entry) => !edits[entry.id]?.delete).map((entry) => {
    const edit = edits[entry.id];
    return { ...entry, values: migrateValues(schema, edit?.values || entry.values), hidden: edit?.hidden ?? entry.hidden };
  });
  for (const draft of drafts.filter((image) => image.filename)) {
    if (catalog.entries.some((entry) => entry.id === draft.token)) continue;
    entries.push({ id: draft.token, file: `${draft.token}.png`, filename: draft.filename,
      values: migrateValues(schema, draft.values), hidden: false, revision: 1 });
  }
  return { ...catalog, schema, entries };
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
