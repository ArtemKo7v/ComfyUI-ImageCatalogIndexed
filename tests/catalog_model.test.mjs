import assert from "node:assert/strict";
import test from "node:test";
import { defaultValues, executionEntries, migrateValues, nextIndex, outputDefinitions } from "../js/catalog_model.js";

test("index controls wrap and randomize over visible records", () => {
  assert.equal(nextIndex(2, 3, "increment"), 0);
  assert.equal(nextIndex(0, 3, "decrement"), 2);
  assert.equal(nextIndex(7, 3, "fixed"), 1);
  assert.equal(nextIndex(0, 3, "randomize", () => 0.99), 2);
  assert.equal(nextIndex(2, 0, "increment"), 0);
});

test("queued edits affect indexing only when saved", () => {
  const catalog = { entries: [{ id: "a", hidden: false }, { id: "b", hidden: true }, { id: "c", hidden: false }] };
  const edits = { a: { hidden: false, delete: true }, b: { hidden: false, delete: false } };
  assert.deepEqual(executionEntries(catalog, edits, false, null).map((entry) => entry.id), ["a", "c"]);
  assert.deepEqual(executionEntries(catalog, edits, true, { token: "d" }).map((entry) => entry.id), ["b", "c", "d"]);
  assert.deepEqual(executionEntries(catalog, {}, false, { token: "a" }).map((entry) => entry.id), ["a", "c"]);
});

test("schema determines typed output order and default values", () => {
  const schema = [{ name: "caption", type: "Text" }, { name: "seed", type: "Integer" }, { name: "enabled", type: "Boolean" }];
  assert.deepEqual(outputDefinitions(schema), [{ name: "IMAGE", type: "IMAGE" }, { name: "caption", type: "STRING" },
    { name: "seed", type: "INT" }, { name: "enabled", type: "BOOLEAN" }]);
  assert.deepEqual(defaultValues(schema), { caption: "", seed: 0, enabled: false });
});

test("all pending images participate in index advancement without duplicates", () => {
  const catalog = { entries: [{ id: "a", hidden: false }] };
  const pending = [{ token: "b" }, { token: "c" }, { token: "a" }];
  const entries = executionEntries(catalog, {}, false, pending);
  assert.deepEqual(entries.map((entry) => entry.id), ["a", "b", "c"]);
  assert.equal(nextIndex(2, entries.length, "increment"), 0);
});

test("schema migration preserves hidden values and defaults only added properties", () => {
  const schema = [{ name: "caption", type: "Text", hidden: true }, { name: "enabled", type: "Boolean" }];
  assert.deepEqual(migrateValues(schema, { caption: "Keep this", deleted: 12 }), { caption: "Keep this", enabled: false });
  assert.deepEqual(outputDefinitions(schema), [{ name: "IMAGE", type: "IMAGE" }, { name: "caption", type: "STRING" }, { name: "enabled", type: "BOOLEAN" }]);
});
