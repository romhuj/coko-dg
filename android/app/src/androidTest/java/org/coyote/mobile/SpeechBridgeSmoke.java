package org.coyote.mobile;

import android.app.Activity;
import android.app.Instrumentation;
import android.net.Uri;
import android.os.SystemClock;
import android.webkit.WebView;
import org.json.JSONObject;
import java.lang.reflect.Field;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import junit.framework.Assert;

/** Fake playback only: no TTS, model download or microphone is started. */
final class SpeechBridgeSmoke extends Assert {
    private final Instrumentation instrumentation;
    private final Activity activity;
    private final WebView web;
    private final String origin;
    private final FakeEngine fake = new FakeEngine();
    private SpeechBridge bridge;
    private static final class FakeEngine implements SpeechBridge.Engine {
        ReplySpeech.Listener listener;
        volatile int enables, speaks, stops;
        volatile String voice, text;
        volatile boolean replaced;
        public void enable(String session,String voiceId) { enables++;voice=voiceId;listener.state(session,"ready",null,""); }
        public void speak(String session,String utterance,String words) { speaks++;text=words;listener.state(session,"speaking",utterance,""); }
        public void speak(String session,String utterance,String words,boolean replace) {replaced=replace;speak(session,utterance,words);}
        public void stop() { stops++; }
        public void disable(String message) { stops++; }
        public void close() { stops++; }
    }
    static void verify(Instrumentation instrumentation,Activity activity,WebView web,String origin) throws Exception {
        new SpeechBridgeSmoke(instrumentation,activity,web,origin).run();
    }
    private SpeechBridgeSmoke(Instrumentation instrumentation,Activity activity,WebView web,String origin) {
        this.instrumentation=instrumentation;this.activity=activity;this.web=web;this.origin=origin;
    }
    private void run() throws Exception {
        assertTrue("Emulator only",android.os.Build.FINGERPRINT.contains("generic") || android.os.Build.MODEL.contains("sdk_gphone") || android.os.Build.HARDWARE.contains("ranchu"));
        verifyProtocol();
        js("window.speechSmokeOldDocument=true;true");
        Field field=MainActivity.class.getDeclaredField("speech");field.setAccessible(true);
        AtomicReference<Throwable> failure=new AtomicReference<>();
        instrumentation.runOnMainSync(()->{
            try {
                ((SpeechBridge)field.get(activity)).close();
                bridge=new SpeechBridge(activity,web,(context,listener)->{fake.listener=listener;return fake;});
                field.set(activity,bridge);bridge.installOrigin(origin);bridge.onResume();web.reload();
            } catch(Exception error) { failure.set(error); }
        });
        assertNull(failure.get());
        try {
            waitJs("!window.speechSmokeOldDocument && !!window.CoyoteSpeech && !!document.querySelector('[data-coyote-estop]')");
            js("window.speechSmokeEvents=[];window.CoyoteSpeech.onmessage=e=>window.speechSmokeEvents.push(JSON.parse(e.data));true");
            send("enable","speech_smoke_001",null,null,"melo-zh");
            waitJs("window.speechSmokeEvents.some(e=>e.status==='ready')");
            assertEquals(1,fake.enables);assertEquals("melo-zh",fake.voice);
            send("enable","speech_smoke_001",null,null,"melo-zh");
            send("enable","speech_smoke_001",null,null,"kokoro-zm_010");
            js("window.CoyoteSpeech.postMessage('not-json');window.CoyoteSpeech.postMessage(JSON.stringify({type:'enable',sessionId:'speech_invalid_001',voiceId:'foreign-url'}));true");
            SystemClock.sleep(150);assertEquals("Duplicate or invalid enables have no effect",1,fake.enables);
            js("(()=>{const f=document.createElement('iframe');f.id='speech-smoke-frame';f.srcdoc='<p>frame</p>';document.body.appendChild(f);return true})()");
            waitJs("!!document.getElementById('speech-smoke-frame')?.contentWindow?.CoyoteSpeech");
            js("document.getElementById('speech-smoke-frame').contentWindow.CoyoteSpeech.postMessage(JSON.stringify({type:'enable',sessionId:'speech_frame_001',voiceId:'melo-zh'}));true");
            SystemClock.sleep(150);assertEquals("Subframes cannot start playback",1,fake.enables);
            String words="引号与<script>window.speechInjected=true</script>";
            send("speak","speech_smoke_001","words-1",words,null);
            waitJs("window.speechSmokeEvents.some(e=>e.status==='speaking'&&e.utteranceId==='words-1')");
            assertEquals(words,fake.text);
            assertTrue("Bridge forwards the replacement flag",fake.replaced);
            fake.listener.state("speech_smoke_001","ready","words-1",words);
            waitJs("window.speechSmokeEvents.some(e=>e.status==='ready'&&e.utteranceId==='words-1')");
            assertEquals("Text is data only","false",js("window.speechInjected===true"));
            fake.listener.state("speech_smoke_001","interrupted","words-1","");
            waitJs("window.speechSmokeEvents.some(e=>e.type==='interrupted'&&e.utteranceId==='words-1')");
            send("disable","speech_smoke_001",null,null,null);
            send("enable","speech_smoke_002",null,null,"kokoro-zf_001");
            waitJs("window.speechSmokeEvents.some(e=>e.sessionId==='speech_smoke_002'&&e.status==='ready')");
            fake.listener.state("speech_smoke_001","speaking","stale","");
            send("enable","speech_smoke_001",null,null,"melo-zh");
            send("speak","speech_smoke_001","stale","旧角色回复",null);
            instrumentation.runOnMainSync(()->bridge.onPause());
            send("enable","speech_paused_001",null,null,"melo-zh");
            send("speak","speech_smoke_002","background-1","切换应用后继续朗读",null);
            waitJs("window.speechSmokeEvents.some(e=>e.utteranceId==='background-1')");
            assertEquals("Paused activity cannot enable a fresh session",2,fake.enables);
            assertEquals("Current playback continues in background",2,fake.speaks);
            assertEquals("Stale events are discarded","false",js("window.speechSmokeEvents.some(e=>e.utteranceId==='stale')"));
            instrumentation.runOnMainSync(()->bridge.cancel("屏幕已锁定，朗读已关闭"));
            waitJs("window.speechSmokeEvents.some(e=>e.sessionId==='speech_smoke_002'&&e.status==='off')");
            fake.listener.state("speech_smoke_002","ready","late-after-stop","");
            send("speak","speech_smoke_002","late-after-stop","过期回复",null);
            instrumentation.runOnMainSync(()->bridge.onResume());
            SystemClock.sleep(150);assertEquals(2,fake.speaks);assertEquals(2,fake.enables);
            assertEquals("Stopped events are discarded","false",js("window.speechSmokeEvents.some(e=>e.utteranceId==='late-after-stop')"));
        } finally {
            js("window.speechSmokeOldDocument=true;true");
            instrumentation.runOnMainSync(()->{
                bridge.close();
                try {
                    SpeechBridge restored=new SpeechBridge(activity,web);
                    field.set(activity,restored);restored.installOrigin(origin);restored.onResume();web.reload();
                } catch(Exception error) { failure.set(error); }
            });
            assertNull(failure.get());
            waitJs("!window.speechSmokeOldDocument && !!window.CoyoteSpeech && !!document.querySelector('[data-coyote-estop]')");
        }
    }
    private void verifyProtocol() throws Exception {
        for(String voice:new String[]{"system-default","melo-zh","kokoro-zf_001","kokoro-zm_010"})
            assertTrue(SpeechBridge.validCommand(new JSONObject().put("type","enable").put("sessionId","valid_session").put("voiceId",voice)));
        JSONObject enable=new JSONObject().put("type","enable").put("sessionId","valid_session");
        assertFalse(SpeechBridge.validCommand(enable));
        assertFalse(SpeechBridge.validCommand(enable.put("voiceId","foreign-url")));
        assertFalse(SpeechBridge.validCommand(enable.put("voiceId",15)));
        JSONObject speak=new JSONObject().put("type","speak").put("sessionId","valid_session").put("utteranceId","id");
        assertFalse(SpeechBridge.validCommand(speak.put("text",15)));
        assertFalse(SpeechBridge.validCommand(speak.put("text"," ")));
        assertTrue(SpeechBridge.validCommand(speak.put("text","words").put("replace",true)));
        assertFalse(SpeechBridge.validCommand(speak.put("replace","true")));
        assertFalse(SpeechBridge.validCommand(speak.put("replace",1)));
        assertTrue(SpeechBridge.validCommand(speak.put("replace",false)));
        assertFalse(SpeechBridge.validCommand(speak.put("text","words").put("extra","field")));
        assertTrue(SpeechBridge.sameOrigin(Uri.parse(origin+"/chat"),origin));
        for(String wrong:new String[]{"https://127.0.0.1:","http://localhost:","http://user@127.0.0.1:"})
            assertFalse(SpeechBridge.sameOrigin(Uri.parse(wrong+Uri.parse(origin).getPort()),origin));
        assertFalse(SpeechBridge.sameOrigin(Uri.parse(origin),"http://foreign.invalid:"+Uri.parse(origin).getPort()));
    }
    private void send(String type,String session,String utterance,String text,String voiceId) throws Exception {
        JSONObject message=new JSONObject().put("type",type).put("sessionId",session);
        if(utterance!=null)message.put("utteranceId",utterance);
        if(text!=null)message.put("text",text);
        if("speak".equals(type))message.put("replace",true);
        if(voiceId!=null)message.put("voiceId",voiceId);
        js("window.CoyoteSpeech.postMessage("+JSONObject.quote(message.toString())+");true");
    }
    private String js(String script) throws Exception {
        AtomicReference<String> result=new AtomicReference<>();CountDownLatch done=new CountDownLatch(1);
        instrumentation.runOnMainSync(()->web.evaluateJavascript(script,value->{result.set(value);done.countDown();}));
        assertTrue("Speech script timeout",done.await(4,TimeUnit.SECONDS));return result.get();
    }
    private void waitJs(String script) throws Exception {
        long until=SystemClock.elapsedRealtime()+12000;
        while(SystemClock.elapsedRealtime()<until) {if("true".equals(js(script)))return;SystemClock.sleep(80);}
        fail("Speech condition timed out: "+script);
    }
}
