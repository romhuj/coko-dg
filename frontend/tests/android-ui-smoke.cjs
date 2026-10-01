// Android UI integration smoke. Every API is intercepted; no running backend or hardware is used.
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const fs = require("node:fs");
const { build } = require("esbuild");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_DIR
  ? path.join(process.env.PLAYWRIGHT_MODULE_DIR, "playwright") : "playwright");

async function main() {
  const bundle = await build({ stdin: { contents: `
    import React from "react";
    import { createRoot } from "react-dom/client";
    import App from "./src/App";
    import { useApp, useChat } from "./src/store";
    const pair = { A: 0, B: 0 };
    const initial = {
      platform: "android", capabilities: {camera:false,audio:false,dungeon:false,ble:false},
      lang:"zh",role:"assistant",profile:"角色扮演",role_title:"情景助手",intensity_level:"中",
      connected:false,test_mode:false,estop:true,autopilot:false,turn_busy:false,pending_chat:0,
      current:pair,requested:pair,patterns:pair,pulse_active:{A:false,B:false},
      effective_caps:{A:14,B:11},caps:{A:100,B:100},user_caps:{A:14,B:11},
      device_channels:{A:{name:"A",location:""},B:{name:"B",location:""}},
      intensity_device_link:true,enabled_channels:{A:true,B:true},active_channels:{A:true,B:true},
      ui:{quick_strengths:[0,5,10],default_temp_s:5,default_pulse_s:5},
      sensors:{camera:false,audio:false},config_info:{version:"android-test",model:"",title:"情景助手"},
      roles:[{name:"assistant",label:"情景助手",is_custom:true,profiles:[{name:"角色扮演",available:true}]}],
      relay:{status:"waiting",controller_id:"android-controller",last_error:"",clients:[]},
      presets:Array.from({length:236},(_,i)=>({name:"波形"+i,label:"波形"+i,category:i<100?"内置":"导入",frames:[]})),
    };
    useApp.setState({state:initial});
    window.fixture = {
      getState:()=>useApp.getState().state,
      setState:(patch)=>useApp.setState({state:{...useApp.getState().state,...patch}}),
      pushAutoMessage:(text)=>useChat.getState().push({role:"ai",text}),
    };
    window.WebSocket = class { close() {} };
    Object.defineProperty(navigator, "clipboard", {value:{writeText:async()=>{throw Error("fallback test")}}});
    document.execCommand = (command)=>{window.copiedPairLink=document.activeElement.value;return command==="copy";};
    createRoot(document.getElementById("root")).render(<App />);
  `, loader:"tsx",resolveDir:path.resolve(__dirname,"..")},bundle:true,write:false,format:"iife",loader:{".png":"dataurl"},define:{"process.env.NODE_ENV":'"development"'}});
  const assets = path.resolve(__dirname,"../dist/assets");
  const css = fs.readFileSync(path.join(assets,fs.readdirSync(assets).find((p)=>p.endsWith(".css"))),"utf8");
  const server = http.createServer((req,res)=>{
    res.setHeader("Content-Type",req.url==="/app.js"?"application/javascript":"text/html; charset=utf-8");
    res.end(req.url==="/app.js"?bundle.outputFiles[0].text:`<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style></head><body><div id="root"></div><script src="/app.js"></script></body></html>`);
  });
  await new Promise((resolve)=>server.listen(0,"127.0.0.1",resolve));
  const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||undefined});
  try {
    const page = await browser.newPage({viewport:{width:375,height:812}});
    const errors=[]; const calls=[]; const chat=[];
    page.on("pageerror",(e)=>errors.push(String(e)));
    let llm={has_key:false,saved:false,api_key_masked:"",base_url:"https://provider.invalid/v1",model:"",json_mode:true};
    let pendingChat=null;
    await page.route("**/api/**",async(route)=>{
      const req=route.request();const url=new URL(req.url());calls.push(url.pathname);
      if(url.pathname==="/api/state")return route.fulfill({json:await page.evaluate(()=>window.fixture.getState())});
      if(url.pathname==="/api/settings/llm"){
        if(req.method()==="POST"){
          const body=req.postDataJSON();assert.equal(body.api_key,"test-only-secret");
          llm={...llm,has_key:!!body.api_key,saved:true,base_url:body.base_url,model:body.model};
          return route.fulfill({json:{ok:true,model:body.model}});
        }
        return route.fulfill({json:llm});
      }
      if(url.pathname==="/api/qrcode.png") return route.fulfill({contentType:"image/png",body:Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlSAAAAAASUVORK5CYII=","base64")});
      if(url.pathname==="/api/pair_url")return route.fulfill({json:{url:"https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#test-payload"}});
      if(url.pathname==="/api/chat"){
        chat.push(req.postDataJSON());
        if(pendingChat)return pendingChat(route);
        return route.fulfill({json:{line:"手机测试回复",executed:[],dropped:[]}});
      }
      throw Error("Unexpected API request: "+url.pathname);
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const nav=page.getByRole("navigation",{name:"手机导航"});
    await nav.waitFor();
    await page.getByRole("button",{name:"配置模型",exact:true}).waitFor();
    const message=page.getByRole("textbox",{name:"聊天消息"});
    assert(await message.isVisible());
    assert(await page.getByRole("button",{name:"解除急停",exact:true}).isVisible());
    assert.equal(await page.getByRole("button",{name:/地牢/}).count(),0);
    await message.fill("保留手机草稿");
    await page.getByRole("button",{name:"配置模型",exact:true}).click();
    await page.getByLabel("API Key",{exact:true}).fill("test-only-secret");
    await page.getByLabel("模型名",{exact:true}).fill("mock-model");
    await page.getByRole("button",{name:"保存并生效",exact:true}).click();
    await page.getByText("已保存并生效（模型：mock-model）").waitFor();
    assert.equal(await page.getByLabel("API Key",{exact:true}).inputValue(),"");
    await nav.getByRole("button",{name:"聊天",exact:true}).click();
    await page.getByRole("button",{name:"配置模型",exact:true}).waitFor({state:"hidden"});
    assert.equal(await message.inputValue(),"保留手机草稿");
    await message.press("Enter");
    assert.equal(chat.length,0,"Android enter inserts a newline instead of sending");
    await page.getByRole("button",{name:"发送",exact:true}).click();
    await page.getByText("手机测试回复",{exact:true}).waitFor();
    assert.equal(chat[0].mode,"auto");
    assert.equal(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}).includes("test-only-secret")),false);
    console.log("PASS Android model onboarding, secret handling, draft retention, unpaired/estop chat");

    await nav.getByRole("button",{name:"角色",exact:true}).click();
    await page.getByTitle("切换角色入口与电击强度").click();
    for(const level of ["低","中","高","极高","最高","炼狱"])assert(await page.getByRole("button",{name:level,exact:true}).isVisible());
    await page.getByTitle("展开入口列表").click();
    assert.equal(await page.getByRole("button",{name:/体验版|品评会|哥布林/}).count(),0);
    await page.getByTitle("展开入口列表").click();
    await page.getByRole("button",{name:"＋ 角色扮演 · 网络搜索角色",exact:true}).click();
    const dialog=page.getByRole("dialog");await dialog.waitFor();
    const bounds=await dialog.boundingBox();assert(bounds.x>=0 && bounds.x+bounds.width<=375);
    await page.getByRole("button",{name:"关闭角色搜索",exact:true}).click();
    await nav.getByRole("button",{name:"设备",exact:true}).click();
    assert.equal(await page.getByRole("button",{name:/摄像头|麦克风|蓝牙/}).count(),0);
    assert.equal(await page.locator("body").evaluate((e)=>e.scrollWidth),375);
    console.log("PASS Android neutral role, six levels, network role access and unsupported controls hidden");

    await nav.getByRole("button",{name:"配对",exact:true}).click();
    await page.getByRole("button",{name:"复制配对链接",exact:true}).click();
    await page.getByText("配对链接已复制",{exact:true}).waitFor();
    assert((await page.evaluate(()=>window.copiedPairLink)).includes("test-payload"));
    assert((await page.getByRole("link",{name:"保存二维码",exact:true}).getAttribute("href")).startsWith("/api/qrcode.png?"));
    assert(await page.getByText(/切换到 DG-LAB App 前/).isVisible());
    assert(await page.getByText(/尚待验证/).isVisible());
    assert.equal(calls.includes("/api/network"),false);
    await page.evaluate(()=>window.fixture.setState({relay:{status:"connecting",controller_id:null,last_error:"",clients:[]}}));
    assert(await page.getByRole("button",{name:"复制配对链接",exact:true}).isDisabled());
    await page.getByRole("textbox",{name:"配对链接",exact:true}).waitFor({state:"hidden"});
    console.log("PASS Android pairing clipboard fallback, QR download, bridge/background caveats and stale link removal");

    await nav.getByRole("button",{name:"聊天",exact:true}).click();
    await page.getByRole("button",{name:"波形：AI 自动选择 · 更改",exact:true}).click();
    assert.equal(await page.getByLabel("本条消息的波形").locator("option").count(),237);
    await page.getByLabel("搜索波形或分类").fill("波形235");
    await page.getByLabel("本条消息的波形").selectOption("波形235");
    await page.getByRole("button",{name:"收起波形选择",exact:true}).click();
    await page.evaluate(()=>window.fixture.setState({autopilot:true,turn_busy:true}));
    let release;
    const gate=new Promise((resolve)=>release=resolve);
    pendingChat=async(route)=>{await gate;return route.fulfill({json:{line:"排队测试回复",executed:[],dropped:[]}});};
    await message.fill("自动运行时发消息");
    await page.getByRole("button",{name:"发送",exact:true}).click();
    await page.evaluate(()=>{window.fixture.setState({pending_chat:1});window.fixture.pushAutoMessage("自动回合消息");});
    await page.getByRole("button",{name:"排队中…",exact:true}).waitFor();
    release();pendingChat=null;
    await page.getByText("排队测试回复",{exact:true}).waitFor();
    assert.equal(await page.getByText("自动回合消息",{exact:true}).count(),1);
    assert.equal(await page.getByText("排队测试回复",{exact:true}).count(),1);
    assert.equal(chat.at(-1).preferred_pattern,"波形235");
    console.log("PASS Android 236 waveforms, autopilot queue and unique replies");

    await page.setViewportSize({width:375,height:450});
    await message.fill("键盘打开时的草稿");
    const send=page.getByRole("button",{name:"发送",exact:true});
    const inputBox=await message.boundingBox();const sendBox=await send.boundingBox();
    const footer=await page.getByRole("contentinfo",{name:"设备保护"}).boundingBox();
    assert(inputBox.y>=0 && inputBox.y+inputBox.height<=footer.y);
    assert(sendBox.y>=0 && sendBox.y+sendBox.height<=footer.y);
    assert(footer.y+footer.height<=451);
    assert.equal(await page.locator("body").evaluate((e)=>e.scrollWidth),375);
    const out=process.env.ANDROID_UI_SCREENSHOT_DIR;
    if(out){fs.mkdirSync(out,{recursive:true});await page.screenshot({path:path.join(out,"android-keyboard.png")});await page.setViewportSize({width:375,height:812});await page.screenshot({path:path.join(out,"android-chat.png")});}
    assert.deepEqual(errors,[]);
    console.log("PASS Android compact keyboard viewport, visible send/emergency controls and mobile width");
  } finally {await browser.close();await new Promise((resolve)=>server.close(resolve));}
}
main().catch((error)=>{console.error(error);process.exitCode=1;});
