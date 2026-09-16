import assert from "node:assert/strict";
import test from "node:test";
import { buildClientCatalog, defaultValues, migrateValues, nextIndex, outputDefinitions } from "../js/catalog_model.js";

test("index controls wrap and randomize over visible records", () => {
  assert.equal(nextIndex(2, 3, "increment"), 0);
  assert.equal(nextIndex(0, 3, "decrement"), 2);
  assert.equal(nextIndex(7, 3, "fixed"), 1);
  assert.equal(nextIndex(0, 3, "randomize", () => 0.99), 2);
  assert.equal(nextIndex(2, 0, "increment"), 0);
});

test("local edits affect visible indexing without mutating saved entries", () => {
  const catalog = { entries: [{ id: "a", hidden: false, values: {} }, { id: "b", hidden: true, values: {} }, { id: "c", hidden: false, values: {} }] };
  const original = structuredClone(catalog);
  const edits = { a: { hidden: false, delete: true }, b: { hidden: false, delete: false } };
  const client = buildClientCatalog(catalog, [], edits, []);
  assert.deepEqual(client.entries.filter((entry) => !entry.hidden).map((entry) => entry.id), ["b", "c"]);
  assert.deepEqual(catalog, original);
});

test("schema determines typed output order and default values", () => {
  const schema = [{ name: "caption", type: "Text" }, { name: "seed", type: "Integer" }, { name: "enabled", type: "Boolean" }];
  assert.deepEqual(outputDefinitions(schema), [{ name: "IMAGE", type: "IMAGE" }, { name: "caption", type: "STRING" },
    { name: "seed", type: "INT" }, { name: "enabled", type: "BOOLEAN" }]);
  assert.deepEqual(defaultValues(schema), { caption: "", seed: 0, enabled: false });
});

test("all pending images participate in index advancement without duplicates", () => {
  const catalog = { entries: [{ id: "a", hidden: false, values: {} }] };
  const pending = ["b", "c", "a"].map((token) => ({ token, filename: `${token}.png`, values: {} }));
  pending.push({ token: "blank", filename: "", values: {} });
  const { entries } = buildClientCatalog(catalog, [], {}, pending);
  assert.deepEqual(entries.map((entry) => entry.id), ["a", "b", "c"]);
  assert.equal(nextIndex(2, entries.length, "increment"), 0);
});

test("schema migration preserves hidden values and defaults only added properties", () => {
  const schema = [{ name: "caption", type: "Text", hidden: true }, { name: "enabled", type: "Boolean" }];
  assert.deepEqual(migrateValues(schema, { caption: "Keep this", deleted: 12 }), { caption: "Keep this", enabled: false });
  assert.deepEqual(outputDefinitions(schema), [{ name: "IMAGE", type: "IMAGE" }, { name: "caption", type: "STRING" }, { name: "enabled", type: "BOOLEAN" }]);
});

test("replacing a file preserves entry identity, order, properties and visibility", () => {
  const schema = [{ name: "caption", type: "Text" }];
  const catalog = { schema, entries: [
    { id: "first", file: "old.png", filename: "old.png", values: { caption: "Keep" }, hidden: true },
    { id: "second", file: "second.png", filename: "second.png", values: { caption: "Second" }, hidden: false },
  ] };
  const original = structuredClone(catalog);
  const client = buildClientCatalog(catalog, schema, { first: { replacement: { token: "new", filename: "replacement.jpg" } } }, []);
  assert.deepEqual(client.entries.map((entry) => entry.id), ["first", "second"]);
  assert.deepEqual(client.entries[0], { ...original.entries[0], file: "new.png", filename: "replacement.jpg" });
  assert.deepEqual(catalog, original);
});
