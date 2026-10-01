// Isolated component smoke test. All API responses are mocked; no device server is used.
// Set PLAYWRIGHT_MODULE_DIR if Playwright is provided by an external runtime.
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const { build } = require("esbuild");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_DIR
  ? path.join(process.env.PLAYWRIGHT_MODULE_DIR, "playwright") : "playwright");

async function main() {
  const bundle = await build({
    stdin: {
      contents: `
        import React from "react";
        import { createRoot } from "react-dom/client";
        import ChatPanel from "./src/components/ChatPanel";
        import RoleCard from "./src/components/RoleCard";
        import { useApp, useChat } from "./src/store";
        const initial = {
          lang: "zh", role: "触手", profile: "正式", intensity_level: "中",
          connected: false, test_mode: false, estop: false, autopilot: false, turn_busy: false, pending_chat: 0,
          effective_caps: { A: 14, B: 11 }, intensity_device_link: true,
          roles: [
            { name: "触手", label: "触手", profiles: [{ name: "正式", available: true }] },
            { name: "custom-role-id", label: "自定义角色", profiles: [{ name: "正式", available: true }] },
          ],
          relay: { status: "disconnected", controller_id: null, last_error: "", clients: [] },
          enabled_channels: { A: true, B: true },
          presets: [
            { name: "经典", label: "经典脉冲", category: "内置" },
            { name: "导入波形", label: "Imported Wave", category: "导入" },
          ],
        };
        useApp.setState({ state: initial });
        window.fixture = {
          setState: (patch) => useApp.setState({ state: { ...useApp.getState().state, ...patch } }),
          getState: () => useApp.getState().state,
          setChatBusy: (busy) => useChat.setState({ busy }),
          pushAutoMessage: (line) => useChat.getState().push({ role: "ai", text: line }),
          reset: () => { useApp.setState({ state: initial }); useChat.setState({ messages: [], busy: false }); },
        };
        const root = createRoot(document.getElementById("root"));
        window.fixture.showChat = () => root.render(<ChatPanel />);
        root.render(
          new URLSearchParams(location.search).get("view") === "role"
            ? <div style={{width: "min(300px, 100%)", padding: 12}}><RoleCard /></div> : <ChatPanel />
        );
      `,
      loader: "tsx",
      resolveDir: path.resolve(__dirname, ".."),
    },
    bundle: true,
    write: false,
    format: "iife",
    loader: { ".png": "dataurl" },
    define: { "process.env.NODE_ENV": '"development"' },
  });
  const assetsDir = path.resolve(__dirname, "../dist/assets");
  const cssName = fs.existsSync(assetsDir) ? fs.readdirSync(assetsDir).find((file) => file.endsWith(".css")) : null;
  const css = cssName ? fs.readFileSync(path.join(assetsDir, cssName), "utf8") : "";
  const server = http.createServer((req, res) => {
    res.setHeader("Content-Type", req.url === "/app.js" ? "application/javascript" : "text/html; charset=utf-8");
    res.end(req.url === "/app.js" ? bundle.outputFiles[0].text
      : `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head><body><div id="root"></div><script src="/app.js"></script></body></html>`);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 640, height: 850 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const requests = [];
    const intensityRequests = [];
    const searchRequests = [];
    const createRequests = [];
    const maliciousTitle = '<img src=x onerror="window.sourceInjected=1">';
    const maliciousSummary = '<script>window.sourceInjected=2</script> 网络资料原文。';
    let nextResponse = null;
    let profileGate = null;
    let qrAttempts = 0;
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/state") return route.fulfill({ json: await page.evaluate(() => window.fixture.getState()) });
      if (url.pathname === "/api/intensity") {
        const payload = route.request().postDataJSON();
        intensityRequests.push(payload);
        await page.evaluate((level) => window.fixture.setState({ intensity_level: level }), payload.level);
        return route.fulfill({ json: { ok: true, intensity_level: payload.level } });
      }
      if (url.pathname === "/api/character/profile") {
        const payload = route.request().postDataJSON();
        if (profileGate) {
          await profileGate;
          profileGate = null;
        }
        await page.evaluate((patch) => window.fixture.setState(patch), payload);
        return route.fulfill({ json: { ok: true, ...payload } });
      }
      if (url.pathname === "/api/character/search") {
        searchRequests.push(route.request().postDataJSON());
        return route.fulfill({ json: {
          search_id: "fixture-search-id", query: "福尔摩斯", warnings: [], sources: [
            { title: maliciousTitle, summary: maliciousSummary, url: "https://example.org/source-one", language: "zh", provider: "测试资料" },
            { title: "福尔摩斯", summary: "角色资料第二项。", url: "https://example.org/source-two", language: "en", provider: "测试资料" },
          ],
        } });
      }
      if (url.pathname === "/api/character/create") {
        const payload = route.request().postDataJSON();
        createRequests.push(payload);
        await page.evaluate((name) => {
          const current = window.fixture.getState();
          window.fixture.setState({ role: name, profile: "正式", roles: [
            ...current.roles, { name, label: name, is_custom: true, profiles: [{ name: "正式", available: true }] },
          ] });
        }, payload.name);
        return route.fulfill({ json: { ok: true, role: payload.name, profile: "正式" } });
      }
      if (url.pathname === "/api/qrcode.png") {
        qrAttempts++;
        if (qrAttempts === 1) return route.fulfill({ status: 503, body: "not ready" });
        return route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlSAAAAAASUVORK5CYII=", "base64") });
      }
      assert.equal(url.pathname, "/api/chat", "Unexpected API call: " + url.pathname);
      requests.push(route.request().postDataJSON());
      if (nextResponse) {
        const respond = nextResponse;
        nextResponse = null;
        return respond(route);
      }
      return route.fulfill({ json: { line: "测试回复", executed: [], dropped: [] } });
    });
    await page.clock.install();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const message = page.getByRole("textbox", { name: "聊天消息" });
    assert.equal(await message.isVisible(), true, "Unpaired users must see the composer");
    assert.equal(await page.getByRole("button", { name: "情景互动", exact: true }).getAttribute("aria-pressed"), "true");
    if (process.env.CHAT_UI_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.CHAT_UI_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ animations: "disabled", path: path.join(process.env.CHAT_UI_SCREENSHOT_DIR, "chat-text-wide.png") });
      await page.setViewportSize({ width: 375, height: 812 });
      await page.screenshot({ animations: "disabled", path: path.join(process.env.CHAT_UI_SCREENSHOT_DIR, "chat-text-mobile.png") });
      await page.setViewportSize({ width: 640, height: 850 });
    }
    await message.fill("你好");
    await message.press("Enter");
    await page.getByText("测试回复", { exact: true }).waitFor();
    assert.deepEqual(requests[0], { message: "你好", mode: "auto" });
    assert.equal(await message.inputValue(), "");
    console.log("PASS: default scene interaction can chat without pairing");

    const beforeIme = requests.length;
    await message.fill("输入法测试");
    await message.dispatchEvent("compositionstart");
    await message.press("Enter");
    assert.equal(requests.length, beforeIme, "IME composition must not submit");
    await message.dispatchEvent("compositionend");
    await message.press("Shift+Enter");
    assert.equal(requests.length, beforeIme, "Shift+Enter must not submit");
    assert.equal((await message.inputValue()).includes("\n"), true);
    console.log("PASS: IME composition and Shift+Enter preserve the draft");

    let releasePending;
    const pendingGate = new Promise((resolve) => { releasePending = resolve; });
    nextResponse = async (route) => { await pendingGate; await route.fulfill({ json: { line: "延迟回复", executed: [], dropped: [] } }); };
    await message.fill("等待测试");
    await message.press("Enter");
    await page.getByRole("button", { name: "回复中…", exact: true }).waitFor();
    await message.press("Enter");
    assert.equal(await page.getByRole("button", { name: "回复中…", exact: true }).isDisabled(), true);
    assert.equal(await page.getByRole("button", { name: "情景互动", exact: true }).isDisabled(), true);
    releasePending();
    await page.getByText("延迟回复", { exact: true }).waitFor();
    assert.equal(requests.filter((r) => r.message === "等待测试").length, 1);
    console.log("PASS: pending requests cannot be submitted twice");

    await page.evaluate(() => window.fixture.setState({ autopilot: true, turn_busy: true, connected: true }));
    let releaseAuto;
    const autoGate = new Promise((resolve) => { releaseAuto = resolve; });
    nextResponse = async (route) => { await autoGate; await route.fulfill({ json: { line: "用户回合回复", executed: [], dropped: [] } }); };
    await message.fill("自动运行时聊天");
    assert.equal(await page.getByRole("button", { name: "发送", exact: true }).isEnabled(), true);
    await message.press("Enter");
    await page.getByRole("button", { name: "回复中…", exact: true }).waitFor();
    await page.evaluate(() => window.fixture.setState({ pending_chat: 1 }));
    await page.getByRole("button", { name: "排队中…", exact: true }).waitFor();
    assert.equal(await message.getAttribute("readonly"), "");
    await page.evaluate(() => {
      window.fixture.pushAutoMessage("自动回合回复");
      window.fixture.setState({ pending_chat: 0 });
    });
    releaseAuto();
    await page.getByText("用户回合回复", { exact: true }).waitFor();
    assert.equal(await page.getByText("自动回合回复", { exact: true }).count(), 1);
    assert.equal(await page.getByText("用户回合回复", { exact: true }).count(), 1);
    assert.equal(requests.filter((r) => r.message === "自动运行时聊天").length, 1);
    assert.equal(await page.evaluate(() => window.fixture.getState().autopilot), true);
    await page.evaluate(() => window.fixture.setState({ turn_busy: false }));
    console.log("PASS: Autopilot accepts queued chat and both automatic and user replies appear once");

    nextResponse = (route) => route.fulfill({ status: 503, json: { error: "模型暂不可用" } });
    await message.fill("保留这条消息");
    await message.press("Enter");
    await page.getByRole("alert").waitFor();
    assert.equal(await message.inputValue(), "保留这条消息");
    assert.match(await page.getByRole("alert").innerText(), /模型暂不可用/);
    await page.getByRole("button", { name: "重试发送", exact: true }).click();
    await message.waitFor();
    await page.waitForFunction(() => document.querySelector("#chat-message").value === "");
    assert.equal(await page.getByText("保留这条消息", { exact: true }).count(), 1);
    console.log("PASS: failed requests preserve the draft and retry without duplicate messages");

    await message.fill("设备消息");
    assert.equal(await page.getByRole("button", { name: "发送", exact: true }).isEnabled(), true);
    await page.getByRole("textbox", { name: "搜索波形或分类" }).fill("导入");
    await page.getByRole("combobox", { name: "本条消息的波形" }).selectOption("导入波形");
    await message.press("Enter");
    await page.waitForFunction(() => document.querySelector("#chat-message").value === "");
    assert.deepEqual(requests.at(-1), { message: "设备消息", mode: "auto", preferred_pattern: "导入波形" });
    await page.evaluate(() => window.fixture.setState({ estop: true }));
    await message.fill("急停测试");
    assert.equal(await page.getByRole("button", { name: "发送", exact: true }).isEnabled(), true);
    await message.press("Enter");
    await page.waitForFunction(() => document.querySelector("#chat-message").value === "");
    assert.deepEqual(requests.at(-1), { message: "急停测试", mode: "auto", preferred_pattern: "导入波形" });
    await page.getByRole("button", { name: "仅文字", exact: true }).click();
    await message.fill("仅文字测试");
    await message.press("Enter");
    await page.waitForFunction(() => document.querySelector("#chat-message").value === "");
    assert.deepEqual(requests.at(-1), { message: "仅文字测试", mode: "text" });
    console.log("PASS: scene mode includes waveform preference and chat survives E-Stop; text-only is optional");

    await page.evaluate(() => window.fixture.setState({ estop: false, test_mode: false, connected: false, relay: { status: "waiting", controller_id: "controller-1", last_error: "" } }));
    await page.getByRole("button", { name: "配对设备", exact: true }).click();
    await page.getByText("二维码加载失败", { exact: true }).waitFor();
    await page.clock.runFor(4100);
    await page.getByRole("img", { name: "配对二维码" }).waitFor();
    assert.equal(qrAttempts, 2);
    const firstSrc = await page.getByRole("img", { name: "配对二维码" }).getAttribute("src");
    assert.match(firstSrc, /controller_id=controller-1/);
    await page.evaluate(() => window.fixture.setState({ relay: { status: "waiting", controller_id: "controller-2", last_error: "" } }));
    await page.waitForFunction(() => document.querySelector('img[alt="配对二维码"]').getAttribute("src").includes("controller-2"));
    assert.notEqual(await page.getByRole("img", { name: "配对二维码" }).getAttribute("src"), firstSrc);
    console.log("PASS: QR failures retry and controller changes refresh the image URL");

    await page.goto(`http://127.0.0.1:${server.address().port}/?view=role`);
    await page.getByTitle("切换角色入口与电击强度", { exact: true }).click();
    for (const [level, scale] of [["低", "0.7"], ["中", "1.0"], ["高", "1.3"], ["极高", "1.6"], ["最高", "2.0"], ["炼狱", "2.5"]]) {
      await page.getByRole("button", { name: level, exact: true }).click();
      await page.getByText(`当前 ×${scale}`, { exact: true }).waitFor();
      assert.deepEqual(intensityRequests.at(-1), { level });
    }
    assert.equal(intensityRequests.length, 6);
    assert.match(await page.locator("#root").innerText(), /A ≤ 14 · B ≤ 11/);
    if (process.env.CHAT_UI_SCREENSHOT_DIR) {
      await page.screenshot({ animations: "disabled", path: path.join(process.env.CHAT_UI_SCREENSHOT_DIR, "role-six-levels.png") });
    }
    console.log("PASS: all six intensity buttons send their level and display the configured scale and caps");
    await page.getByTitle("展开入口列表", { exact: true }).click();
    let finishProfile;
    profileGate = new Promise((resolve) => { finishProfile = resolve; });
    await page.getByRole("button").filter({ has: page.getByText("自定义角色", { exact: true }) }).click();
    await page.getByText("正在切换角色，当前回合结束后生效…", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button").filter({ has: page.getByText("自定义角色", { exact: true }) }).isDisabled(), true);
    finishProfile();
    await page.waitForFunction(() => window.fixture.getState().role === "custom-role-id");
    assert.match(await page.getByTitle("切换角色入口与电击强度", { exact: true }).innerText(), /自定义角色/);
    console.log("PASS: backend roles appear in the entry list even without an is_custom flag");

    await page.evaluate(() => window.fixture.setChatBusy(true));
    const searchRoleButton = page.getByRole("button", { name: "＋ 角色扮演 · 网络搜索角色", exact: true });
    assert.equal(await searchRoleButton.isDisabled(), true, "Role creation must wait for the pending chat");
    await page.getByTitle("展开入口列表", { exact: true }).click();
    assert.equal(await page.getByRole("button").filter({ has: page.getByText("自定义角色", { exact: true }) }).last().isDisabled(), true, "Role switching must wait for the pending chat");
    await page.getByTitle("展开入口列表", { exact: true }).click();
    await page.evaluate(() => window.fixture.setChatBusy(false));
    assert.equal(await searchRoleButton.isEnabled(), true);
    console.log("PASS: pending chat locks role switching and character creation");

    await page.getByRole("button", { name: "＋ 角色扮演 · 网络搜索角色", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "搜索并创建角色" });
    await dialog.waitFor();
    await page.getByRole("textbox", { name: "角色名 / 作品名" }).fill("福尔摩斯");
    await dialog.getByRole("button", { name: "搜索", exact: true }).click();
    await dialog.getByRole("radio", { name: `选择资料：${maliciousTitle}`, exact: true }).waitFor();
    assert.deepEqual(searchRequests, [{ query: "福尔摩斯" }]);
    assert.equal(await dialog.getByText(maliciousTitle, { exact: true }).count(), 1);
    assert.equal(await dialog.getByText(maliciousSummary, { exact: true }).count(), 1);
    assert.equal(await dialog.locator("img,script").count(), 0, "Source text must not become HTML");
    assert.equal(await page.evaluate(() => window.sourceInjected), undefined);
    assert.equal(await dialog.getByRole("link", { name: "查看来源" }).nth(1).getAttribute("href"), "https://example.org/source-two");
    await dialog.getByRole("radio", { name: "选择资料：福尔摩斯", exact: true }).check();
    await page.getByRole("textbox", { name: "角色显示名称", exact: true }).fill("网络测试角色");
    await page.getByRole("textbox", { name: "性格、动机、说话习惯", exact: false }).fill("冷静地提问并引导推理。");
    await page.setViewportSize({ width: 375, height: 812 });
    const modalBox = await dialog.boundingBox();
    assert.ok(modalBox && modalBox.x >= 0 && modalBox.y >= 0 && modalBox.x + modalBox.width <= 375 && modalBox.y + modalBox.height <= 812, "The modal must fit a mobile viewport");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, "No horizontal page overflow");
    const createButton = dialog.getByRole("button", { name: "创建并使用", exact: true });
    assert.equal(await createButton.isVisible(), true);
    if (process.env.CHAT_UI_SCREENSHOT_DIR) {
      await page.screenshot({ animations: "disabled", path: path.join(process.env.CHAT_UI_SCREENSHOT_DIR, "role-search-mobile.png") });
    }
    await createButton.click();
    await dialog.waitFor({ state: "detached" });
    assert.deepEqual(createRequests, [{ search_id: "fixture-search-id", source_index: 1, name: "网络测试角色", note: "冷静地提问并引导推理。" }]);
    assert.match(await page.getByTitle("切换角色入口与电击强度", { exact: true }).innerText(), /网络测试角色/);
    console.log("PASS: role search escapes sources, fits mobile, and creates the explicitly selected source");
    await page.evaluate(() => {
      window.fixture.setState({ connected: true, autopilot: true });
      window.fixture.showChat();
    });
    await page.getByText("网络测试角色", { exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "情景互动", exact: true }).getAttribute("aria-pressed"), "true");
    nextResponse = (route) => route.fulfill({ json: { line: "网络角色结合情景回复", executed: [{ label: "更新波形", sent: true }], dropped: [] } });
    await message.fill("继续当前情景");
    await message.press("Enter");
    await page.getByText("网络角色结合情景回复", { exact: true }).waitFor();
    assert.deepEqual(requests.at(-1), { message: "继续当前情景", mode: "auto" });
    assert.equal(await page.getByText("✓ 已执行 1", { exact: true }).count(), 1);
    assert.equal(await page.getByText("更新波形", { exact: true }).count(), 1);
    if (process.env.CHAT_UI_SCREENSHOT_DIR) {
      await page.screenshot({ animations: "disabled", path: path.join(process.env.CHAT_UI_SCREENSHOT_DIR, "chat-scene-autopilot-mobile.png") });
    }
    console.log("PASS: a searched role remains selected in chat and its scene actions are displayed");
    assert.deepEqual(errors, [], "Browser runtime errors");
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
