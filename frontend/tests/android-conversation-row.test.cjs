const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const test = require('node:test'), ts = require('typescript');

function fixture() {
  const hooks = [], timers = new Map(), effects = [], calls = [];
  let cursor = 0, serial = 0;
  const react = { useRef: (value) => hooks[cursor++] ??= { current: value }, useEffect: (fn) => { effects.push(fn()); } };
  const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/components/AndroidConversationRow.tsx'), 'utf8'), { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText;
  vm.runInNewContext(code, { exports: module.exports, module, require: (id) => id === 'react' ? react : id === 'lucide-react' ? { Pin: 'Pin' } : { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) }, window: { setTimeout(fn) { const id = ++serial; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); } } });
  const render = (overrides = {}) => { cursor = 0; return module.exports.default({ chat: { id: 'one', title: '聊天', message_count: 2 }, active: false, disabled: false, onSelect() { calls.push('select'); }, onManage() { calls.push('manage'); }, ...overrides }); };
  return { render, calls, timers, effects, expire() { const scheduled = [...timers.values()]; timers.clear(); scheduled.forEach((fn) => fn()); } };
}
const pointer = (x = 20, y = 20, extra = {}) => ({ pointerId: 1, isPrimary: true, button: 0, clientX: x, clientY: y, ...extra });
const click = () => ({ prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} });

test('long press opens management once and consumes the release click without selecting', () => {
  const f = fixture(), row = f.render(); row.props.onPointerDown(pointer()); f.expire();
  row.props.onPointerUp(); const event = click(); row.props.onClick(event);
  assert.deepEqual(f.calls, ['manage']); assert.equal(event.prevented, true);
  row.props.onPointerDown(pointer()); row.props.onPointerUp(); row.props.onClick(click());
  assert.deepEqual(f.calls, ['manage', 'select'], 'the next deliberate tap selects normally');
});
test('vertical scrolling, pointer cancellation, and unmount cancel pending long presses', () => {
  for (const cancel of [(row) => row.props.onPointerMove(pointer(22, 45)), (row) => row.props.onPointerCancel(), (_row, f) => f.effects.forEach((dispose) => dispose())]) {
    const f = fixture(), row = f.render(); row.props.onPointerDown(pointer()); cancel(row, f); f.expire();
    assert.deepEqual(f.calls, []); assert.equal(f.timers.size, 0);
  }
});
test('a secondary touch or non-primary mouse button cannot arm a long press', () => {
  const f = fixture(), row = f.render(); row.props.onPointerDown(pointer(20, 20, { isPrimary: false })); f.expire();
  row.props.onPointerDown(pointer(20, 20, { button: 2 })); f.expire(); assert.deepEqual(f.calls, []);
});
test('context menu and keyboard context-menu keys offer the same actions without selection', () => {
  for (const trigger of [(row, event) => row.props.onContextMenu(event), (row, event) => row.props.onKeyDown({ ...event, key: 'F10', shiftKey: true }), (row, event) => row.props.onKeyDown({ ...event, key: 'ContextMenu' })]) {
    const f = fixture(), row = f.render(); trigger(row, click());
    assert.deepEqual(f.calls, ['manage']); assert.equal(row.props['aria-haspopup'], 'dialog');
  }
});
test('disabling a row during a pending long press cancels the timer', () => {
  const f = fixture(); f.render().props.onPointerDown(pointer()); f.render({ disabled: true }); f.expire();
  assert.deepEqual(f.calls, []);
});
