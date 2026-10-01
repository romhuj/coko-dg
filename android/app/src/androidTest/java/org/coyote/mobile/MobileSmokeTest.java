package org.coyote.mobile;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.*;
import android.content.pm.ActivityInfo;
import android.graphics.Bitmap;
import android.os.*;
import android.test.InstrumentationTestCase;
import android.view.*;
import android.view.inputmethod.InputMethodManager;
import android.webkit.WebView;
import com.chaquo.python.*;
import com.chaquo.python.android.AndroidPlatform;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Test APK only. Requires a clean emulator. No real relay, model or device is used. */
public final class MobileSmokeTest extends InstrumentationTestCase {
    private Context context;
    private PyObject globals, runtime;
    private Activity activity;
    private RuntimeService service;
    private ServiceConnection binding;
    private final String token="instrumentation_session_only_00000000000000000000000000000000";

    public void testEmbeddedRuntimeAndNativeLifecycle() throws Exception {
        context=getInstrumentation().getTargetContext();
        assertEquals("io.github.romhuj.cokodg",context.getPackageName());
        assertEquals("coko DG",context.getApplicationInfo().loadLabel(context.getPackageManager()).toString());
        File actualData=new File(context.getFilesDir(),"runtime");
        assertFalse("Use a clean emulator: never overwrite an existing app configuration",
            new File(actualData,"config/config.yaml").exists());
        File seed=new File(context.getFilesDir(),"instrumentation-seed");
        File data=new File(context.getFilesDir(),"instrumentation-data");
        copyAsset("runtime_seed",seed);
        copyAsset("runtime_seed",actualData);
        if(!Python.isStarted()) Python.start(new AndroidPlatform(context));
        Python python=Python.getInstance();
        globals=python.getModule("builtins").callAttr("dict");
        globals.callAttr("__setitem__","private_root",data.getAbsolutePath());
        // This patch exists only in the instrumentation process, never in the app APK.
        execute("import os\nos.environ['DGLAB_DATA_DIR'] = private_root\n"
            + "from backend.device import factory\nfrom backend import main\n"
            + "original_factory = factory.build_backend\noriginal_main_factory = main.build_backend\n"
            + "class MemoryDevice:\n"
            + " name = 'dglab_relay'\n"
            + " async def start(self): pass\n"
            + " async def stop(self): pass\n"
            + " def ready(self): return False\n"
            + " def controller_id(self): return None\n"
            + " def client_state(self): return None\n"
            + " def on_disconnect(self, callback): pass\n"
            + " def to_state(self): return {'status':'disconnected','controller_id':None,'clients':[]}\n"
            + " def loops_active(self): return {'A':False,'B':False}\n"
            + " def stop_pulse_hold(self, channel=None): pass\n"
            + " async def apply(self, command): raise AssertionError('Unexpected hardware command')\n"
            + " async def start_pulse_hold(self, channel, command): raise AssertionError('Unexpected hardware playback')\n"
            + "factory.build_backend = main.build_backend = lambda *a, **kw: MemoryDevice()\n");
        runtime=python.getModule("backend.mobile_runtime");
        try {
            int port=runtime.callAttr("start",data.getAbsolutePath(),seed.getAbsolutePath(),token).toInt();
            assertTrue(port>0);
            assertEquals(403,request(port,"GET","/api/state",false,null).status);
            assertEquals(403,request(port,"GET","/",false,null).status);
            Response accepted=request(port,"GET","/api/state",true,null);
            assertEquals(200,accepted.status);
            JSONObject state=new JSONObject(accepted.body); assertSafeState(state);
            assertEquals(236,state.getJSONArray("presets").length());
            assertEquals(100,state.getJSONObject("effective_caps").getInt("A"));
            assertEquals(100,state.getJSONObject("effective_caps").getInt("B"));
            assertEquals(409,request(port,"POST","/api/autopilot",true,"{\"enabled\":true}").status);
            assertEquals(200,request(port,"GET","/",true,null).status);
            globals.callAttr("__setitem__","smoke_port",port); globals.callAttr("__setitem__","smoke_token",token);
            execute("import asyncio, json\nfrom websockets.legacy.client import connect\n"
                + "from websockets.exceptions import InvalidStatusCode\n"
                + "async def check_socket():\n"
                + " origin = f'http://127.0.0.1:{smoke_port}'\n"
                + " async with connect(f'ws://127.0.0.1:{smoke_port}/ws', origin=origin, extra_headers={'Cookie':f'coyote_session={smoke_token}'}) as ws:\n"
                + "  frame=json.loads(await asyncio.wait_for(ws.recv(),3))\n"
                + "  assert frame['type']=='state' and frame['data']['platform']=='android'\n"
                + " try:\n"
                + "  async with connect(f'ws://127.0.0.1:{smoke_port}/ws',origin=origin):\n"
                + "   raise AssertionError('Unauthenticated WebSocket accepted')\n"
                + " except InvalidStatusCode as exc: assert exc.status_code == 403\n"
                + "asyncio.run(check_socket())\n");
            assertEquals(200,request(port,"POST","/api/resume",true,"{}").status);
            runtime.callAttr("estop");
            long until=SystemClock.elapsedRealtime()+5000;
            while(!new JSONObject(request(port,"GET","/api/state",true,null).body).getBoolean("estop") && SystemClock.elapsedRealtime()<until) SystemClock.sleep(25);
            assertSafeState(new JSONObject(request(port,"GET","/api/state",true,null).body));
            runtime.callAttr("stop");
            int next=runtime.callAttr("start",data.getAbsolutePath(),seed.getAbsolutePath(),token).toInt();
            assertSafeState(new JSONObject(request(next,"GET","/api/state",true,null).body));
            runtime.callAttr("stop");

            if(Build.VERSION.SDK_INT>=33) {
                shell("pm grant " + context.getPackageName() + " android.permission.POST_NOTIFICATIONS");
            }
            activity=getInstrumentation().startActivitySync(new Intent(context,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            CountDownLatch connected=new CountDownLatch(1);
            binding=new ServiceConnection() {
                public void onServiceConnected(ComponentName name,IBinder binder) { service=((RuntimeService.LocalBinder)binder).getService(); connected.countDown(); }
                public void onServiceDisconnected(ComponentName name) { service=null; }
            };
            assertTrue(context.bindService(new Intent(context,RuntimeService.class),binding,Context.BIND_AUTO_CREATE));
            assertTrue(connected.await(10,TimeUnit.SECONDS));
            until=SystemClock.elapsedRealtime()+30000;
            while("starting".equals(service.getSnapshot().status) && SystemClock.elapsedRealtime()<until) SystemClock.sleep(50);
            assertEquals(service.getSnapshot().message,"ready",service.getSnapshot().status);
            String firstOrigin=service.getSnapshot().origin();
            WebView view=findWebView(); assertNotNull(view);
            until=SystemClock.elapsedRealtime()+15000;
            while(!pageReady(view) && SystemClock.elapsedRealtime()<until) SystemClock.sleep(100);
            assertTrue("Authenticated local WebView did not become ready",pageReady(view));
            assertTrue("Bright-screen background must be enabled without a setting",service.isBackgroundAllowed());
            SystemClock.sleep(400);
            dismissStartupPairing(view);
            capture("mobile-smoke-home.png");
            verifyNavigationAndExternalBrowser(view);
            verifyChatResumeAndActionReceipts(view);
            verifyRapidChatSubmit(view);
            android.util.Log.i("VoiceSmoke","bridge-test-starting");
            VoiceBridgeSmoke.verify(getInstrumentation(), activity, view, firstOrigin);
            SpeechBridgeSmoke.verify(getInstrumentation(), activity, view, firstOrigin);
            PairingBridgeSmoke.verify(getInstrumentation(), activity, firstOrigin);
            VoiceServiceSmoke.verify(getInstrumentation(), activity);
            shell("input keyevent 3");
            SystemClock.sleep(700);
            assertEquals("Bright-screen app switching must retain the current session","ready",service.getSnapshot().status);
            assertEquals(firstOrigin,service.getSnapshot().origin());
            getInstrumentation().runOnMainSync(()->activity.startActivity(new Intent(context,MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)));
            getInstrumentation().waitForIdleSync(); SystemClock.sleep(400);
            shell("settings put secure show_ime_with_hard_keyboard 1");
            getInstrumentation().runOnMainSync(()->{
                view.requestFocus();
                view.evaluateJavascript("(()=>{const e=document.querySelector('textarea');if(e){e.focus();return true;}return false;})()",focused->{
                    ((InputMethodManager)context.getSystemService(Context.INPUT_METHOD_SERVICE)).showSoftInput(view,InputMethodManager.SHOW_IMPLICIT);
                });
            });
            SystemClock.sleep(1200);
            AtomicReference<Integer> availableHeight=new AtomicReference<>();
            AtomicReference<Boolean> imeVisible=new AtomicReference<>(false);
            getInstrumentation().runOnMainSync(()->{
                availableHeight.set(view.getHeight());
                if(Build.VERSION.SDK_INT>=30) imeVisible.set(view.getRootWindowInsets().isVisible(WindowInsets.Type.ime()));
                int[] point=new int[2];view.getLocationOnScreen(point);
                android.util.Log.i("MobileSmoke","webHeight="+view.getHeight()+" webTop="+point[1]+" ime="+imeVisible.get()+" softInput="+activity.getWindow().getAttributes().softInputMode);
            });
            assertTrue("Keyboard left too little WebView height",availableHeight.get()>160*context.getResources().getDisplayMetrics().density);
            assertEquals("Emergency stop must remain reachable above the keyboard","true",evaluate(view,stopReachableExpression()));
            capture("mobile-smoke-keyboard.png");
            if(Build.VERSION.SDK_INT>=30) assertTrue("Soft keyboard did not open in emulator",imeVisible.get());
            getInstrumentation().runOnMainSync(()->((InputMethodManager)context.getSystemService(Context.INPUT_METHOD_SERVICE)).hideSoftInputFromWindow(view.getWindowToken(),0));
            getInstrumentation().runOnMainSync(()->activity.setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE));
            getInstrumentation().waitForIdleSync(); SystemClock.sleep(500);
            assertEquals("ready",service.getSnapshot().status); assertEquals(firstOrigin,service.getSnapshot().origin());
            assertTrue(pageReady(view));
            getInstrumentation().runOnMainSync(()->service.requestStop());
            until=SystemClock.elapsedRealtime()+18000;
            while(!"stopped".equals(service.getSnapshot().status) && SystemClock.elapsedRealtime()<until) SystemClock.sleep(50);
            assertEquals("stopped",service.getSnapshot().status);
            assertNativeFallback(true);
            getInstrumentation().runOnMainSync(()->context.startForegroundService(new Intent(context,RuntimeService.class).setAction(RuntimeService.START)));
            until=SystemClock.elapsedRealtime()+30000;
            while(!"ready".equals(service.getSnapshot().status) && SystemClock.elapsedRealtime()<until) SystemClock.sleep(50);
            assertEquals(service.getSnapshot().message,"ready",service.getSnapshot().status);
            assertTrue(service.isBackgroundAllowed());
            execute("from backend import mobile_runtime\n"
                + "s=mobile_runtime._runtime.app.state.runtime\n"
                + "assert s.safety.estop_active and not s.loop.autopilot\n"
                + "assert s.safety.current == {'A':0,'B':0}\n");
            until=SystemClock.elapsedRealtime()+15000;
            while(!pageReady(view) && SystemClock.elapsedRealtime()<until) SystemClock.sleep(100);
            assertTrue("New session cookie did not load the local page",pageReady(view));
            shell("input keyevent 223");
            try {
                until=SystemClock.elapsedRealtime()+18000;
                while(!"stopped".equals(service.getSnapshot().status) && SystemClock.elapsedRealtime()<until) SystemClock.sleep(50);
                assertEquals("Screen-off must stop even an allowed background session","stopped",service.getSnapshot().status);
            } finally { shell("input keyevent 224"); shell("wm dismiss-keyguard"); }
        } finally {
            if(service!=null) getInstrumentation().runOnMainSync(()->service.requestStop());
            if(runtime!=null) runtime.callAttr("stop");
            if(activity!=null) getInstrumentation().runOnMainSync(()->activity.finish());
            if(binding!=null) context.unbindService(binding);
            execute("factory.build_backend=original_factory\nmain.build_backend=original_main_factory\n");
        }
    }
    private void dismissStartupPairing(WebView view) throws Exception {
        String dialog="[role=\"dialog\"][aria-labelledby=\"android-pair-title\"]";
        assertTrue("Startup pairing decision did not complete",waitFor(view,
            "document.querySelector("+JSONObject.quote(dialog)+")!==null || sessionStorage.getItem('coyote.pairing-shown-session')!==null",4000));
        getInstrumentation().waitForIdleSync();
        if(!"true".equals(evaluate(view,"document.querySelector("+JSONObject.quote(dialog)+")!==null"))) return;
        assertEquals("Startup pairing must keep urgent stop reachable","true",evaluate(view,stopReachableExpression()));
        assertEquals("Pairing dialog must disable underlying navigation","true",evaluate(view,"document.querySelector('.android-header').inert"));
        capture("mobile-smoke-startup-pairing.png");
        tapElement(view,dialog+" .android-dialog-cancel");
        assertTrue("Cancel must close the startup pairing dialog",waitFor(view,"document.querySelector("+JSONObject.quote(dialog)+")===null",3000));
        assertEquals("Cancel must restore chat navigation","false",evaluate(view,"document.querySelector('.android-header').inert"));
        assertEquals("Cancel must not change output protection","true",evaluate(view,"document.querySelector('.android-resume-hint')!==null"));
    }
    private void verifyNavigationAndExternalBrowser(WebView view) throws Exception {
        assertNativeFallback(false);
        assertEquals("true",evaluate(view,"(()=>{document.querySelector('[data-coyote-menu]').click();return true;})()"));
        assertTrue(waitFor(view,"document.querySelector('[data-coyote-sidebar]')!==null",3000));
        SystemClock.sleep(200); capture("mobile-smoke-sidebar.png");
        getInstrumentation().runOnMainSync(()->activity.onBackPressed());
        assertTrue("System Back must close the drawer",waitFor(view,"document.querySelector('[data-coyote-sidebar]')===null",3000));
        evaluate(view,"document.querySelector('[data-coyote-menu]').click()");
        assertTrue(waitFor(view,"document.querySelector('[data-coyote-sidebar]')!==null",3000));
        evaluate(view,"window.__touchTrace=[];['pointerdown','pointerup','pointercancel'].forEach(type=>document.addEventListener(type,e=>window.__touchTrace.push({type,pointerType:e.pointerType,x:e.clientX,y:e.clientY}),true))");
        touch(view,"(()=>{const r=document.querySelector('[data-coyote-sidebar]').getBoundingClientRect();return {x:r.left+r.width-40,y:r.top+160,dx:-140};})()",true);
        boolean swipeClosed=waitFor(view,"document.querySelector('[data-coyote-sidebar]')===null",3000);
        assertTrue("A real left swipe must close the drawer: "+evaluate(view,"JSON.stringify(window.__touchTrace)"),swipeClosed);
        SystemClock.sleep(350);
        evaluate(view,"document.querySelector('[data-coyote-menu]').click()");
        assertTrue(waitFor(view,"document.querySelector('[data-coyote-sidebar]')!==null",3000));
        touch(view,"(()=>{const r=document.querySelector('.android-scrim').getBoundingClientRect();return {x:r.right-20,y:r.top+r.height/2};})()",false);
        assertTrue("A real tap on the right scrim must close the drawer",waitFor(view,"document.querySelector('[data-coyote-sidebar]')===null",3000));
        evaluate(view,"document.querySelector('[data-coyote-menu]').click()");
        assertTrue(waitFor(view,"document.querySelector('[data-coyote-settings]')!==null",3000));
        evaluate(view,"document.querySelector('[data-coyote-settings]').click()");
        assertTrue(waitFor(view,"document.querySelector('[data-coyote-settings-page]')!==null",3000));
        assertEquals("true",evaluate(view,"document.querySelectorAll('[data-coyote-estop]').length===1"));
        capture("mobile-smoke-settings.png");

        AtomicReference<Intent> externalIntent=new AtomicReference<>();
        Instrumentation.ActivityMonitor monitor=new Instrumentation.ActivityMonitor() {
            @Override public Instrumentation.ActivityResult onStartActivity(Intent intent) {
                if(Intent.ACTION_VIEW.equals(intent.getAction())) {
                    externalIntent.set(new Intent(intent));
                    return new Instrumentation.ActivityResult(Activity.RESULT_CANCELED,null);
                }
                return null;
            }
        };
        String originalUrl=evaluate(view,"location.href");
        getInstrumentation().addMonitor(monitor);
        try {
            // A real touch establishes WebView's user-gesture requirement. Intercept only
            // the outgoing system intent so the test never contacts a website or browser.
            evaluate(view,"document.querySelector('a[href=\"https://platform.deepseek.com/api_keys\"]').scrollIntoView({block:'center'})");
            SystemClock.sleep(150);
            tapElement(view,"a[href=\"https://platform.deepseek.com/api_keys\"]");
            long until=SystemClock.elapsedRealtime()+5000;
            while(externalIntent.get()==null && SystemClock.elapsedRealtime()<until) SystemClock.sleep(50);
            assertNotNull("API key link did not launch an external browser intent",externalIntent.get());
            assertEquals(Intent.ACTION_VIEW,externalIntent.get().getAction());
            assertTrue(externalIntent.get().hasCategory(Intent.CATEGORY_BROWSABLE));
            assertEquals("https://platform.deepseek.com/api_keys",externalIntent.get().getDataString());
            assertEquals("External website must not replace local WebView content",originalUrl,evaluate(view,"location.href"));
            assertEquals("ready",service.getSnapshot().status);

            evaluate(view,"document.querySelector('[data-coko-about]').scrollIntoView({block:'center'})");
            tapElement(view,"[data-coko-about]");
            assertTrue("About must show both feedback and public source links",waitFor(view,"document.querySelectorAll('.android-about a[href]').length===2",3000));
            assertEquals("About keeps emergency stop available","true",evaluate(view,stopReachableExpression()));
            for(String link:new String[]{"https://x.com/_Good_Dick_","https://github.com/romhuj/coko-dg"}) {
                externalIntent.set(null);
                String selector=".android-about a[href=\""+link+"\"]";
                evaluate(view,"document.querySelector("+JSONObject.quote(selector)+").scrollIntoView({block:'center'})");
                SystemClock.sleep(150);
                tapElement(view,selector);
                until=SystemClock.elapsedRealtime()+5000;
                while(externalIntent.get()==null && SystemClock.elapsedRealtime()<until) SystemClock.sleep(50);
                assertNotNull("About link did not launch an external browser intent",externalIntent.get());
                assertEquals(Intent.ACTION_VIEW,externalIntent.get().getAction());
                assertTrue(externalIntent.get().hasCategory(Intent.CATEGORY_BROWSABLE));
                assertEquals(link,externalIntent.get().getDataString());
                assertEquals("About links must preserve the local WebView",originalUrl,evaluate(view,"location.href"));
                assertEquals("ready",service.getSnapshot().status);
            }
            capture("mobile-smoke-about.png");
            getInstrumentation().runOnMainSync(()->activity.onBackPressed());
            assertTrue("System Back must return from About to settings",waitFor(view,"document.querySelector('.android-about')===null && document.querySelector('[data-coyote-settings-page]')!==null",3000));
        } finally {getInstrumentation().removeMonitor(monitor);}
        getInstrumentation().runOnMainSync(()->activity.onBackPressed());
        assertTrue("System Back must return from settings to chat",waitFor(view,"document.querySelector('[data-coyote-settings-page]')===null && document.querySelector('textarea')!==null",3000));
        evaluate(view,"[...document.querySelectorAll('.android-tabs button')].find(b=>b.textContent.trim()==='设备').click()");
        assertTrue(waitFor(view,"document.querySelector('main[aria-label=\"设备面板\"]')!==null",3000));
        SystemClock.sleep(200); capture("mobile-smoke-device.png");
        evaluate(view,"document.querySelector('[data-coyote-menu]').click()");
        assertTrue(waitFor(view,"document.querySelector('[data-coyote-sidebar]')!==null",3000));
        evaluate(view,"[...document.querySelectorAll('[data-coyote-sidebar] button')].find(b=>b.textContent.trim()==='角色').click()");
        assertTrue(waitFor(view,"document.querySelector('main[aria-label=\"角色面板\"]')!==null",3000));
        SystemClock.sleep(250); capture("mobile-smoke-roles.png");
        verifyRoleManagement(view);
        verifyCustomRoleCreation(view);
        evaluate(view,"document.querySelector('.android-role-search').click()");
        assertTrue(waitFor(view,"document.querySelector('[data-role-create-option=\"search\"]')!==null",3000));
        tapElement(view,"[data-role-create-option=\"search\"]");
        assertTrue(waitFor(view,"document.querySelector('[role=\"dialog\"]')!==null",3000));
        assertEquals("Role search must make the underlying global navigation inert","true",evaluate(view,"document.querySelector('.android-header').inert"));
        assertEquals("Role search must leave the global emergency stop reachable","true",evaluate(view,stopReachableExpression()));
        capture("mobile-smoke-role-search.png");
        tapElement(view,"[data-coyote-estop]");
        assertTrue("Urgent stop touch must work while role search is open",waitFor(view,"document.querySelector('.android-toast')!==null",3000));
        getInstrumentation().runOnMainSync(()->activity.onBackPressed());
        assertTrue(waitFor(view,"document.querySelector('[role=\"dialog\"]')===null",3000));
        getInstrumentation().runOnMainSync(()->activity.onBackPressed());
        assertTrue(waitFor(view,"document.querySelector('main[aria-label=\"角色面板\"]')===null",3000));
    }
    private void verifyCustomRoleCreation(WebView view) throws Exception {
        assertEquals("Create entry label","\"创建角色\"",evaluate(view,"document.querySelector('.android-role-search').textContent.trim()"));
        tapElement(view,".android-role-search");
        assertTrue(waitFor(view,"document.querySelector('[data-role-create-option=\"custom\"]')!==null && document.querySelector('[data-role-create-option=\"search\"]')!==null",3000));
        assertEquals("true",evaluate(view,stopReachableExpression()));
        capture("mobile-smoke-role-create-choice.png");
        tapElement(view,"[data-role-create-option=\"custom\"]");
        assertTrue(waitFor(view,"document.querySelector('#role-custom-personality')!==null",3000));
        fillInput(view,"role-custom-name","自定义离线角色");
        fillInput(view,"role-custom-personality","冷静、耐心，有自己的判断，结合情景回应。");
        fillInput(view,"role-custom-background","虚构图书馆管理员，喜欢简洁的表达。");
        capture("mobile-smoke-role-custom.png");
        assertEquals("true",evaluate(view,stopReachableExpression()));
        evaluate(view,"document.querySelector('[data-role-custom-submit]').scrollIntoView({block:'center'})");
        tapElement(view,"[data-role-custom-submit]");
        assertTrue("Custom role creation did not finish",waitFor(view,"document.querySelector('[role=\"dialog\"]')===null && [...document.querySelectorAll('.android-role-row strong')].some(e=>e.textContent.trim()==='自定义离线角色')",5000));
        execute("r=mobile_runtime._runtime\n"
            + "s=r.app.state.runtime\n"
            + "created=s.cfg['character']\n"
            + "assert created['name']=='自定义离线角色'\n"
            + "assert '冷静、耐心' in created['prompt'] and '虚构图书馆管理员' in created['prompt']\n"
            + "entry=load_custom_roles(r.data_root)[created['role']]\n"
            + "assert entry['is_custom'] and not entry.get('sources')\n");
    }
    private void fillInput(WebView view,String id,String value) throws Exception {
        evaluate(view,"(()=>{const e=document.getElementById("+JSONObject.quote(id)+");const p=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,"+JSONObject.quote(value)+");e.dispatchEvent(new Event('input',{bubbles:true}));})()");
        getInstrumentation().waitForIdleSync();
    }
    private void verifyRoleManagement(WebView view) throws Exception {
        // Seed only this clean emulator's private fixture through the existing
        // runtime queue. The role source is never fetched and has no model call.
        execute("from backend import mobile_runtime\n"
            + "from backend.role_library import save_custom_role, load_custom_roles\n"
            + "from backend.config import save_character_runtime\n"
            + "async def seed_ui_role():\n"
            + " r=mobile_runtime._runtime\n"
            + " s=r.app.state.runtime\n"
            + " async with s.loop.conversation_edit():\n"
            + "  role=save_custom_role(r.data_root,'离线角色测试',{'title':'Offline fixture','summary':'A fictional test role.','url':'https://example.invalid/fixture'})\n"
            + "  save_character_runtime(s.cfg,role=role,profile='角色扮演')\n"
            + " await s.broadcast()\n"
            + " return role\n"
            + "ui_role_id=asyncio.run_coroutine_threadsafe(seed_ui_role(),mobile_runtime._runtime.loop).result(5)\n");
        String fixture="[...document.querySelectorAll('.android-role-swipe')].find(e=>e.querySelector('strong').textContent.trim()==='离线角色测试')";
        assertTrue("Offline custom role did not reach UI",waitFor(view,"Boolean("+fixture+")",4000));
        assertEquals("Built-in assistant must not expose deletion","true",evaluate(view,"(()=>{const e=[...document.querySelectorAll('.android-role-swipe')].find(e=>e.querySelector('strong').textContent.trim()==='情景助手');return !!e&&!e.querySelector('.android-role-delete');})()"));
        String swipePoint="(()=>{const e="+fixture+";e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.right-28,y:r.top+r.height/2,dx:-180};})()";
        touch(view,swipePoint,true);
        assertTrue("Left swipe did not reveal pin/delete",waitFor(view,"Boolean(("+fixture+")?.classList.contains('revealed'))",3000));
        capture("mobile-smoke-role-actions.png");
        tapElement(view,".android-role-swipe.revealed .android-role-pin");
        assertTrue("Pin did not update the role",waitFor(view,"Boolean(("+fixture+")?.querySelector('.android-role-pinned-indicator'))",4000));
        assertTrue("Pin request did not unlock the role",waitFor(view,"Boolean(("+fixture+")&&!(("+fixture+").querySelector('.android-role-row').disabled)&&!(("+fixture+").classList.contains('revealed')))",3000));
        SystemClock.sleep(200);
        execute("assert load_custom_roles(mobile_runtime._runtime.data_root)[ui_role_id]['pinned'] is True\n");
        assertEquals("Pinned role must sort first","true",evaluate(view,"document.querySelector('.android-role-list .android-role-row strong').textContent.trim()==='离线角色测试'"));
        // Trace only the synthetic role: no chat text, credentials, or real role data.
        evaluate(view,"(()=>{const row="+fixture+";window.__fixtureRoleTrace=[];['pointerdown','pointerup','pointercancel','click'].forEach(type=>[true,false].forEach(capture=>row.addEventListener(type,event=>{window.__fixtureRoleTrace.push({type,phase:capture?'capture':'bubble',tag:event.target.tagName,cls:event.target.getAttribute?.('class')||'',time:Math.round(performance.now()),x:event.clientX,y:event.clientY});if(window.__fixtureRoleTrace.length>40)window.__fixtureRoleTrace.shift();},capture)));return true;})()");
        touch(view,swipePoint,true);
        assertTrue(waitFor(view,"Boolean(("+fixture+")?.classList.contains('revealed'))",3000));
        tapElement(view,".android-role-swipe.revealed .android-role-delete");
        boolean deleteDialogOpened=waitFor(view,"document.querySelector('.android-role-delete-dialog[role=\"dialog\"]')!==null",3000);
        String deleteDiagnostic="";
        if(!deleteDialogOpened) {
            deleteDiagnostic=evaluate(view,"(()=>{const row="+fixture+";const button=row?.querySelector('.android-role-delete');const rect=button?.getBoundingClientRect();const hit=rect?document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2):null;return JSON.stringify({fixturePresent:!!row,revealed:!!row?.classList.contains('revealed'),rowDisabled:row?.querySelector('.android-role-row')?.disabled,deleteDisabled:button?.disabled,rowInert:!!row?.closest('[inert]'),deleteInert:!!button?.closest('[inert]'),error:document.querySelector('.android-roles .android-compose-error')?.textContent?.slice(0,300)||'',dialogs:document.querySelectorAll('[role=\"dialog\"]').length,overlays:[...document.querySelectorAll('.android-role-delete-overlay,.android-role-create-overlay,[data-coyote-sidebar]')].map(e=>({cls:e.getAttribute('class'),inert:e.inert})),hit:hit?{tag:hit.tagName,cls:hit.getAttribute('class')||'',insideDelete:!!button?.contains(hit)}:null,trace:window.__fixtureRoleTrace||[]});})()");
            try { capture("mobile-smoke-role-delete-failure.png"); }
            catch(Exception | AssertionError captureError) { deleteDiagnostic += "; screenshot unavailable: "+captureError.getClass().getSimpleName(); }
        }
        assertTrue("Delete must open an accessible React confirmation"+(deleteDiagnostic.isEmpty()?"":": "+deleteDiagnostic),deleteDialogOpened);
        assertEquals("Delete confirmation must disable underlying navigation","true",evaluate(view,"document.querySelector('.android-header').inert"));
        assertEquals("Urgent stop must remain reachable during deletion confirmation","true",evaluate(view,stopReachableExpression()));
        capture("mobile-smoke-role-delete.png");
        tapElement(view,"[data-coyote-estop]");
        assertTrue(waitFor(view,"document.querySelector('.android-toast')!==null",3000));
        tapElement(view,".android-role-delete-buttons button:first-child");
        assertTrue("Cancel must close deletion confirmation",waitFor(view,"document.querySelector('.android-role-delete-dialog')===null",3000));
        assertEquals("Cancel must keep the role","true",evaluate(view,"Boolean("+fixture+")"));
        execute("assert ui_role_id in load_custom_roles(mobile_runtime._runtime.data_root)\n"
            + "assert (mobile_runtime._runtime.data_root / 'content' / 'custom-roles' / (ui_role_id+'.md')).is_file()\n");
        // Cancel intentionally leaves the row actions visible for a second choice.
        tapElement(view,".android-role-swipe.revealed .android-role-delete");
        assertTrue(waitFor(view,"document.querySelector('.android-role-delete-dialog')!==null",3000));
        tapElement(view,".android-role-delete-buttons .danger");
        assertTrue("Confirmed deletion did not remove the role",waitFor(view,"document.querySelector('.android-role-delete-dialog')===null && !("+fixture+")",5000));
        execute("r=mobile_runtime._runtime\n"
            + "s=r.app.state.runtime\n"
            + "assert ui_role_id not in load_custom_roles(r.data_root)\n"
            + "assert not (r.data_root / 'content' / 'custom-roles' / (ui_role_id+'.md')).exists()\n"
            + "assert s.cfg['character']['role'] != ui_role_id\n"
            + "assert s.safety.estop_active and not s.loop.autopilot\n");
    }
    private void verifyChatResumeAndActionReceipts(WebView view) throws Exception {
        assertTrue("Locked chat should show an inline resume action",waitFor(view,"document.querySelector('.android-resume-hint button')!==null",3000));
        tapElement(view,".android-resume-hint button");
        assertTrue("One inline resume touch must clear the protection hint",waitFor(view,"document.querySelector('.android-resume-hint')===null",3000));
        execute("from backend import mobile_runtime\n"
            + "s=mobile_runtime._runtime.app.state.runtime\n"
            + "assert not s.safety.estop_active and not s.loop.autopilot\n"
            + "assert s.safety.current == {'A':0,'B':0}\n");
        tapElement(view,"[data-coyote-estop]");
        assertTrue("Global stop must restore the inline protection hint",waitFor(view,"document.querySelector('.android-resume-hint button')!==null",3000));
        // Test-only synthetic receipts exercise presentation. No action executor,
        // model endpoint, real relay, or device method runs for these examples.
        execute("r=mobile_runtime._runtime\n"
            + "s=r.app.state.runtime\n"
            + "fixture={'line':'离线回执演示：以下是界面测试数据，未连接设备。','executed':["
            + "{'label':'test hold','reason':'test fixture','sent':True,'command':{'kind':'hold','channel':'A','value':54}},"
            + "{'label':'test wave','reason':'test fixture','sent':True,'command':{'kind':'pulse','channel':'A','pattern':'呼吸','duration_s':9}},"
            + "{'label':'test add','reason':'test fixture','sent':True,'command':{'kind':'add','channel':'B','delta':15}}], 'dropped':[]}\n"
            + "asyncio.run_coroutine_threadsafe(s.broadcast_chat(fixture),r.loop).result(5)\n"
            + "assert s.safety.current == {'A':0,'B':0}\n"
            + "assert s.safety.estop_active and not s.loop.autopilot\n");
        assertTrue("Sent receipt chips did not render",waitFor(view,"document.querySelectorAll('.android-command-chip').length===3",4000));
        assertEquals("true",evaluate(view,"(()=>{const labels=[...document.querySelectorAll('.android-command-chip')].map(e=>e.textContent);return labels.includes('A 强度 54')&&labels.includes('A 呼吸 9 秒')&&labels.includes('B 增加 15');})()"));
        assertEquals("true",evaluate(view,stopReachableExpression()));
        capture("mobile-smoke-chat-actions.png");
    }
    private String stopReachableExpression() {
        return "(()=>{const e=document.querySelector('[data-coyote-estop]'),r=e.getBoundingClientRect();return r.top>=0&&r.bottom<100&&!e.closest('[inert]')&&document.elementFromPoint(r.left+r.width/2,r.top+r.height/2).closest('[data-coyote-estop]')===e;})()";
    }
    private void verifyRapidChatSubmit(WebView view) throws Exception {
        execute("s=mobile_runtime._runtime.app.state.runtime\n"
            + "original_ui_chat=s.llm.chat\nui_chat_calls=0\n"
            + "async def fake_ui_chat(*args,**kwargs):\n"
            + " global ui_chat_calls\n ui_chat_calls+=1\n"
            + " await asyncio.sleep(.3)\n return '离线快速发送验证完成',[]\n"
            + "s.llm.chat=fake_ui_chat\n");
        try {
            fillInput(view,"chat-message","快速连续提交测试");
            evaluate(view,"(()=>{const form=document.querySelector('.android-composer');form.requestSubmit();form.requestSubmit();})()");
            assertTrue("Rapid submission should receive one reply",waitFor(view,"[...document.querySelectorAll('.android-message.ai')].some(e=>e.textContent.includes('离线快速发送验证完成'))",5000));
            execute("assert ui_chat_calls==1, ui_chat_calls\n");
            assertEquals("One user bubble per submission","1",evaluate(view,"[...document.querySelectorAll('.android-message.user')].filter(e=>e.textContent.includes('快速连续提交测试')).length"));
        } finally {execute("s.llm.chat=original_ui_chat\n");}
    }
    private void tapElement(WebView view,String selector) throws Exception {
        assertTrue("Tap target is not yet exposed: "+selector,waitFor(view,
            "(()=>{const e=document.querySelector("+JSONObject.quote(selector)+");if(!e||e.disabled||e.closest('[inert]'))return false;const r=e.getBoundingClientRect();const h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return !!h&&(h===e||e.contains(h));})()",4000));
        touch(view,"(()=>{const r=document.querySelector("+JSONObject.quote(selector)+").getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()",false);
    }
    private void touch(WebView view,String pointExpression,boolean swipe) throws Exception {
        JSONObject point=new JSONObject(evaluate(view,pointExpression));
        double scale=Double.parseDouble(evaluate(view,"devicePixelRatio"));
        AtomicReference<int[]> location=new AtomicReference<>();
        getInstrumentation().runOnMainSync(()->{int[] result=new int[2];view.getLocationOnScreen(result);location.set(result);});
        float x=(float)(location.get()[0]+point.getDouble("x")*scale);
        float y=(float)(location.get()[1]+point.getDouble("y")*scale);
        float dx=swipe?(float)(point.getDouble("dx")*scale):0;
        long at=SystemClock.uptimeMillis();
        sendTouch(at,MotionEvent.ACTION_DOWN,x,y);
        if(swipe) for(int i=1;i<=10;i++) {SystemClock.sleep(20);sendTouch(at,MotionEvent.ACTION_MOVE,x+dx*i/10,y);}
        else SystemClock.sleep(60);
        sendTouch(at,MotionEvent.ACTION_UP,x+dx,y);
        if(swipe) SystemClock.sleep(220); // Allow the row's CSS reveal transition to settle before tapping its actions.
    }
    private void sendTouch(long downTime,int action,float x,float y) {
        MotionEvent.PointerProperties finger=new MotionEvent.PointerProperties(); finger.id=0; finger.toolType=MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords point=new MotionEvent.PointerCoords(); point.x=x; point.y=y; point.pressure=1; point.size=1;
        MotionEvent event=MotionEvent.obtain(downTime,SystemClock.uptimeMillis(),action,1,
            new MotionEvent.PointerProperties[]{finger},new MotionEvent.PointerCoords[]{point},0,0,1,1,0,0,InputDevice.SOURCE_TOUCHSCREEN,0);
        try {getInstrumentation().sendPointerSync(event);} finally {event.recycle();}
    }
    private void assertNativeFallback(boolean visible) {
        AtomicReference<Boolean> actual=new AtomicReference<>();
        getInstrumentation().runOnMainSync(()->{
            View button=activity.getWindow().getDecorView().findViewWithTag("native-estop");
            actual.set(button!=null && button.getVisibility()==View.VISIBLE);
        });
        assertEquals("Native emergency action visibility",visible,actual.get().booleanValue());
    }
    private boolean waitFor(WebView view,String expression,long timeout) throws Exception {
        long until=SystemClock.elapsedRealtime()+timeout;
        do {if("true".equals(evaluate(view,expression)))return true;SystemClock.sleep(50);} while(SystemClock.elapsedRealtime()<until);
        return false;
    }
    private String evaluate(WebView view,String script) throws Exception {
        CountDownLatch completed=new CountDownLatch(1); AtomicReference<String> value=new AtomicReference<>();
        getInstrumentation().runOnMainSync(()->view.evaluateJavascript(script,result->{value.set(result);completed.countDown();}));
        assertTrue("Local page script did not complete",completed.await(3,TimeUnit.SECONDS)); return value.get();
    }
    private void shell(String command) throws Exception {
        try(ParcelFileDescriptor descriptor=getInstrumentation().getUiAutomation().executeShellCommand(command);
            InputStream result=new ParcelFileDescriptor.AutoCloseInputStream(descriptor)) {
            byte[] buffer=new byte[1024]; while(result.read(buffer)!=-1) { }
        }
    }
    private void execute(String script) { Python.getInstance().getModule("builtins").callAttr("exec",script,globals); }
    private void capture(String name) throws Exception {
        WebView view=findWebView();
        if(view!=null) {
            CountDownLatch drawn=new CountDownLatch(1);
            getInstrumentation().runOnMainSync(()->view.postVisualStateCallback(1,new WebView.VisualStateCallback() {
                @Override public void onComplete(long requestId) {drawn.countDown();}
            }));
            assertTrue("WebView compositor did not settle before screenshot",drawn.await(5,TimeUnit.SECONDS));
            getInstrumentation().waitForIdleSync(); SystemClock.sleep(500);
        }
        Bitmap screenshot=getInstrumentation().getUiAutomation().takeScreenshot();
        assertNotNull(screenshot);
        try(OutputStream output=new FileOutputStream(new File(context.getExternalFilesDir(null),name))) {
            assertTrue(screenshot.compress(Bitmap.CompressFormat.PNG,100,output));
        } finally { screenshot.recycle(); }
    }
    private void assertSafeState(JSONObject state) throws Exception {
        assertEquals("android",state.getString("platform")); assertTrue(state.getBoolean("estop")); assertFalse(state.getBoolean("autopilot"));
        assertEquals(0,state.getJSONObject("current").getInt("A")); assertEquals(0,state.getJSONObject("current").getInt("B"));
        for(String key:new String[]{"camera","audio","ble","dungeon"}) assertFalse(state.getJSONObject("capabilities").getBoolean(key));
    }
    private WebView findWebView() {
        AtomicReference<WebView> found=new AtomicReference<>();
        getInstrumentation().runOnMainSync(()->found.set(find(activity.getWindow().getDecorView()))); return found.get();
    }
    private WebView find(View view) {
        if(view instanceof WebView) return (WebView)view;
        if(view instanceof ViewGroup) for(int i=0;i<((ViewGroup)view).getChildCount();i++) { WebView result=find(((ViewGroup)view).getChildAt(i)); if(result!=null)return result; }
        return null;
    }
    private boolean pageReady(WebView view) throws Exception {
        CountDownLatch completed=new CountDownLatch(1); AtomicReference<String> value=new AtomicReference<>();
        getInstrumentation().runOnMainSync(()->view.evaluateJavascript(
            "location.hostname==='127.0.0.1' && document.querySelector('textarea')!==null && document.querySelector('[data-coyote-estop]')!==null && !document.cookie.includes('coyote_session')",
            result->{ value.set(result); completed.countDown(); }));
        return completed.await(3,TimeUnit.SECONDS) && "true".equals(value.get());
    }
    private static final class Response {
        final int status; final String body; Response(int status,String body){this.status=status;this.body=body;}
    }
    private Response request(int port,String method,String path,boolean authenticated,String body) throws Exception {
        String origin="http://127.0.0.1:"+port;
        HttpURLConnection connection=(HttpURLConnection)new URL(origin+path).openConnection();
        try {
            connection.setConnectTimeout(5000); connection.setReadTimeout(5000); connection.setInstanceFollowRedirects(false);
            connection.setRequestMethod(method); connection.setRequestProperty("Origin",origin);
            if(authenticated) connection.setRequestProperty("Cookie","coyote_session="+token);
            if(body!=null) { connection.setDoOutput(true); connection.setRequestProperty("Content-Type","application/json"); try(OutputStream out=connection.getOutputStream()){out.write(body.getBytes(StandardCharsets.UTF_8));} }
            int status=connection.getResponseCode(); InputStream input=status>=400?connection.getErrorStream():connection.getInputStream();
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();
            if(input!=null) try(InputStream stream=input) {byte[] buffer=new byte[8192];int count;while((count=stream.read(buffer))!=-1)bytes.write(buffer,0,count);}
            return new Response(status,bytes.toString("UTF-8"));
        } finally {connection.disconnect();}
    }
    private void copyAsset(String path,File target) throws Exception {
        String[] children=context.getAssets().list(path);
        if(children!=null && children.length>0) { assertTrue(target.isDirectory()||target.mkdirs()); for(String child:children)copyAsset(path+"/"+child,new File(target,child)); }
        else try(InputStream input=context.getAssets().open(path);OutputStream output=new FileOutputStream(target)) {byte[] buffer=new byte[32768];int count;while((count=input.read(buffer))!=-1)output.write(buffer,0,count);}
    }
}
