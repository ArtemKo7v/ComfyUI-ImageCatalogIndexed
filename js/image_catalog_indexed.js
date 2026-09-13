import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "ArtemKo7vImageCatalogIndexed";

/* -------------------------------------------------------------- helpers */

function widgetOf(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function widgetValue(node, name, fallback) {
  const w = widgetOf(node, name);
  return w ? w.value : fallback;
}

function parseExtensions(raw) {
  return String(raw || "")
    .split(/[,;\s]+/)
    .map((x) => x.trim().toLowerCase().replace(/^\./, ""))
    .filter(Boolean);
}

function extensionOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
}


function setupNode(node) {
  // ...
  syncAll(node);
}




/* ---------------------------------------------------------- registration */

app.registerExtension({
  name: "artemko7v.ImageCatalogIndexed",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_CLASS) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated?.apply(this, arguments);
      setupNode(this);
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure?.apply(this, arguments);
      // ...
      syncAll(this);
      return result;
    };

    const onRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
      // ...
      return onRemoved?.apply(this, arguments);
    };
  },

  async setup() {
    installHook();
  },
});
