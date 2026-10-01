package org.coyote.mobile;

import android.app.Activity;
import android.app.Instrumentation;
import android.os.SystemClock;
import android.webkit.WebView;
import org.json.JSONObject;
import java.lang.reflect.Field;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import junit.framework.Assert;

/** Called only by the clean-emulator smoke harness; all speech capture is fake. */
final class VoiceBridgeSmoke extends Assert {
    private final Instrumentation instrumentation;
    private final Activity activity;
    private final WebView web;
    private final String origin;
    private final FakeEngine fake = new FakeEngine();
    private VoiceBridge bridge;
    private static final class FakeEngine implements VoiceBridge.Engine {
        ContinuousVoiceRecognizer.Listener listener;
        volatile int starts, stops;
        public void start(String session) { starts++; listener.onState(session,"listening",100,""); }
        public void stop() { stops++; }
        public void close() { stop(); }
    }
    static void verify(Instrumentation instrumentation, Activity activity, WebView web, String origin) throws Exception {
        new VoiceBridgeSmoke(instrumentation,activity,web,origin).run();
    }
    private VoiceBridgeSmoke(Instrumentation instrumentation, Activity activity, WebView web, String origin) {
        this.instrumentation=instrumentation; this.activity=activity; this.web=web; this.origin=origin;
    }
    private void run() throws Exception {
        assertTrue("Voice smoke must run only on an emulator",android.os.Build.FINGERPRINT.contains("generic")
            || android.os.Build.MODEL.contains("sdk_gphone") || android.os.Build.HARDWARE.contains("ranchu"));
        instrumentation.getUiAutomation().grantRuntimePermission(activity.getPackageName(),"android.permission.RECORD_AUDIO");
        js("window.voiceSmokeOldDocument=true;true");
        Field field=MainActivity.class.getDeclaredField("voice"); field.setAccessible(true);
        AtomicReference<Throwable> failure=new AtomicReference<>();
        instrumentation.runOnMainSync(()->{
            try {
                android.util.Log.i("VoiceSmoke","bridge-replacing");
                ((VoiceBridge)field.get(activity)).close();
                android.util.Log.i("VoiceSmoke","original-bridge-closed");
                bridge=new VoiceBridge(activity,web,(context,listener)->{fake.listener=listener;return fake;});
                field.set(activity,bridge); bridge.installOrigin(origin); bridge.onResume(); web.reload();
            } catch(Exception error) { failure.set(error); }
        });
        assertNull(failure.get());
        try {
            android.util.Log.i("VoiceSmoke","bridge-installed");
            waitJs("!window.voiceSmokeOldDocument && !!window.CoyoteVoice && !!document.querySelector('[data-coyote-estop]')");
            js("window.voiceSmokeEvents=[];window.CoyoteVoice.onmessage=e=>window.voiceSmokeEvents.push(JSON.parse(e.data));true");
            send("start","voice_smoke_001");
            android.util.Log.i("VoiceSmoke","first-start-sent");
            waitJs("window.voiceSmokeEvents.some(e=>e.status==='listening')");
            assertEquals(1,fake.starts);
            android.util.Log.i("VoiceSmoke","first-listening");
            send("start","voice_smoke_001");
            js("window.CoyoteVoice.postMessage('not-json');window.CoyoteVoice.postMessage(JSON.stringify({type:'start',sessionId:'bad'}));true");
            SystemClock.sleep(150); assertEquals("Duplicate or malformed starts must not reopen capture",1,fake.starts);
            js("(()=>{const f=document.createElement('iframe');f.id='voice-smoke-frame';f.srcdoc='<p>frame</p>';document.body.appendChild(f);return true})()");
            android.util.Log.i("VoiceSmoke","subframe-created");
            waitJs("!!document.getElementById('voice-smoke-frame')?.contentWindow?.CoyoteVoice");
            js("document.getElementById('voice-smoke-frame').contentWindow.CoyoteVoice.postMessage(JSON.stringify({type:'start',sessionId:'voice_frame_001'}));true");
            SystemClock.sleep(150); assertEquals("Subframes cannot control the microphone",1,fake.starts);
            send("stop","voice_smoke_001");
            android.util.Log.i("VoiceSmoke","subframe-rejected");
            send("start","voice_smoke_002");
            waitJs("window.voiceSmokeEvents.some(e=>e.sessionId==='voice_smoke_002'&&e.status==='listening')");
            fake.listener.onSentence("voice_smoke_001","late-old","过期的语音");
            String words="引号\"与换行\n<script>window.voiceInjected=true</script>";
            fake.listener.onSentence("voice_smoke_002","current-1",words);
            waitJs("window.voiceSmokeEvents.some(e=>e.utteranceId==='current-1')");
            assertEquals("Stale capture must not leak into the current conversation","1",js("window.voiceSmokeEvents.filter(e=>e.type==='sentence').length"));
            assertEquals(words,new org.json.JSONArray("["+js("window.voiceSmokeEvents.find(e=>e.utteranceId==='current-1').text")+"]").getString(0));
            assertEquals("Voice text must be data, never executable script","false",js("window.voiceInjected===true"));
            fake.listener.onLevel("voice_smoke_001",.9f);
            fake.listener.onPreview("voice_smoke_001","过期的临时预览");
            fake.listener.onLevel("voice_smoke_002",.4f);
            fake.listener.onPreview("voice_smoke_002","实时转写<script>window.voiceInjected=true</script>");
            waitJs("window.voiceSmokeEvents.some(e=>e.type==='preview'&&e.text.startsWith('实时转写'))");
            assertEquals("Live preview never becomes a final sentence","1",js("window.voiceSmokeEvents.filter(e=>e.type==='sentence').length"));
            assertEquals("Stale live events must be rejected","false",js("window.voiceSmokeEvents.some(e=>e.type==='preview'&&e.text==='过期的临时预览')"));
            assertEquals("Meter level reaches the page","true",js("window.voiceSmokeEvents.some(e=>e.type==='level'&&Math.abs(e.level-.4)<.001)"));
            assertEquals("Preview is text data, not script","false",js("window.voiceInjected===true"));
            instrumentation.runOnMainSync(()->bridge.onPause());
            android.util.Log.i("VoiceSmoke","background-callback-test");
            send("start","voice_paused_001");
            fake.listener.onSentence("voice_smoke_002","background-1","后台继续识别的语音");
            waitJs("window.voiceSmokeEvents.some(e=>e.utteranceId==='background-1')");
            SystemClock.sleep(150);
            assertEquals("Paused app cannot start recording",2,fake.starts);
            assertEquals("An already-started capture continues after app switching","2",js("window.voiceSmokeEvents.filter(e=>e.type==='sentence').length"));
            fake.listener.onState("voice_smoke_002","off",0,"锁屏停止");
            waitJs("window.voiceSmokeEvents.some(e=>e.sessionId==='voice_smoke_002'&&e.status==='off')");
            fake.listener.onSentence("voice_smoke_002","late-lock","锁屏后过期的语音");
            SystemClock.sleep(150);
            assertEquals("A locked/stopped session cannot publish late words","2",js("window.voiceSmokeEvents.filter(e=>e.type==='sentence').length"));
            instrumentation.runOnMainSync(()->bridge.onResume());
            assertEquals("Returning to the app cannot silently restart recording",2,fake.starts);
        } finally {
            android.util.Log.i("VoiceSmoke","bridge-restoring");
            js("window.voiceSmokeOldDocument=true;true");
            instrumentation.runOnMainSync(()->{
                bridge.close();
                try {
                    VoiceBridge restored=new VoiceBridge(activity,web);
                    field.set(activity,restored); restored.installOrigin(origin); restored.onResume(); web.reload();
                } catch(Exception error) { failure.set(error); }
            });
            assertNull(failure.get());
            waitJs("!window.voiceSmokeOldDocument && !!window.CoyoteVoice && !!document.querySelector('[data-coyote-estop]')");
        }
    }
    private void send(String type,String session) throws Exception {
        JSONObject message=new JSONObject(); message.put("type",type);message.put("sessionId",session);
        js("window.CoyoteVoice.postMessage("+JSONObject.quote(message.toString())+");true");
    }
    private String js(String script) throws Exception {
        AtomicReference<String> result=new AtomicReference<>(); CountDownLatch done=new CountDownLatch(1);
        instrumentation.runOnMainSync(()->web.evaluateJavascript(script,value->{result.set(value);done.countDown();}));
        assertTrue("Voice smoke script did not complete",done.await(4,TimeUnit.SECONDS));return result.get();
    }
    private void waitJs(String script) throws Exception {
        long until=SystemClock.elapsedRealtime()+12000;
        while(SystemClock.elapsedRealtime()<until) {if("true".equals(js(script)))return;SystemClock.sleep(80);}
        fail("Voice smoke condition did not become true: "+script);
    }
}
