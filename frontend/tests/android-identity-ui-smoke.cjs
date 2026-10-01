// Browser form/gesture/image processing verification with a local mock API only.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { build } = require('esbuild');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_DIR ? path.join(process.env.PLAYWRIGHT_MODULE_DIR, 'playwright') : 'playwright');
const root = path.resolve(__dirname, '../..');
async function main() {
  let contents = fs.readFileSync(path.join(__dirname, 'android-ui-smoke.cjs'), 'utf8').match(/contents: `([\s\S]*?)`, loader:/)[1];
  contents = contents.replace('useApp.setState({state:initial});', `
    initial.roles = [
      {name:'assistant',label:'情景助手',creation_type:'builtin',editable:true,profiles:[{name:'角色扮演',available:true}]},
      {name:'manual',label:'手工角色',creation_type:'manual',editable:true,manageable:true,profiles:[{name:'角色扮演',available:true}]},
      {name:'search',label:'搜索角色',creation_type:'search',editable:true,manageable:true,sources:[{title:'原资料',url:'https://example.invalid'}],profiles:[{name:'角色扮演',available:true}]}
    ];
    useApp.setState({state:initial});
  `);
  contents = contents.replace('getState:()=>useApp.getState().state,', 'getState:()=>useApp.getState().state, getChat:()=>useChat.getState(), setChat:patch=>useChat.setState(patch),');
  const bundle = await build({ stdin: { contents, loader: 'tsx', resolveDir: path.join(root, 'frontend') }, bundle: true, write: false, format: 'iife', loader: { '.png': 'dataurl' }, define: { 'process.env.NODE_ENV': '"development"' } });
  const assets = path.join(root, 'frontend/dist/assets'), sourceCss = fs.readFileSync(path.join(root, 'frontend/src/styles.css'), 'utf8');
  const css = fs.readFileSync(path.join(assets, fs.readdirSync(assets).find((name) => name.endsWith('.css'))), 'utf8') + sourceCss.slice(sourceCss.indexOf('/* Android v5.'));
  const server = http.createServer((req, res) => { res.setHeader('Content-Type', req.url === '/app.js' ? 'application/javascript' : 'text/html;charset=utf-8'); res.end(req.url === '/app.js' ? bundle.outputFiles[0].text : '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>' + css + '</style><div id="root"></div><script src="/app.js"></script>'); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 400, height: 860 }, isMobile: true, hasTouch: true }), errors = [], calls = [], uploads = {};
    page.on('pageerror', (error) => errors.push(String(error)));
    const character = (role, kind, name) => ({ role, kind, name, personality: kind === 'manual' ? '原本冷静' : '', background: kind === 'manual' ? '旧背景' : '', note: kind === 'search' ? '原搜索补充' : '', voiceId: kind === 'manual' ? 'kokoro-zm_010' : 'system-default', avatar_url: null, sources: kind === 'search' ? [{ title: '原资料', url: 'https://example.invalid' }] : [], legacy_prompt_preserved: kind === 'builtin' });
    const roles = { assistant: character('assistant', 'builtin', '情景助手'), manual: character('manual', 'manual', '手工角色'), search: character('search', 'search', '搜索角色') };
    let failEdit = false, avatarVersion = 0;
    await page.route('**/api/**', async (route) => {
      const req = route.request(), url = new URL(req.url()), body = ['POST', 'PUT'].includes(req.method()) ? req.postDataJSON() : null;
      calls.push({ path: url.pathname, method: req.method(), body });
      if (url.pathname === '/api/state') return route.fulfill({ json: await page.evaluate(() => window.fixture.getState()) });
      if (url.pathname === '/api/settings/llm') return route.fulfill({ json: { has_key: true, saved: true, base_url: 'https://api.deepseek.com', model: 'deepseek-flash', json_mode: true } });
      if (url.pathname === '/api/conversations') return route.fulfill({ json: { active_id: '', conversations: [], has_more: false } });
      if (url.pathname === '/api/device/channel-names') {
        const devices = await page.evaluate((names) => { const old = window.fixture.getState().device_channels, next = { A: { ...old.A, name: names.A || 'A 通道' }, B: { ...old.B, name: names.B || 'B 通道' } }; window.fixture.setState({ device_channels: next }); return next; }, body);
        return route.fulfill({ json: { ok: true, device_channels: devices } });
      }
      if (url.pathname === '/api/character/edit' && req.method() === 'GET') return route.fulfill({ json: roles[url.searchParams.get('role')] });
      if (url.pathname === '/api/character/edit' && req.method() === 'PUT') {
        if (failEdit) { failEdit = false; return route.fulfill({ status: 503, json: { detail: '模拟保存失败，旧头像保留' } }); }
        const old = roles[body.role]; assert(old); assert.equal(body.kind, undefined); assert.equal(body.sources, undefined);
        Object.assign(old, { name: body.name, personality: body.personality, background: body.background, note: body.note, voiceId: body.voiceId });
        if (body.avatar_data !== undefined) { uploads[body.role] = body.avatar_data; old.avatar_url = body.avatar_data ? '/api/character/avatar?role=' + body.role + '&v=' + (++avatarVersion) : null; }
        await page.evaluate((info) => { const state = window.fixture.getState(); window.fixture.setState({ roles: state.roles.map((role) => role.name === info.role ? { ...role, label: info.name, voiceId: info.voiceId, avatar_url: info.avatar_url } : role) }); }, old);
        return route.fulfill({ json: { ok: true, character: old } });
      }
      if (url.pathname === '/api/character/avatar') return route.fulfill({ contentType: 'image/jpeg', body: Buffer.from(uploads[url.searchParams.get('role')].split(',')[1], 'base64') });
      if (url.pathname === '/api/character/profile') { await page.evaluate((body) => window.fixture.setState({ role: body.role, profile: body.profile }), body); return route.fulfill({ json: { ok: true } }); }
      if (url.pathname === '/api/character/custom') { assert.match(body.avatar_data, /^data:image\/jpeg;base64,/); assert.equal(body.name, '带头像的新角色'); return route.fulfill({ json: { ok: true, role: 'created', profile: '角色扮演' } }); }
      if (url.pathname === '/api/estop') return route.fulfill({ json: { estop: true, sent: true } });
      throw new Error('Unexpected API: ' + req.method() + ' ' + url.pathname);
    });
    await page.goto('http://127.0.0.1:' + server.address().port);
    const stop = page.getByRole('button', { name: '急停：停止输出并清零' });
    const screenshot = async (name) => { assert.equal(await page.evaluate(() => document.body.scrollWidth), 400); assert(await stop.isVisible()); if (process.env.ANDROID_UI_SCREENSHOT_DIR) { fs.mkdirSync(process.env.ANDROID_UI_SCREENSHOT_DIR, { recursive: true }); await page.screenshot({ path: path.join(process.env.ANDROID_UI_SCREENSHOT_DIR, name + '.png') }); } };
    await page.getByRole('button', { name: '打开菜单', exact: true }).click();
    const menu = await page.locator('[aria-label="功能菜单"] button').allTextContents(); assert.equal(menu[1], '通道名称');
    await page.getByRole('button', { name: '通道名称', exact: true }).click();
    await page.getByLabel('A 通道名称', { exact: true }).fill('很'.repeat(21)); await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByText('通道名称最多 20 字，不能包含换行或控制字符。').waitFor(); assert.equal(calls.filter((call) => call.path.endsWith('channel-names')).length, 0);
    await page.getByLabel('A 通道名称', { exact: true }).fill('左侧'); await page.getByLabel('B 通道名称', { exact: true }).fill(''); await screenshot('channel-names');
    await page.getByRole('button', { name: '保存', exact: true }).click();
    await page.getByRole('navigation', { name: '主要视图' }).getByRole('button', { name: '设备', exact: true }).click();
    await page.getByRole('heading', { name: 'A · 左侧', exact: true }).first().waitFor(); await page.getByRole('heading', { name: 'B 通道', exact: true }).first().waitFor();
    await page.getByRole('button', { name: '打开菜单', exact: true }).click(); await page.getByRole('button', { name: '通道名称', exact: true }).click();
    assert.equal(await page.getByLabel('A 通道名称', { exact: true }).inputValue(), '左侧'); assert.equal(await page.getByLabel('B 通道名称', { exact: true }).inputValue(), 'B 通道');
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('button', { name: '打开菜单', exact: true }).click(); await page.getByRole('button', { name: '角色', exact: true }).click();
    await page.getByRole('button', { name: '编辑手工角色', exact: true }).click();
    await page.getByLabel('角色名称', { exact: true }).waitFor(); assert.equal(await page.getByLabel('角色性格', { exact: true }).inputValue(), '原本冷静');
    assert.equal(await page.getByLabel('音色', { exact: true }).inputValue(), 'kokoro-zm_010');
    assert.equal(await page.evaluate(() => window.fixture.getState().role), 'assistant', 'editing another avatar never selects that role');
    assert.equal(calls.filter((call) => call.path === '/api/character/profile').length, 0);
    const picture = Buffer.from(await page.evaluate(() => { const canvas = document.createElement('canvas'); canvas.width = 1200; canvas.height = 800; const context = canvas.getContext('2d'); context.fillStyle = '#489aaa'; context.fillRect(0, 0, 1200, 800); context.fillStyle = '#e9c05f'; context.fillRect(300, 100, 400, 600); return canvas.toDataURL('image/png').split(',')[1]; }), 'base64');
    const fileInput = page.locator('#role-edit-avatar'); assert.equal(await fileInput.getAttribute('capture'), null); assert.equal(await fileInput.getAttribute('multiple'), null); assert.equal(await fileInput.getAttribute('accept'), 'image/*');
    const chooser = page.waitForEvent('filechooser'); await page.getByRole('button', { name: '选择头像', exact: true }).click(); await (await chooser).setFiles({ name: 'test-avatar.png', mimeType: 'image/png', buffer: picture });
    const preview = page.getByRole('img', { name: '所选角色头像' }); await preview.waitFor();
    assert.match(await preview.getAttribute('src'), /^data:image\/jpeg;base64,/); await page.waitForFunction(() => document.querySelector('.android-avatar-preview img')?.naturalWidth === 512);
    await page.getByLabel('角色名称', { exact: true }).fill('修改后的角色'); await page.getByLabel('角色性格', { exact: true }).fill('独立冷静'); await page.getByLabel(/角色背景/).fill('修改后的背景'); await page.getByLabel('音色', { exact: true }).selectOption('kokoro-zf_001');
    await screenshot('role-edit-avatar'); await page.getByRole('button', { name: '返回角色', exact: true }).click();
    assert.equal(calls.filter((call) => call.method === 'PUT').length, 0, 'cancel does not upload pixels or form edits');
    await page.getByRole('button', { name: '编辑手工角色', exact: true }).click(); await page.getByLabel('角色名称', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('角色名称', { exact: true }).inputValue(), '手工角色'); assert.equal(await preview.count(), 0);
    await fileInput.setInputFiles({ name: 'test-avatar.png', mimeType: 'image/png', buffer: picture }); await preview.waitFor();
    await page.getByLabel('角色名称', { exact: true }).fill('修改后的角色'); await page.getByLabel('角色性格', { exact: true }).fill('独立冷静');
    await page.evaluate(() => window.fixture.setChat({ awaitingConfirmation: true })); assert(await page.getByRole('button', { name: '保存修改', exact: true }).isDisabled());
    await page.evaluate(() => window.fixture.setChat({ awaitingConfirmation: false })); failEdit = true;
    await page.getByRole('button', { name: '保存修改', exact: true }).click(); await page.getByText('模拟保存失败，旧头像保留', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('角色名称', { exact: true }).inputValue(), '修改后的角色'); assert.match(await preview.getAttribute('src'), /^data:image\/jpeg/);
    await page.getByRole('button', { name: '保存修改', exact: true }).click(); await page.getByRole('button', { name: '编辑修改后的角色', exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.fixture.getState().role), 'assistant'); assert(uploads.manual.length < 680000);
    await page.getByRole('button', { name: '编辑修改后的角色', exact: true }).click(); await page.getByLabel('角色名称', { exact: true }).waitFor();
    assert.match(await preview.getAttribute('src'), /^\/api\/character\/avatar\?role=manual/);
    await page.getByRole('button', { name: '恢复默认头像', exact: true }).click(); await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.getByRole('button', { name: '编辑修改后的角色', exact: true }).waitFor(); assert.equal(uploads.manual, null);
    await page.getByRole('button', { name: '编辑搜索角色', exact: true }).click(); await page.getByLabel('补充设定', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('补充设定', { exact: true }).inputValue(), '原搜索补充'); assert.equal(await page.getByLabel('角色性格', { exact: true }).count(), 0);
    await page.getByLabel('补充设定', { exact: true }).fill('新的补充设定'); await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.getByRole('button', { name: '编辑搜索角色', exact: true }).waitFor(); assert.equal(roles.search.note, '新的补充设定'); assert.equal(roles.search.sources[0].title, '原资料');
    await page.getByRole('button', { name: '编辑情景助手', exact: true }).click(); await page.getByLabel('角色名称', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('音色', { exact: true }).inputValue(), 'system-default');
    await page.getByLabel('角色名称', { exact: true }).fill('我的助手'); await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.getByRole('button', { name: '编辑我的助手', exact: true }).waitFor(); assert.equal(await page.evaluate(() => window.fixture.getState().role), 'assistant');
    await screenshot('roles-with-edit'); assert.equal(await page.locator('button button').count(), 0);
    await page.locator('.android-role-select').filter({ hasText: '修改后的角色' }).click(); await page.waitForFunction(() => window.fixture.getState().role === 'manual');
    await page.getByRole('button', { name: '创建角色', exact: true }).click(); await page.getByRole('button', { name: '自定义创建角色', exact: true }).click();
    await page.getByLabel('角色名称', { exact: true }).fill('带头像的新角色'); await page.getByLabel('角色性格', { exact: true }).fill('耐心');
    await page.locator('#role-custom-avatar').setInputFiles({ name: 'new.png', mimeType: 'image/png', buffer: picture }); await preview.waitFor();
    await page.getByRole('button', { name: '创建并使用', exact: true }).click(); await page.getByRole('button', { name: '创建角色', exact: true }).waitFor();
    assert.equal(calls.filter((call) => call.path === '/api/character/custom').length, 1); assert.deepEqual(errors, []);
    console.log('PASS: channel names/menu/order/validation/reset; manual/search/builtin editing without selection; independent avatar file picker/local JPEG512 preview/cancel/save failure/reset; pending-request protection; creation avatar; global stop and 400px layout.');
  } finally { await browser.close(); await new Promise((resolve) => server.close(resolve)); }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
