export const api = {
  apiURL(path) { return path; },
  fetchApi(path, options) { return fetch(path, options); },
  async queuePrompt(number, prompt) {
    const response = await fetch("/test/queue", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(prompt) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    setTimeout(() => window.catalogNode.onExecuted(result.ui), 30);
    window.lastOutputs = result.outputs;
    return { prompt_id: "test-prompt", node_errors: {} };
  },
};
