package org.coyote.mobile;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.KeyguardManager;
import android.net.Uri;
import android.os.PowerManager;
import android.os.SystemClock;
import android.view.ViewGroup;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import java.io.ByteArrayInputStream;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import junit.framework.Assert;
import org.json.JSONObject;

/** A fresh WebView with fake platform actions; never changes clipboard or opens another application. */
final class PairingBridgeSmoke extends Assert {
    static final class FakeActions implements PairingBridge.Actions {
        volatile int copies,opens;
        volatile boolean installed=true,copyFails,openFails;
        volatile String copied;
        public void copy(String url) {if(copyFails)throw new IllegalStateException();copies++;copied=url;}
        public boolean openDgLab() {opens++;if(openFails)throw new IllegalStateException();return installed;}
    }
    private final Instrumentation instrumentation;
    private final Activity activity;
    private final String origin;
    private final FakeActions fake=new FakeActions();
    private WebView web;
    private PairingBridge bridge;
    private final CountDownLatch firstPageFinished=new CountDownLatch(1);
    private volatile String finishedUrl;
    private static final String HTML="<!doctype html><html><body>Pairing bridge test</body></html>";
    private static final String FIXTURE_PATH="/__pairing_bridge_smoke__.html";
    private PairingBridgeSmoke(Instrumentation instrumentation,Activity activity,String origin) {
        this.instrumentation=instrumentation;this.activity=activity;this.origin=origin;
    }
    static void verify(Instrumentation instrumentation,Activity activity,String origin) throws Exception {
        new PairingBridgeSmoke(instrumentation,activity,origin).run();
    }
    private void run() throws Exception {
        assertTrue("Emulator only",android.os.Build.MODEL.contains("sdk_gphone") || android.os.Build.HARDWARE.contains("ranchu"));
        instrumentation.runOnMainSync(()->{
            web=new WebView(activity);web.getSettings().setJavaScriptEnabled(true);
            web.getSettings().setAllowFileAccess(false);web.getSettings().setAllowContentAccess(false);
            activity.addContentView(web,new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,ViewGroup.LayoutParams.MATCH_PARENT));
            bridge=new PairingBridge(activity,web,fake);
            web.setWebViewClient(new WebViewClient() {
                @Override public void onPageStarted(WebView view,String url,android.graphics.Bitmap icon) {bridge.onPageStarted();}
                @Override public WebResourceResponse shouldInterceptRequest(WebView view,WebResourceRequest request) {
                    String url=request.getUrl().toString();
                    if(url.equals(origin+FIXTURE_PATH) || url.equals("https://untrusted.invalid"+FIXTURE_PATH))
                        return new WebResourceResponse("text/html","UTF-8",new ByteArrayInputStream(HTML.getBytes(StandardCharsets.UTF_8)));
                    return super.shouldInterceptRequest(view,request);
                }
                @Override public void onPageFinished(WebView view,String url) {
                    bridge.onPageReady();finishedUrl=url;
                    if(url.equals(origin+FIXTURE_PATH))firstPageFinished.countDown();
                }
            });
            // Use a real local document URL, like production. loadDataWithBaseURL with
            // null historyUrl can leave native getUrl() at about:blank while JS has a local origin.
            bridge.installOrigin(origin);bridge.onResume();web.loadUrl(origin+FIXTURE_PATH);
        });
        try {
            boolean pageFinished=firstPageFinished.await(10,TimeUnit.SECONDS);
            assertTrue("Pairing fixture did not finish its local navigation"+(pageFinished?"":": "+diagnostics()),pageFinished);
            waitJs("!!window.CoyotePairing && document.readyState==='complete'");
            js("window.pairEvents=[];window.CoyotePairing.onmessage=e=>pairEvents.push(JSON.parse(e.data));true");
            send("copy","pair_copy_001");waitStatus("pair_copy_001","copied");
            assertEquals(1,fake.copies);assertEquals(0,fake.opens);assertEquals(PairingProtocolTest.PUBLIC,fake.copied);
            send("copyAndOpen","pair_open_001");waitStatus("pair_open_001","opened");
            assertEquals(2,fake.copies);assertEquals(1,fake.opens);
            send("copyAndOpen","pair_open_001");
            waitJs("pairEvents.filter(e=>e.requestId==='pair_open_001').length===2");
            assertEquals("Duplicate request must not repeat launch",1,fake.opens);
            assertEquals(2,fake.copies);
            fake.installed=false;
            send("copyAndOpen","pair_missing_001");waitStatus("pair_missing_001","notInstalled");
            assertEquals("true",js("pairEvents.find(e=>e.requestId==='pair_missing_001').copied===true && pairEvents.find(e=>e.requestId==='pair_missing_001').opened===false"));
            fake.openFails=true;
            send("copyAndOpen","pair_failure_001");waitStatus("pair_failure_001","error");
            assertEquals("true",js("pairEvents.find(e=>e.requestId==='pair_failure_001').copied===true"));
            int before=fake.copies,launches=fake.opens;
            fake.copyFails=true;
            send("copyAndOpen","pair_clipboard_001");waitStatus("pair_clipboard_001","error");
            assertEquals("false",js("pairEvents.find(e=>e.requestId==='pair_clipboard_001').copied"));
            assertEquals(launches,fake.opens);fake.copyFails=false;fake.openFails=false;
            js("CoyotePairing.postMessage(JSON.stringify({type:'copyAndOpen',requestId:'pair_invalid_001',pairUrl:'intent://evil',package:'evil'}));true");
            waitStatus("pair_invalid_001","error");assertEquals(before,fake.copies);
            js("(()=>{let frame=document.createElement('iframe');frame.id='pairFrame';frame.srcdoc='<p>frame</p>';document.body.appendChild(frame);return true})()");
            waitJs("!!document.getElementById('pairFrame').contentWindow.CoyotePairing");
            JSONObject sub=new JSONObject().put("type","copy").put("requestId","pair_frame_001").put("pairUrl",PairingProtocolTest.PUBLIC);
            js("document.getElementById('pairFrame').contentWindow.CoyotePairing.postMessage("+JSONObject.quote(sub.toString())+");true");
            SystemClock.sleep(150);assertEquals("A subframe cannot copy or launch",before,fake.copies);
            instrumentation.runOnMainSync(()->bridge.onPause());
            send("copy","pair_background_001");waitStatus("pair_background_001","error");assertEquals(before,fake.copies);
            instrumentation.runOnMainSync(()->{bridge.onResume();bridge.onPageStarted();});
            send("copy","pair_oldpage_001");waitStatus("pair_oldpage_001","error");assertEquals(before,fake.copies);
            instrumentation.runOnMainSync(()->bridge.onPageReady());
            send("copy","pair_return_001");waitStatus("pair_return_001","copied");assertEquals(before+1,fake.copies);
            instrumentation.runOnMainSync(()->web.loadUrl("https://untrusted.invalid"+FIXTURE_PATH));
            waitJs("location.origin==='https://untrusted.invalid'");
            assertEquals("Foreign origins must have no native pairing bridge","undefined",new org.json.JSONArray("["+js("typeof window.CoyotePairing")+"]").getString(0));
        } finally {
            instrumentation.runOnMainSync(()->{
                bridge.close();((ViewGroup)web.getParent()).removeView(web);web.destroy();
            });
        }
    }
    private void send(String type,String id) throws Exception {
        JSONObject command=new JSONObject().put("type",type).put("requestId",id).put("pairUrl",PairingProtocolTest.PUBLIC);
        js("CoyotePairing.postMessage("+JSONObject.quote(command.toString())+");true");
    }
    private void waitStatus(String id,String status) throws Exception {waitJs("pairEvents.some(e=>e.requestId==="+JSONObject.quote(id)+"&&e.status==="+JSONObject.quote(status)+")");}
    private String js(String script) throws Exception {
        AtomicReference<String> result=new AtomicReference<>();CountDownLatch done=new CountDownLatch(1);
        instrumentation.runOnMainSync(()->web.evaluateJavascript(script,value->{result.set(value);done.countDown();}));
        assertTrue("Pairing test script timeout",done.await(4,TimeUnit.SECONDS));return result.get();
    }
    private void waitJs(String expression) throws Exception {
        long until=SystemClock.elapsedRealtime()+10000;
        while(SystemClock.elapsedRealtime()<until) {if("true".equals(js(expression)))return;SystemClock.sleep(60);}
        fail("Pairing bridge condition failed: "+expression+"; "+diagnostics());
    }
    private String diagnostics() throws Exception {
        AtomicReference<String> nativeState=new AtomicReference<>();
        instrumentation.runOnMainSync(()->{
            JSONObject state=new JSONObject();
            try {
                String url=web.getUrl();
                PowerManager power=activity.getSystemService(PowerManager.class);
                KeyguardManager keyguard=activity.getSystemService(KeyguardManager.class);
                state.put("nativeUrl",url).put("finishedUrl",finishedUrl)
                    .put("sameOrigin",url!=null&&SpeechBridge.sameOrigin(Uri.parse(url),origin))
                    .put("interactive",power!=null&&power.isInteractive())
                    .put("locked",keyguard!=null&&keyguard.isKeyguardLocked())
                    .put("activityFinishing",activity.isFinishing()).put("activityDestroyed",activity.isDestroyed())
                    .put("copies",fake.copies).put("opens",fake.opens);
                for(String name:new String[]{"installed","resumed","ready","closed"}) {
                    Field field=PairingBridge.class.getDeclaredField(name);field.setAccessible(true);
                    state.put(name,field.getBoolean(bridge));
                }
            } catch(Exception error) {
                try {state.put("diagnosticError",error.getClass().getSimpleName());}catch(org.json.JSONException ignored) { }
            }
            nativeState.set(state.toString());
        });
        return "native="+nativeState.get()+"; page="+js("JSON.stringify({origin:location.origin,readyState:document.readyState,bridge:typeof window.CoyotePairing,events:window.pairEvents||[]})");
    }
}
