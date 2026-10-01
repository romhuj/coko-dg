// Real browser UI with mock API/native bridges only; no models, microphone or hardware.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { build } = require('esbuild');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_DIR ? path.join(process.env.PLAYWRIGHT_MODULE_DIR, 'playwright') : 'playwright');
const root = path.resolve(__dirname, '../..');
async function main() {
  // Share the existing complete Android state fixture, but use the current source tree.
  let contents = fs.readFileSync(path.join(__dirname, 'android-ui-smoke.cjs'), 'utf8').match(/contents: `([\s\S]*?)`, loader:/)[1];
  contents = contents.replace('platform: "android",', 'platform: "android", chat_session_id:"archive-service", conversation_id:"one", conversation_title:"第一次聊天", model_judgment:false,');
  contents = contents.replace('config_info:{version:', 'config_info:{player_nick:"玩家",version:');
  contents = contents.replace('useApp.setState({state:initial});', `
    useApp.setState({state:initial});
    window.speechPosts=[]; window.voicePosts=[]; window.pairPosts=[];
    window.CoyoteSpeech={onmessage:null,postMessage:raw=>window.speechPosts.push(JSON.parse(raw))};
    window.CoyoteVoice={onmessage:null,postMessage:raw=>window.voicePosts.push(JSON.parse(raw))};
    window.CoyotePairing={onmessage:null,postMessage:raw=>{const cmd=JSON.parse(raw);window.pairPosts.push(cmd);queueMicrotask(()=>window.CoyotePairing.onmessage?.({data:JSON.stringify({type:'result',requestId:cmd.requestId,status:cmd.type==='copyAndOpen'?'opened':'copied',copied:true,opened:cmd.type==='copyAndOpen',message:cmd.type==='copyAndOpen'?'已复制并打开 DG-LAB4':'配对链接已复制'})}));}};
  `);
  contents = contents.replace('getState:()=>useApp.getState().state,', 'getState:()=>useApp.getState().state, getChat:()=>useChat.getState(), setChat:patch=>useChat.setState(patch),');
  contents = contents.replace('window.WebSocket = class { close() {} };', 'window.WebSocket = class { constructor(){window.testSocket=this;} close() {} };');
  const bundle = await build({ stdin: { contents, loader: 'tsx', resolveDir: path.join(root, 'frontend') }, bundle: true, write: false, format: 'iife', loader: { '.png': 'dataurl' }, define: { 'process.env.NODE_ENV': '"development"' } });
  const assets = path.join(root, 'frontend/dist/assets');
  const sourceCss = fs.readFileSync(path.join(root, 'frontend/src/styles.css'), 'utf8');
  const css = fs.readFileSync(path.join(assets, fs.readdirSync(assets).find((name) => name.endsWith('.css'))), 'utf8') + sourceCss.slice(sourceCss.indexOf('/* Android v5.'));
  const server = http.createServer((req, res) => { res.setHeader('Content-Type', req.url === '/app.js' ? 'application/javascript' : 'text/html;charset=utf-8'); res.end(req.url === '/app.js' ? bundle.outputFiles[0].text : '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>' + css + '</style><div id="root"></div><script src="/app.js"></script>'); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 400, height: 860 }, isMobile: true, hasTouch: true });
    const errors = [], calls = [], chats = [], judgment = [];
    page.on('pageerror', (error) => errors.push(String(error)));
    const messages = { one: [{ id: '1', conversation_id: 'one', role: 'ai', text: '已保存的回复', created_at: 1 }], two: [{ id: '2', conversation_id: 'two', role: 'user', text: '旧聊天中的问题', created_at: 2 }, { id: '3', conversation_id: 'two', role: 'ai', text: '旧聊天中的回复', created_at: 3 }], trash: [{ id: '4', conversation_id: 'trash', role: 'user', text: '删除测试', created_at: 4 }] };
    const titles = { one: '第一次聊天', two: '旧聊天', trash: '待删除的聊天' }, pinned = {}; let active = 'one', nextId = 10, prepareFailure = true, preparedAt = 0, deleteFailure = false;
    const transcript = (id) => ({ conversation_id: id, title: titles[id], messages: messages[id], has_more: false });
    await page.route('**/api/**', async (route) => {
      const request = route.request(), url = new URL(request.url()), body = request.method() === 'POST' ? request.postDataJSON() : null;
      calls.push({ path: url.pathname, body, method: request.method() });
      if (url.pathname === '/api/state') return route.fulfill({ json: await page.evaluate(() => window.fixture.getState()) });
      if (url.pathname === '/api/settings/llm') return route.fulfill({ json: { has_key: true, saved: true, api_key_masked: '', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', json_mode: true } });
      if (url.pathname === '/api/qrcode.png') return route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlSAAAAAASUVORK5CYII=', 'base64') });
      if (url.pathname === '/api/pair_url') return route.fulfill({ json: { url: 'https://dungeon-lab.cn/s/?v=1&action=socket&url=wss%3A%2F%2Ftrex.dungeon-lab.cn%2Fv4%3Ftid%3Dandroid-controller' } });
      if (url.pathname === '/api/character/nick') { await page.evaluate((nick) => { const state = window.fixture.getState(); window.fixture.setState({ config_info: { ...state.config_info, player_nick: nick } }); }, body.nick); return route.fulfill({ json: { ok: true } }); }
      if (url.pathname === '/api/conversations' && request.method() === 'GET') return route.fulfill({ json: { active_id: active, conversations: Object.keys(messages).reverse().sort((a, b) => Number(!!pinned[b]) - Number(!!pinned[a])).map((id) => ({ id, title: titles[id], pinned: !!pinned[id], message_count: messages[id].length, created_at: 1, updated_at: 1 })), has_more: false } });
      if (url.pathname === '/api/conversations' && body) { active = 'new'; messages.new = []; titles.new = '新聊天'; return route.fulfill({ json: transcript(active) }); }
      const pin = url.pathname.match(/^\/api\/conversations\/([^/]+)\/pin$/);
      if (pin) { pinned[pin[1]] = body.pinned; return route.fulfill({ json: { conversation_id: pin[1], pinned: body.pinned, active_id: active } }); }
      const deletion = url.pathname.match(/^\/api\/conversations\/([^/]+)$/);
      if (deletion && request.method() === 'DELETE') {
        if (deleteFailure) { deleteFailure = false; return route.fulfill({ status: 503, json: { detail: '设备停止未确认，聊天记录已保留' } }); }
        const id = deletion[1], changed = active === id; delete messages[id]; delete titles[id];
        if (changed) { active = 'after-delete'; messages[active] = []; titles[active] = '新聊天'; await page.evaluate((id) => window.fixture.setState({ conversation_id: id, conversation_title: '新聊天', estop: true, autopilot: false }), active); }
        return route.fulfill({ json: { deleted_id: id, active_id: active, active_changed: changed, conversation: changed ? transcript(active) : null } });
      }
      const match = url.pathname.match(/^\/api\/conversations\/([^/]+)\/(messages|select)$/);
      if (match) { if (match[2] === 'select') active = match[1]; return route.fulfill({ json: transcript(match[1]) }); }
      if (url.pathname === '/api/character/model-judgment/prepare') { if (prepareFailure) { prepareFailure = false; return route.fulfill({ status: 503, json: { error: '模拟准备失败' } }); } preparedAt = Date.now(); return route.fulfill({ json: { confirmation_token: 'confirmation-' + preparedAt, wait_seconds: 5 } }); }
      if (url.pathname === '/api/character/model-judgment') { judgment.push(body); if (body.enabled) assert(Date.now() - preparedAt >= 5000); return route.fulfill({ json: { ok: true, model_judgment: body.enabled } }); }
      if (url.pathname === '/api/estop') { await page.evaluate(() => window.fixture.setState({ estop: true, autopilot: false })); return route.fulfill({ json: { estop: true, sent: true } }); }
      if (url.pathname === '/api/chat') {
        chats.push(body); assert.equal(body.conversation_id, active);
        const turn = [{ id: String(nextId++), conversation_id: active, role: 'user', text: body.message, created_at: nextId }, { id: String(nextId++), conversation_id: active, role: 'ai', text: '新的实时回复', speechText: '新的实时回复', created_at: nextId, executed: [{ sent: true, status: 'sent', command: { kind: 'pulse_hold', channel: 'A', pattern: '挤压' } }, { sent: true, status: 'confirmed', command: { kind: 'hold', channel: 'A', value: 56 } }], dropped: [] }];
        messages[active].push(...turn);
        await page.evaluate((data) => window.testSocket.onmessage({ data: JSON.stringify(data) }), { type: 'chat', conversation_id: active, messages: turn, line: '新的实时回复', executed: [], dropped: [] });
        return route.fulfill({ json: { status: 'completed', request_id: body.request_id, result: { line: '新的实时回复', executed: turn[1].executed, dropped: [], conversation_id: active, messages: turn } } });
      }
      throw new Error('Unexpected mocked API: ' + url.pathname);
    });
    const output = process.env.ANDROID_UI_SCREENSHOT_DIR;
    const capture = async (name) => { if (output) { fs.mkdirSync(output, { recursive: true }); await page.screenshot({ path: path.join(output, name + '.png') }); } assert.equal(await page.evaluate(() => document.body.scrollWidth), 400); };
    await page.goto('http://127.0.0.1:' + server.address().port);
    const stop = page.getByRole('button', { name: '急停：停止输出并清零' });
    const pair = page.getByRole('dialog', { name: '连接 DG-LAB4' }); await pair.waitFor();
    await page.getByRole('img', { name: 'DG-LAB4 配对二维码' }).waitFor();
    await capture('pairing-400'); assert(await stop.isVisible());
    await page.getByRole('button', { name: '复制链接', exact: true }).click(); await page.getByText('配对链接已复制', { exact: true }).waitFor();
    await page.getByRole('button', { name: '复制并打开 DG-LAB4', exact: true }).click(); await page.getByText('已复制并打开 DG-LAB4', { exact: true }).waitFor();
    assert.deepEqual(await page.evaluate(() => window.pairPosts.map((p) => p.type)), ['copy', 'copyAndOpen']);
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.reload(); await page.getByText('已保存的回复', { exact: true }).waitFor(); assert.equal(await pair.count(), 0);
    assert.equal(await page.evaluate(() => window.speechPosts.length), 0, 'restored history is not spoken');
    await page.getByRole('button', { name: '打开菜单', exact: true }).click();
    await page.getByRole('button', { name: /我的名称/ }).click();
    await page.getByLabel('名称', { exact: true }).fill(''); await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByText('请输入 1–20 字的名称').waitFor();
    await page.getByLabel('名称', { exact: true }).fill('旅行者'); await capture('nickname-400'); await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByRole('button', { name: '打开菜单', exact: true }).click();
    await page.getByText('旅行者', { exact: true }).waitFor(); await capture('menu-history-400');
    await page.getByRole('heading', { name: 'coko DG', exact: true }).waitFor();
    const oldRow = page.locator('[data-conversation-id="two"]');
    const point = await oldRow.boundingBox(); await page.mouse.move(point.x + 30, point.y + 20); await page.mouse.down();
    await page.getByRole('dialog', { name: '管理聊天', exact: true }).waitFor(); await page.mouse.up();
    assert.equal(active, 'one', 'long-press release must not select the conversation');
    assert(await stop.isVisible()); assert.equal(await page.locator('[data-coyote-sidebar]').getAttribute('inert'), '');
    await page.getByRole('button', { name: '置顶', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.android-conversation-row')?.dataset.conversationId === 'two');
    await oldRow.focus(); await page.keyboard.press('Shift+F10'); await page.getByRole('button', { name: '取消置顶', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[data-conversation-id="two"] [aria-label="已置顶"]'));
    await page.locator('[data-conversation-id="trash"]').click({ button: 'right' }); await page.getByRole('button', { name: '删除', exact: true }).click();
    await page.getByRole('button', { name: '取消', exact: true }).click(); assert(messages.trash, 'cancel preserves the chat');
    await page.evaluate(() => window.fixture.setChat({ busy: true, awaitingConfirmation: true }));
    await page.locator('[data-conversation-id="trash"]').click({ button: 'right' }); await page.getByRole('button', { name: '删除', exact: true }).click();
    await capture('chat-delete-400'); await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await page.locator('[data-conversation-id="trash"]').waitFor({ state: 'detached' });
    assert.equal(active, 'one'); assert.equal(await page.evaluate(() => window.fixture.getChat().busy), true, 'deleting inactive history preserves a pending turn');
    await page.evaluate(() => window.fixture.setChat({ busy: false, awaitingConfirmation: false }));
    await page.getByRole('button', { name: '设置', exact: true }).click();
    await page.getByLabel('API Key', { exact: true }).fill('unsaved-local-test');
    await page.getByRole('button', { name: '关于', exact: true }).click();
    await page.getByRole('heading', { name: '关于', exact: true }).waitFor();
    assert.equal(await page.getByRole('navigation', { name: '主要视图' }).count(), 0);
    assert.equal(await page.getByRole('button', { name: '打开菜单', exact: true }).count(), 0);
    assert(await stop.isVisible()); assert.equal(await page.locator('.android-about a').count(), 2);
    for (const [label, href] of [['反馈', 'https://x.com/_Good_Dick_'], ['源仓库', 'https://github.com/romhuj/coko-dg']]) {
      const link = page.getByRole('link', { name: label + '（外部浏览器）', exact: true });
      assert.equal(await link.getAttribute('href'), href); assert.equal(await link.getAttribute('target'), '_blank');
      assert.match(await link.getAttribute('rel'), /noopener/);
    }
    await capture('about-400'); assert.equal(await page.evaluate(() => window.coyoteHandleBack()), true);
    assert.equal(await page.getByLabel('API Key', { exact: true }).inputValue(), 'unsaved-local-test', 'visiting About preserves unsaved settings');
    await page.getByRole('button', { name: '返回聊天', exact: true }).click();
    await page.getByRole('button', { name: '打开菜单', exact: true }).click();
    await page.evaluate(() => window.fixture.setChat({ busy: true, awaitingConfirmation: true }));
    const before = calls.filter((c) => c.path === '/api/conversations' && c.body).length;
    await page.getByRole('button', { name: '新建聊天', exact: true }).click();
    await page.getByText('请先等待当前消息完成，或确认上次请求，再切换聊天').waitFor();
    assert.equal(calls.filter((c) => c.path === '/api/conversations' && c.body).length, before);
    assert.equal(await page.evaluate(() => window.fixture.getChat().busy), true);
    await page.evaluate(() => window.fixture.setChat({ busy: false, awaitingConfirmation: false }));
    await page.getByRole('button', { name: '新建聊天', exact: true }).click();
    const input = page.getByRole('textbox', { name: '聊天消息' }); await input.waitFor();
    await page.waitForFunction(() => window.fixture.getState().conversation_id === 'new');
    assert.equal(await page.getByText('已保存的回复', { exact: true }).count(), 0);
    assert.equal(await page.evaluate(() => window.fixture.getState().config_info.player_nick), '旅行者');
    await input.fill('新聊天的草稿');
    await page.getByRole('button', { name: '打开菜单', exact: true }).click(); await page.getByRole('button', { name: /旧聊天.*2 条消息/ }).click();
    await page.getByText('旧聊天中的回复', { exact: true }).waitFor(); assert.equal(await input.inputValue(), '');
    await page.getByRole('button', { name: '开启回复朗读' }).click();
    await page.evaluate(() => window.CoyoteSpeech.onmessage({ data: JSON.stringify({ type: 'state', status: 'ready', sessionId: window.speechPosts.find((p) => p.type === 'enable').sessionId }) }));
    await input.fill('我的问题'); await page.getByRole('button', { name: '发送消息', exact: true }).click();
    await page.getByText('新的实时回复', { exact: true }).waitFor();
    assert.equal(await page.getByText('新的实时回复', { exact: true }).count(), 1); assert.equal(chats.length, 1);
    await page.getByText('A 挤压 循环', { exact: true }).waitFor(); await page.getByText('A 强度 56', { exact: true }).waitFor();
    assert.deepEqual(await page.evaluate(() => window.speechPosts.filter((p) => p.type === 'speak').map((p) => p.text)), ['新的实时回复']); await capture('receipts-history-400');
    await page.getByRole('button', { name: '打开菜单', exact: true }).click();
    assert.equal(await page.evaluate(() => window.speechPosts.at(-1).type), 'disable');
    await page.getByRole('button', { name: /新聊天.*0 条消息/ }).click();
    assert.equal(await input.inputValue(), '新聊天的草稿'); assert.equal(chats.length, 1, 'restoring history never replays actions');
    await page.locator('.android-role-context').click();
    const model = page.getByRole('switch', { name: '模型自判断' }); assert.equal(await model.getAttribute('aria-checked'), 'false');
    await model.click(); await page.getByText('模拟准备失败', { exact: true }).waitFor(); assert.equal(judgment.length, 0);
    await page.getByRole('button', { name: '重新准备', exact: true }).click();
    const confirm = page.getByRole('button', { name: /确认开启/ }); assert(await confirm.isDisabled()); await capture('model-judgment-countdown-400');
    await page.getByRole('button', { name: '取消', exact: true }).click(); assert.equal(judgment.length, 0);
    await model.click(); await page.getByRole('button', { name: '确认开启（5秒）', exact: true }).waitFor(); assert(await confirm.isDisabled());
    await page.waitForFunction(() => [...document.querySelectorAll('button')].some((b) => b.textContent === '确认开启' && !b.disabled), { timeout: 8000 });
    await confirm.click(); await page.waitForFunction(() => document.querySelector('[aria-label="模型自判断"]')?.getAttribute('aria-checked') === 'true');
    assert.equal(judgment.length, 1); assert.equal(judgment[0].enabled, true);
    await page.evaluate(() => window.fixture.setChat({ busy: true }));
    assert.equal(await model.isDisabled(), false, 'enabled judgment can be turned off during a model reply');
    await model.click(); await page.waitForFunction(() => document.querySelector('[aria-label="模型自判断"]')?.getAttribute('aria-checked') === 'false');
    assert.equal(judgment.length, 2); assert.deepEqual(judgment[1], { enabled: false });
    await page.evaluate(() => window.fixture.setChat({ busy: false }));
    await capture('roles-judgment-400');
    await page.getByRole('navigation', { name: '主要视图' }).getByRole('button', { name: '设备', exact: true }).click();
    await page.getByRole('button', { name: '配对', exact: true }).click(); await pair.waitFor();
    await page.evaluate(() => { const state = window.fixture.getState(); window.fixture.setState({ relay: { ...state.relay, status: 'paired' } }); }); await pair.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '打开菜单', exact: true }).click();
    await page.locator('[data-conversation-id="new"]').click({ button: 'right' }); await page.getByRole('button', { name: '删除', exact: true }).click();
    await page.evaluate(() => window.fixture.setChat({ awaitingConfirmation: true }));
    const deletes = calls.filter((call) => call.method === 'DELETE').length;
    await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await page.getByText('请先等待当前消息完成，或确认上次请求，再删除当前聊天', { exact: true }).waitFor();
    assert.equal(calls.filter((call) => call.method === 'DELETE').length, deletes);
    await page.evaluate(() => window.fixture.setChat({ awaitingConfirmation: false })); deleteFailure = true;
    await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await page.getByText('设备停止未确认，聊天记录已保留', { exact: true }).waitFor(); assert(messages.new);
    await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await page.waitForFunction(() => window.fixture.getChat().conversationId === 'after-delete');
    assert.equal(messages.new, undefined); assert.equal(await input.inputValue(), '');
    assert(await stop.isVisible()); assert.deepEqual(errors, []);
    console.log('PASS: 400px pairing/nickname/archive/receipts/judgment regressions; coko DG menu; long-press+keyboard+context menu pin/unpin; cancel/noncurrent/current deletion and pending/failure guards; standalone About/external links/back/unsaved settings/global stop.');
  } finally { await browser.close(); await new Promise((resolve) => server.close(resolve)); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
