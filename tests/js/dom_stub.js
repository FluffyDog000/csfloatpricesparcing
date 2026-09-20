// A DOM small enough to run the dashboard's page scripts under node, so a
// runtime error in one is caught here rather than as a button that silently
// does nothing. Usage: node dom_stub.js <script.js> <api-response.json>
const fs = require("fs");

function el(tag) {
  const node = {
    tag, id: "", value: "", innerHTML: "", textContent: "", className: "",
    disabled: false, title: "", checked: false, children: [], onclick: null,
    style: {},
    appendChild(c) { this.children.push(c); c.parentElement = this; return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); },
    remove() { if (this.parentElement) this.parentElement.removeChild(this); },
    addEventListener() {},
    querySelectorAll: () => [],
  };
  node.classList = {
    add(c) { node.className = (node.className + " " + c).trim(); },
    remove(c) {
      node.className = node.className.split(/\s+/).filter((x) => x !== c)
        .join(" ");
    },
    contains: (c) => node.className.split(/\s+/).includes(c),
  };
  return node;
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
// A page that polls must not keep node alive after the report is printed.
const realInterval = global.setInterval;
global.setInterval = (fn, ms) => {
  const t = realInterval(fn, ms);
  if (t && t.unref) t.unref();
  return t;
};
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

// In a browser textContent is the text of a node AND everything under it.
// The stub keeps them apart, so reading one property missed anything a script
// appended as a child - a list of what a failed request tried, for instance.
function deepText(node) {
  if (!node) return "";
  return (node.textContent || "")
    + (node.children || []).map(deepText).join(" ");
}

(async () => {
  if (doc._ready) await doc._ready();
  await new Promise((r) => setTimeout(r, 50));
  // Either page's status line: whichever one the script under test uses.
  const note = nodes["an-note"] && nodes["an-note"].textContent
    ? nodes["an-note"] : (nodes["j-note"] || el("div"));
  const tableOf = (id) => {
    const box = nodes[id] || el("div");
    const t = box.children[0];
    const body = t && t.children[0];
    return body ? body.children.length : 0;
  };
  console.log(JSON.stringify({
    status: note.textContent,
    kind: note.className,
    journalRows: tableOf("j-table"),
    tiles: (nodes["j-summary"] || el("div")).children.length,
    defence: deepText(nodes["j-state"]),
    sync: deepText(nodes["j-sync-state"]),
    chips: (nodes["an-list"] || el("div")).children.length,
    listHtml: (nodes["an-list"] || el("div")).innerHTML,
    sections: (nodes["an-results"] || el("div")).children.length,
    funnel: (nodes["an-funnel"] || el("div")).children.length,
    funnelText: deepText(nodes["an-funnel"]),
    planSync: (nodes["plan-sync"] || el("div")).textContent,
    step: (nodes["p-step"] || el("div")).value,
    budget: (nodes["l-total"] || el("div")).value,
  }));
})();
