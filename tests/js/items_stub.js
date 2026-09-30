// The items page under node: a DOM just big enough for static/index.js, the
// /api/items reply from a file, and a scenario run in the page's own scope
// afterwards. Usage: node items_stub.js <reply.json> <scenario.js>
const fs = require("fs");
const vm = require("vm");

function el(tag) {
  const node = {
    tag, id: "", value: "", innerHTML: "", textContent: "", className: "",
    hidden: false, disabled: false, checked: false, children: [], style: {},
    dataset: {}, draggable: false,
    appendChild(c) { this.children.push(c); c.parentElement = this; return c; },
    insertAdjacentHTML(_, html) { this.innerHTML += html; },
    addEventListener(ev, fn) { (this._on = this._on || {})[ev] = fn; },
  };
  node.classList = {
    add(c) { node.className = (node.className + " " + c).trim(); },
    remove(c) { node.className = node.className.split(/\s+/).filter((x) => x !== c).join(" "); },
    toggle(c, on) { if (on === false) node.classList.remove(c); else node.classList.add(c); },
    contains: (c) => node.className.split(/\s+/).includes(c),
  };
  Object.defineProperty(node, "innerHTML", {
    get() { return this._html || ""; },
    set(v) { this._html = v; this.children = []; },
  });
  return node;
}

const nodes = {};
const byId = (id) => { if (!nodes[id]) { nodes[id] = el("div"); nodes[id].id = id; } return nodes[id]; };
const selected = {};
global.document = {
  getElementById: byId,
  createElement: (t) => el(t),
  querySelector(sel) { return (selected[sel] = selected[sel] || el("div")); },
  querySelectorAll: () => [],
  addEventListener() {},
  currentScript: { src: "/static/index.js?v=test" },
};
global.window = { addEventListener() {} };
const store = {};
global.localStorage = { getItem: (k) => store[k] || "", setItem: (k, v) => { store[k] = v; } };
global.confirm = () => true;
global.prompt = () => null;
const realInterval = global.setInterval;
global.setInterval = (fn, ms) => { const t = realInterval(fn, ms); if (t.unref) t.unref(); return t; };

const reply = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
global.fetch = async () => ({ ok: true, statusText: "OK", json: async () => reply });

vm.runInThisContext(fs.readFileSync("static/common.js", "utf8") + "\n"
  + fs.readFileSync("static/index.js", "utf8"), { filename: "index.js" });

(async () => {
  await new Promise((r) => setTimeout(r, 30));
  global.__nodes = nodes;
  global.__selected = selected;
  const out = vm.runInThisContext(fs.readFileSync(process.argv[3], "utf8"));
  console.log(JSON.stringify(await out));
})();
