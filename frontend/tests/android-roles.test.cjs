const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function harness() {
  const calls = [], scopes = new Map(), cache = new Map();
  let hooks, cursor;
  const custom = "custom_" + "a".repeat(32);
  const role = (name, label, manageable, pinned = false) => ({ name, label, manageable, pinned, profiles: [{ name: "默认", available: true }] });
  const model = { state: { platform: "android", role: custom, profile: "默认", intensity_level: "中", roles: [role("assistant", "情景助手", false), role(custom, "测试角色", true), role("custom_" + "b".repeat(32), "置顶角色", true, true)] } };
  const chat = { busy: false, clear() { calls.push(["clearChat"]); } };
  const useApp = Object.assign((select) => select(model), { getState: () => model, setState: (value) => Object.assign(model, value) });
  const useChat = Object.assign((select) => select(chat), { getState: () => chat });
  const api = {
    async state() { return model.state; },
    async pinCharacter(name, pinned) { calls.push(["pin", name, pinned]); model.state.roles.find((item) => item.name === name).pinned = pinned; return { ok: true, role: name, pinned }; },
    async deleteCharacter(name) { calls.push(["delete", name]); model.state.roles = model.state.roles.filter((item) => item.name !== name); if (model.state.role === name) model.state.role = "assistant"; return { ok: true, deleted_role: name, role: model.state.role, profile: "默认" }; },
    async createCustomCharacter(name, personality, background, voiceId) { calls.push(["create", name, personality, background, voiceId]); return { ok: true, role: custom, profile: "角色扮演" }; },
    async searchCharacter(query) { calls.push(["search", query]); return { search_id: "mock-search", sources: [{ title: "旅人", summary: "A character", url: "https://example.invalid", provider: "Test", language: "zh" }], warnings: [] }; },
    async createCharacter(searchId, sourceIndex, name, note, voiceId) { calls.push(["searchCreate", searchId, sourceIndex, name, note, voiceId]); return { ok: true, role: custom, profile: "角色扮演" }; },
  };
  const react = {
    useState(initial) { const values = hooks, index = cursor++; if (!(index in values)) values[index] = typeof initial === "function" ? initial() : initial; return [values[index], (value) => { values[index] = typeof value === "function" ? value(values[index]) : value; }]; },
    useRef(initial) { const index = cursor++; return hooks[index] ??= { current: initial }; },
    useEffect() { cursor++; },
  };
  const jsx = (type, props) => ({ type, props });
  const deps = {
    react, "react/jsx-runtime": { jsx, jsxs: jsx }, "react-dom": { createPortal: (element) => element },
    "lucide-react": new Proxy({}, { get: (_, key) => key }), "../api": { api }, "../store": { useApp, useChat },
    "../i18n": { useT: () => (text) => text },
    "../roleTheme": { ENTRIES: [], INTENSITY_LEVELS: ["低", "中", "高", "极高", "最高", "炼狱"], entryOf: () => null },
    "./RoleSearch": { default: () => null },
  };
  function loadModule(filename) {
    if (cache.has(filename)) return cache.get(filename);
    const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText;
    const module = { exports: {} };
    vm.runInNewContext(code, { exports: module.exports, module, require: (id) => {
      if (deps[id]) return deps[id];
      const base = path.resolve(path.dirname(filename), id);
      return loadModule(base + (fs.existsSync(base + ".tsx") ? ".tsx" : ".ts"));
    }, window: { localStorage: { getItem: () => null } }, document: { body: {} }, Date }, { filename });
    cache.set(filename, module.exports);
    return module.exports;
  }
  const load = (name) => loadModule(path.join(__dirname, "../src/components", name + ".tsx")).default;
  function render(component, props, key = component) { hooks = scopes.get(key) || []; scopes.set(key, hooks); cursor = 0; return component(props); }
  return { calls, custom, model, api, render, Card: load("RoleCard"), Row: load("AndroidRoleRow"), Dialog: load("AndroidRoleDeleteDialog"), Creator: load("AndroidRoleCreateDialog"), Search: load("RoleSearch"), VoiceSelect: load("RoleVoiceSelect") };
}
function all(node, predicate) {
  if (!node || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap((child) => all(child, predicate));
  return [...(predicate(node) ? [node] : []), ...all(node.props?.children, predicate)];
}
const find = (tree, predicate) => { const value = all(tree, predicate)[0]; assert.ok(value, "Expected UI control exists"); return value; };
const flush = () => new Promise((resolve) => setImmediate(resolve));
const pointer = (x, y) => ({ clientX: x, clientY: y, isPrimary: true, pointerType: "touch", button: 0, stopPropagation() {} });
const rowProps = () => ({ label: "测试角色", description: "联网角色", active: true, disabled: false, manageable: true, pinned: false, open: false, onOpenChange() {}, onSelect() {}, onPin() {}, onDelete() {} });

test("left swipe reveals actions without selecting or accidentally activating the row", () => {
  const h = harness(), changes = [];
  const tree = h.render(h.Row, { ...rowProps(), onOpenChange: (open) => changes.push(open) });
  tree.props.onPointerDown(pointer(300, 40));
  tree.props.onPointerMove(pointer(150, 44));
  tree.props.onPointerUp(pointer(150, 44));
  assert.deepEqual(changes, [true]);
  let prevented = false;
  tree.props.onClickCapture({ preventDefault() { prevented = true; }, stopPropagation() {} });
  assert.equal(prevented, true);
  assert.equal(h.calls.length, 0);
});

test("right swipe closes actions; vertical scrolling and built-in rows expose none", () => {
  const h = harness(), changes = [];
  let tree = h.render(h.Row, { ...rowProps(), open: true, onOpenChange: (open) => changes.push(open) });
  tree.props.onPointerDown(pointer(100, 20)); tree.props.onPointerMove(pointer(240, 22));
  assert.deepEqual(changes, [false]);
  changes.length = 0;
  tree.props.onPointerDown(pointer(100, 20)); tree.props.onPointerMove(pointer(110, 110)); tree.props.onPointerMove(pointer(200, 120));
  assert.deepEqual(changes, []);
  tree = h.render(h.Row, { ...rowProps(), manageable: false });
  assert.equal(all(tree, (node) => node.props?.className === "android-role-actions").length, 0);
});

test("pinned roles sort first; pin action updates only the chosen role", async () => {
  const h = harness();
  let tree = h.render(h.Card, {});
  let rows = all(tree, (node) => node.type === h.Row);
  assert.equal(rows[0].props.label, "置顶角色");
  assert.equal(rows.find((node) => node.props.label === "情景助手").props.manageable, false);
  rows.find((node) => node.props.label === "测试角色").props.onPin();
  await flush();
  assert.deepEqual(h.calls, [["pin", h.custom, true]]);
  tree = h.render(h.Card, {}); rows = all(tree, (node) => node.type === h.Row);
  assert.equal(rows.find((node) => node.props.label === "测试角色").props.pinned, true);
});

test("delete cancel sends nothing; guarded confirm preserves archived chat for server conversation switch", async () => {
  const h = harness();
  const render = () => h.render(h.Card, {});
  const currentRow = () => find(render(), (node) => node.type === h.Row && node.props.label === "测试角色");
  currentRow().props.onDelete();
  let dialog = find(render(), (node) => node.type === h.Dialog);
  assert.equal(dialog.props.active, true);
  dialog.props.onCancel();
  assert.equal(all(render(), (node) => node.type === h.Dialog).length, 0);
  assert.equal(h.calls.length, 0);
  currentRow().props.onDelete();
  dialog = find(render(), (node) => node.type === h.Dialog);
  dialog.props.onConfirm(); dialog.props.onConfirm();
  await flush();
  assert.deepEqual(h.calls, [["delete", h.custom]]);
  assert.equal(all(render(), (node) => node.type === h.Dialog).length, 0);
});

test("deletion errors refresh a server-side fallback instead of leaving the old role chat", async () => {
  const h = harness();
  h.api.deleteCharacter = async () => { h.model.state.role = "assistant"; throw new Error("mock disk failure"); };
  find(h.render(h.Card, {}), (node) => node.type === h.Row && node.props.label === "测试角色").props.onDelete();
  find(h.render(h.Card, {}), (node) => node.type === h.Dialog).props.onConfirm();
  await flush();
  const dialog = find(h.render(h.Card, {}), (node) => node.type === h.Dialog);
  assert.match(dialog.props.error, /mock disk failure/);
  assert.deepEqual(h.calls, []);
  assert.equal(h.model.state.role, "assistant");
});

test("delete confirmation is an accessible React dialog, keeps global stop outside its overlay", () => {
  const h = harness();
  const tree = h.render(h.Dialog, { label: "测试角色", active: true, busy: false, deleted: false, error: "", onCancel() {}, onConfirm() {} });
  const dialog = find(tree, (node) => node.props?.role === "dialog");
  assert.equal(dialog.props["aria-modal"], false);
  assert.ok(dialog.props["aria-labelledby"]);
  assert.match(find(tree, (node) => node.props?.id === "android-delete-role-description").props.children, /停止设备输出和自动运行/);
});

test("create entry opens two choices and search delegates without a network request", () => {
  const h = harness();
  find(h.render(h.Card, {}), (node) => node.props?.className === "android-role-search").props.onClick();
  assert.ok(find(h.render(h.Card, {}), (node) => node.type === h.Creator));
  let searches = 0;
  const tree = h.render(h.Creator, { onSearch() { searches++; }, onClose() {}, onCreated() {} });
  assert.ok(find(tree, (node) => node.props?.["data-role-create-option"] === "custom"));
  find(tree, (node) => node.props?.["data-role-create-option"] === "search").props.onClick();
  assert.equal(searches, 1);
  assert.equal(h.calls.length, 0);
});

test("custom role form validates identity and submits one guarded creation", async () => {
  const h = harness();
  let created = 0;
  const props = { onSearch() {}, onClose() {}, onCreated() { created++; } };
  const render = () => h.render(h.Creator, props);
  find(render(), (node) => node.props?.["data-role-create-option"] === "custom").props.onClick();
  find(render(), (node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  await flush();
  assert.equal(h.calls.length, 0);
  assert.ok(find(render(), (node) => node.props?.role === "alert"));
  for (const [id, value] of [["role-custom-name", " 旅人 "], ["role-custom-personality", "冷静并保持自己的判断"], ["role-custom-background", "来自安静的小镇"]]) {
    find(render(), (node) => node.props?.id === id).props.onChange({ target: { value } });
  }
  const form = find(render(), (node) => node.type === "form");
  form.props.onSubmit({ preventDefault() {} }); form.props.onSubmit({ preventDefault() {} });
  await flush();
  assert.deepEqual(h.calls, [["create", "旅人", "冷静并保持自己的判断", "来自安静的小镇", "melo-zh"]]);
  assert.equal(created, 1);
});

test("custom creation offers approved voices and submits the selected voice separately from personality", async () => {
  const h = harness(), props = { onSearch() {}, onClose() {}, onCreated() {} };
  const render = () => h.render(h.Creator, props);
  find(render(), (node) => node.props?.["data-role-create-option"] === "custom").props.onClick();
  find(render(), (node) => node.props?.id === "role-custom-name").props.onChange({ target: { value: "旅人" } });
  find(render(), (node) => node.props?.id === "role-custom-personality").props.onChange({ target: { value: "冷静" } });
  const choice = find(render(), (node) => node.type === h.VoiceSelect);
  assert.equal(choice.props.value, "melo-zh");
  const selector = h.render(h.VoiceSelect, choice.props);
  assert.deepEqual(all(selector, (node) => node.type === "option").map((node) => node.props.value), ["kokoro-zf_001", "kokoro-zm_010", "melo-zh"]);
  find(selector, (node) => node.type === "select").props.onChange({ target: { value: "kokoro-zf_001" } });
  find(render(), (node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  await flush();
  assert.deepEqual(h.calls[0], ["create", "旅人", "冷静", "", "kokoro-zf_001"]);
});

test("network role creation retains the selected voice and default desktop does not select new resources", async () => {
  for (const android of [true, false]) {
    const h = harness();
    if (!android) h.model.state.platform = "desktop";
    const render = () => h.render(h.Search, { onClose() {}, onCreated() {} });
    find(render(), (node) => node.props?.id === "role-search-query").props.onChange({ target: { value: "旅人" } });
    find(render(), (node) => node.type === "form").props.onSubmit({ preventDefault() {} }); await flush();
    find(render(), (node) => node.type === "input" && node.props.type === "radio").props.onChange();
    if (android) {
      const choice = find(render(), (node) => node.type === h.VoiceSelect);
      assert.equal(choice.props.value, "melo-zh"); choice.props.onChange("kokoro-zm_010");
    } else assert.equal(all(render(), (node) => node.type === h.VoiceSelect).length, 0);
    const buttons = all(render(), (node) => node.type === "button");
    buttons.at(-1).props.onClick(); await flush();
    assert.deepEqual(h.calls.find((call) => call[0] === "searchCreate"), ["searchCreate", "mock-search", 0, "旅人", "", android ? "kokoro-zm_010" : undefined]);
  }
});

test("successful custom creation retries only state refresh after a refresh failure", async () => {
  const h = harness();
  const render = () => h.render(h.Creator, { onSearch() {}, onClose() {}, onCreated() {} });
  find(render(), (node) => node.props?.["data-role-create-option"] === "custom").props.onClick();
  find(render(), (node) => node.props?.id === "role-custom-name").props.onChange({ target: { value: "旅人" } });
  find(render(), (node) => node.props?.id === "role-custom-personality").props.onChange({ target: { value: "冷静" } });
  h.api.state = async () => { throw new Error("mock refresh failure"); };
  find(render(), (node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  await flush();
  assert.match(find(render(), (node) => node.props?.role === "alert").props.children, /角色已创建/);
  h.api.state = async () => h.model.state;
  find(render(), (node) => node.type === "form").props.onSubmit({ preventDefault() {} });
  await flush();
  assert.equal(h.calls.filter((call) => call[0] === "create").length, 1);
});
