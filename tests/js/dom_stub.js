// A DOM small enough to run the dashboard's page scripts under node, so a
// runtime error in one is caught here rather than as a button that silently
// does nothing. Usage: node dom_stub.js <script.js> <api-response.json>
const fs = require("fs");

function el(tag) {
  return {
    tag, id: "", value: "", innerHTML: "", textContent: "", className: "",
    disabled: false, title: "", children: [], onclick: null,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    querySelectorAll: () => [],
  };
}

const nodes = {};
const doc = {
  getElementById(id) {
    if (!nodes[id]) { nodes[id] = el("div"); nodes[id].id = id; }
    return nodes[id];
  },
  createElement: (t) => el(t),
  createTextNode: (t) => ({ tag: "#text", textContent: t }),
  addEventListener(ev, fn) { if (ev === "DOMContentLoaded") doc._ready = fn; },
  querySelectorAll: () => [],
  currentScript: { src: "/static/x.js?v=testbuild" },
};

global.document = doc;
global.window = { addEventListener() {} };
global.localStorage = { getItem: () => "" };
global.confirm = () => true;

const api = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
// A fourth argument supplies the plan endpoint's reply; without one it
// answers empty, which is also what an unconfigured bot returns.
const plan = process.argv[4]
  ? JSON.parse(fs.readFileSync(process.argv[4], "utf8"))
  : { actions: [], limits: { total_capital: 0 }, held: {}, armed: false,
      placement: "постановка не настроена", can_place: false,
      can_cancel: false };
global.fetch = async (url) => ({
  ok: true,
  statusText: "OK",
  json: async () => {
    if (url.startsWith("/api/analysis/plan")) return plan;
    if (url.startsWith("/api/analysis")) return api;
    return { items: [] };
  },
});

// One program, one scope — as a browser loads them. Two eval() calls gave
// each file its own scope for const/let, so a redeclaration between them
// parsed cleanly here and threw a SyntaxError in the browser, skipping the
// page script entirely while every test passed.
const vm = require("vm");
const source = fs.readFileSync("static/common.js", "utf8") + "\n"
  + fs.readFileSync(process.argv[2], "utf8");
vm.runInThisContext(source, { filename: "page.js" });

(async () => {
  if (doc._ready) await doc._ready();
  await new Promise((r) => setTimeout(r, 50));
  const note = nodes["an-note"] || el("div");
  console.log(JSON.stringify({
    status: note.textContent,
    kind: note.className,
    chips: (nodes["an-list"] || el("div")).children.length,
    listHtml: (nodes["an-list"] || el("div")).innerHTML,
    sections: (nodes["an-results"] || el("div")).children.length,
    step: (nodes["p-step"] || el("div")).value,
  }));
})();
