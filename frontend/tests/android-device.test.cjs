const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

// A small hook harness exercises the component's event handlers without a device,
// browser, network connection, or extra production/test dependencies.
function harness() {
  const calls = [];
  const scopes = new Map();
  let hooks, cursor;
  const model = {
    state: {
      connected: true, estop: false,
      current: { A: 10, B: 20 }, user_caps: { A: 100, B: 100 },
      effective_caps: { A: 30, B: 40 }, caps: { A: 200, B: 200 },
      presets: [{ name: "呼吸", category: "经典", label: "呼吸" }, { name: "导入波形", category: "导入", label: "导入波形" }],
      relay: { status: "paired", clients: [] },
    },
    linkOn: false, lastPreset: { A: null, B: null },
    toggleLink() { model.linkOn = !model.linkOn; },
    setFocus() {},
    setLastPreset(ch, name) { model.lastPreset[ch] = name; },
  };
  const react = {
    useState(initial) {
      const current = hooks, index = cursor++;
      if (!(index in current)) current[index] = typeof initial === "function" ? initial() : initial;
      return [current[index], (value) => { current[index] = typeof value === "function" ? value(current[index]) : value; }];
    },
    useRef(value) { const index = cursor++; return hooks[index] ??= { current: value }; },
    useEffect() { cursor++; },
    useMemo(fn) { cursor++; return fn(); },
  };
  const api = {
    async manual(action) { calls.push(["manual", action]); return { executed: [{ label: "已处理", sent: true }], dropped: [] }; },
    async setChannelCap(ch, value) { calls.push(["cap", ch, value]); return { ok: true, user_caps: { [ch]: value } }; },
    async setAutopilot(value) { calls.push(["auto", value]); },
  };
  const jsx = (type, props) => ({ type, props });
  const dependencies = {
    react,
    "react/jsx-runtime": { jsx, jsxs: jsx },
    "lucide-react": new Proxy({}, { get: (_, name) => name }),
    "../api": { api },
    "../store": { useApp: (select) => select(model), useChat: { getState: () => ({ push() {} }) } },
    "../commands": { targets: (ch) => model.linkOn ? ["A", "B"] : [ch], doResume: async () => { calls.push(["resume"]); } },
  };
  function load(name) {
    const filename = path.join(__dirname, "../src/components", name + ".tsx");
    const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText;
    const module = { exports: {} };
    vm.runInNewContext(code, { exports: module.exports, module, require: (id) => dependencies[id] }, { filename });
    return module.exports.default;
  }
  function render(component, props = {}, key = component) {
    hooks = scopes.get(key) || [];
    scopes.set(key, hooks);
    cursor = 0;
    return component(props);
  }
  return { calls, model, render, Device: load("AndroidDevice"), Waves: load("AndroidWaves") };
}

function all(node, predicate) {
  if (!node || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap((child) => all(child, predicate));
  return [...(predicate(node) ? [node] : []), ...all(node.props?.children, predicate)];
}
const find = (tree, predicate) => { const found = all(tree, predicate)[0]; assert.ok(found, "Expected control exists"); return found; };
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("manual +/- and clear preserve independent and linked A/B targets", async () => {
  const h = harness();
  let tree = h.render(h.Device, { onPair() {} });
  const minus = find(tree, (node) => node.props?.["aria-label"] === "减弱 A 通道");
  assert.equal(typeof minus.props.children, "object");
  minus.props.onClick();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls)), [["manual", { op: "add_strength", channel: "A", delta: -10 }]]);
  h.model.linkOn = true;
  h.calls.length = 0;
  tree = h.render(h.Device, { onPair() {} });
  find(tree, (node) => node.props?.["aria-label"] === "增强 B 通道").props.onClick();
  await flush();
  assert.deepEqual(h.calls.map((call) => call[1].channel), ["A", "B"]);
  assert.ok(h.calls.every((call) => call[1].op === "add_strength" && call[1].delta === 10));
  h.calls.length = 0;
  tree = h.render(h.Device, { onPair() {} });
  find(tree, (node) => node.props?.["aria-label"] === "停止 A/B 通道并清零").props.onClick();
  await flush();
  assert.deepEqual(h.calls.map((call) => [call[1].op, call[1].channel]), [["clear", "A"], ["clear", "B"]]);
});

test("wave selection is inert until explicit playback", async () => {
  const h = harness();
  let tree = h.render(h.Device, { onPair() {} });
  find(tree, (node) => node.type === "select" && node.props.id === "android-wave-A").props.onChange({ target: { value: "导入波形" } });
  assert.equal(h.calls.length, 0);
  tree = h.render(h.Device, { onPair() {} });
  find(tree, (node) => node.type === "button" && JSON.stringify(node.props.children) === JSON.stringify(["立即播放到 ", "A"])).props.onClick();
  await flush();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][1].pattern, "导入波形");
  assert.equal(h.calls[0][1].channel, "A");
});

test("numeric caps validate integers against backend bounds without touching the other channel", async () => {
  const h = harness();
  const tree = h.render(h.Device, { onPair() {} });
  const cap = find(tree, (node) => typeof node.type === "function" && node.type.name === "CapInput" && node.props.ch === "A");
  const renderCap = () => h.render(cap.type, cap.props, "cap-A");
  const input = () => find(renderCap(), (node) => node.type === "input");
  assert.equal(input().props.type, "number");
  assert.equal(input().props.min, 1);
  assert.equal(input().props.max, 200);
  assert.equal(input().props.value, "100"); // User cap, not the App-constrained effective cap.
  for (const value of ["", "0", "2.5", "201"]) {
    input().props.onChange({ target: { value } });
    input().props.onBlur();
    await flush();
    assert.equal(h.calls.length, 0);
  }
  input().props.onChange({ target: { value: "25" } });
  input().props.onBlur();
  await flush();
  assert.deepEqual(h.calls, [["cap", "A", 25]]);
});

test("unlock sends one resume request so it cannot chain past a later estop", async () => {
  const h = harness();
  h.model.state.estop = true;
  const tree = h.render(h.Device, { onPair() {} });
  find(tree, (node) => node.type === "button" && node.props.children === "解除急停").props.onClick();
  await flush();
  assert.deepEqual(h.calls, [["resume"]]);
});

test("wave catalog is searchable and browsing sends no commands", () => {
  const h = harness();
  let tree = h.render(h.Waves, { onManual() {} });
  const search = find(tree, (node) => node.type === "input");
  search.props.onChange({ target: { value: "导入" } });
  tree = h.render(h.Waves, { onManual() {} });
  assert.deepEqual(all(tree, (node) => node.type === "strong").map((node) => node.props.children), ["导入波形"]);
  assert.equal(h.calls.length, 0);
});
