// Execute the generated page with DOM/timer stubs and browser global constraints.
// This checks runtime behavior, not pixel layout or a real browser rendering.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const htmlPath = process.argv[2] || path.join(
  __dirname, "../outputs/pilot_success_boundary_route.html"
);
const html = fs.readFileSync(htmlPath, "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(script, "generated HTML must contain a playback script");

class Element {
  constructor() {
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.style = {};
    this.children = [];
    this.listeners = {};
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  fire(type) {
    assert.ok(this.listeners[type], "missing event handler: " + type);
    this.listeners[type]({target: this});
  }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
  replaceChildren(...items) { this.children = items; }
}

const elements = new Map();
for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
  elements.set(match[1], new Element());
}
const el = id => {
  assert.ok(elements.has(id), "unknown element: " + id);
  return elements.get(id);
};
const drawingCalls = [];
const drawing = {};
for (const method of [
  "save", "restore", "beginPath", "moveTo", "lineTo", "setLineDash", "stroke",
  "fillRect", "fillText", "strokeRect", "arc", "fill", "translate", "rotate",
  "closePath", "rect",
]) {
  drawing[method] = (...args) => {
    assert.ok(args.every(arg => typeof arg !== "number" || Number.isFinite(arg)));
    drawingCalls.push([method, ...args]);
  };
}
el("map").width = 900;
el("map").height = 900;
el("map").getContext = () => drawing;
el("speed").value = "1";
el("show-radii").checked = true;
const timers = new Map();
let nextTimer = 0;
const context = vm.createContext({
  console,
  document: {
    getElementById: el,
    createElement: () => new Element(),
    addEventListener() {},
  },
  setInterval(callback) {
    const id = ++nextTimer;
    timers.set(id, callback);
    return id;
  },
  clearInterval(id) { timers.delete(id); },
});
// Define in the context itself: copying a descriptor onto the sandbox object
// does not reproduce browser GlobalDeclarationInstantiation.
vm.runInContext(
  "Object.defineProperty(globalThis, 'top', {value: {browserTop: true}, configurable: false});",
  context
);
vm.runInContext(script[1], context, {filename: htmlPath});
assert.equal(el("time-value").textContent, "0", "initial frame must render");
const expectedMembers = Number(process.argv[3] || 3);
assert.equal(el("legend").children.length, expectedMembers, "every team member needs a legend entry");
assert.ok(drawingCalls.length > 0, "map must be drawn");
assert.equal(vm.runInContext("top.browserTop", context), true);

function tick() {
  assert.equal(timers.size, 1, "exactly one playback timer must be active");
  [...timers.values()][0]();
}
tick();
assert.equal(el("time-value").textContent, "1");
el("play").fire("click");
assert.equal(timers.size, 0, "pause must stop playback");
el("next").fire("click");
assert.equal(el("time-value").textContent, "2");
el("previous").fire("click");
assert.equal(el("time-value").textContent, "1");
el("scrub").value = "12";
el("scrub").fire("input");
assert.equal(el("time-value").textContent, "12");
el("play").fire("click");
el("speed").value = "4";
el("speed").fire("change");
assert.equal(timers.size, 1, "changing speed must replace the timer");
for (let i = 0; i < 6; i++) tick();
assert.equal(el("time-value").textContent, "18");
assert.equal(el("status").textContent, "捕获成功");
assert.equal(timers.size, 0, "playback must stop at the last frame");
el("play").fire("click");
assert.equal(el("time-value").textContent, "0", "replay must restart from frame zero");
tick();
assert.equal(el("time-value").textContent, "1");
el("show-plan").checked = true;
el("show-plan").fire("change");
el("show-radii").checked = false;
el("show-radii").fire("change");
console.log("PASS: initialization, playback, pause, seek, step, speed, finish, replay, overlays");
