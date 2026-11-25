/* Enough of a browser for the shipped assets to run end to end in Node.
 *
 * The assets are plain browser scripts: they read a JSON payload from a
 * script tag, register a load handler, and touch the page through
 * document, window, fetch and Plotly. load() runs one in a fresh vm context
 * against fakes of exactly those, under its real filename, so V8's coverage
 * is attributed to the file the scanner analyses (see run.mjs).
 *
 * The fakes record what was done to them rather than discarding it, so a
 * test can assert the effect: a class toggled, a trace added, a request sent.
 */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

export const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..", "..");
export const ASSETS = path.join(ROOT, "timeslides", "report", "assets");

class ClassList {
  constructor() { this.set = new Set(); }
  add(...names) { names.forEach(n => this.set.add(n)); }
  remove(...names) { names.forEach(n => this.set.delete(n)); }
  contains(name) { return this.set.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    return on;
  }
  toString() { return [...this.set].join(" "); }
}

export class FakeElement {
  constructor(page, key) {
    this.page = page;
    this.key = key;
    this.id = key;
    this.classList = new ClassList();
    this.dataset = {};
    this.style = {};
    this.attrs = {};
    this.handlers = {};
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.lists = {};
    this.children = {};
    this.removed = false;
    this.closestMap = {};
    this.data = [];
    this.layout = {};
  }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    const ev = {target: this, preventDefault() {}, key: "", ...event};
    return Promise.all((this.handlers[type] || []).map(fn => fn(ev)));
  }
  click() { return this.dispatch("click"); }
  querySelector(sel) {
    if (sel in this.lists) return this.lists[sel][0] ?? null;
    return (this.children[sel] ||= new FakeElement(this.page, `${this.key} ${sel}`));
  }
  querySelectorAll(sel) { return this.lists[sel] || []; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  hasAttribute(name) { return name in this.attrs; }
  insertAdjacentHTML(_where, html) { this.innerHTML += html; }
  remove() { this.removed = true; }
  closest(sel) { return this.closestMap[sel] ?? null; }
  focus() { this.page.focused = this; }
  scrollIntoView() {}
}

/* A recording Plotly. Traces live on the plot element, as they do in the
 * real library, so code that reads gd.data after an addTraces sees them. */
function fakePlotly(page) {
  const gd = id => page.byId(id);
  const calls = [];
  return {
    calls,
    restyle(id, update, idx) { calls.push(["restyle", id, update, idx]); },
    relayout(id, update) { calls.push(["relayout", id, update]); Object.assign(gd(id).layout, update); },
    addTraces(id, traces) {
      calls.push(["addTraces", id, traces]);
      gd(id).data.push(...(Array.isArray(traces) ? traces : [traces]));
    },
    deleteTraces(id, idx) {
      calls.push(["deleteTraces", id, idx]);
      const drop = new Set(Array.isArray(idx) ? idx : [idx]);
      gd(id).data = gd(id).data.filter((_t, i) => !drop.has(i));
    },
    Plots: {resize(id) { calls.push(["resize", id]); }},
  };
}

export class FakePage {
  constructor(payloads = {}) {
    this.elements = {};
    this.selectors = {};
    this.payloads = payloads;
    this.windowHandlers = {};
    this.requests = [];
    this.replies = [];
    this.storage = {};
    this.storageThrows = false;
    this.confirmAnswer = true;
    this.timers = [];
    this.warnings = [];
    this.focused = null;
    this.body = new FakeElement(this, "body");
    this.Plotly = fakePlotly(this);
  }
  byId(id) {
    if (!(id in this.elements)) {
      this.elements[id] = new FakeElement(this, id);
      if (id in this.payloads) this.elements[id].textContent = this.payloads[id];
    }
    return this.elements[id];
  }
  /* What document.querySelectorAll(sel) returns. Tests set it. */
  setAll(sel, els) { this.selectors[sel] = els; }
  element(key, props = {}) { return Object.assign(new FakeElement(this, key), props); }
  /* Queue the next fetch reply: {ok, status, body} or an Error to reject with. */
  reply(r) { this.replies.push(r); }
  runTimers() {
    const due = this.timers.splice(0);
    return Promise.all(due.map(fn => fn()));
  }
  globals() {
    const page = this;
    const document = {
      body: page.body,
      getElementById: id => (id in page.payloads || id in page.elements || !page.strictIds
        ? page.byId(id) : null),
      querySelector: sel => (page.selectors[sel] ? page.selectors[sel][0] ?? null : page.byId(`q:${sel}`)),
      querySelectorAll: sel => page.selectors[sel] || [],
    };
    const localStorage = {
      getItem(k) {
        if (page.storageThrows) throw new Error("denied");
        return Object.hasOwn(page.storage, k) ? page.storage[k] : null;
      },
      setItem(k, v) {
        if (page.storageThrows) throw new Error("denied");
        page.storage[k] = String(v);
      },
    };
    const window = {
      addEventListener(name, fn) { page.windowHandlers[name] = fn; },
      localStorage,
      confirm: () => page.confirmAnswer,
      Plotly: page.Plotly,
      location: {href: ""},
    };
    const fetch = (url, opts = {}) => {
      page.requests.push({url, opts, body: opts.body ? JSON.parse(opts.body) : null});
      const r = page.replies.shift() ?? {ok: true, status: 200, body: {}};
      if (r instanceof Error) return Promise.reject(r);
      return Promise.resolve({
        ok: r.ok ?? true, status: r.status ?? 200,
        json: () => (r.badJson ? Promise.reject(new SyntaxError("bad json")) : Promise.resolve(r.body)),
        text: () => Promise.resolve(typeof r.body === "string" ? r.body : JSON.stringify(r.body)),
      });
    };
    return {
      document, window, fetch, Plotly: page.Plotly,
      console: {log() {}, error() {}, warn: (...a) => page.warnings.push(a)},
      setTimeout: fn => { page.timers.push(fn); return page.timers.length; },
      clearTimeout() {},
      CSS: {escape: s => String(s)},
      // new FormData(form) yields the form's [name, value] pairs, which is all
      // Object.fromEntries needs. Returning an object from a constructor
      // replaces the instance, so this is the array itself.
      FormData: function FormData(form) { return form.formEntries || []; },
    };
  }
}

/* Run one asset in a fresh context. Returns {page, ctx, run(expr)}. */
export function load(asset, payloads = {}, setup = () => {}) {
  const page = new FakePage(payloads);
  setup(page);
  const ctx = vm.createContext(page.globals());
  const file = path.join(ASSETS, asset);
  vm.runInContext(fs.readFileSync(file, "utf8"), ctx, {filename: file});
  const run = expr => vm.runInContext(expr, ctx);
  return {page, ctx, run};
}

export function fixture(name) {
  return fs.readFileSync(path.join(ROOT, "tests", "js", "fixtures", name), "utf8");
}

/* Let queued promise callbacks run. */
export const settle = () => new Promise(r => setImmediate(r));
