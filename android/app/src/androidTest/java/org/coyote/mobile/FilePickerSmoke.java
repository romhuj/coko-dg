package org.coyote.mobile;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.os.Build;
import android.os.SystemClock;
import android.provider.MediaStore;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.webkit.WebView;
import java.lang.reflect.Field;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import junit.framework.Assert;
import org.json.JSONObject;

/** Real WebView file-input -> native picker result -> JS FileReader/canvas, using a synthetic image only. */
final class FilePickerSmoke extends Assert {
    static void verify(Instrumentation instrumentation,Activity activity,WebView web) throws Exception {
        new FilePickerSmoke(instrumentation,activity,web).run();
    }
    private final Instrumentation instrumentation;
    private final Activity activity;
    private final WebView web;
    private FilePickerSmoke(Instrumentation instrumentation,Activity activity,WebView web) {
        this.instrumentation=instrumentation;this.activity=activity;this.web=web;
    }
    private void run() throws Exception {
        assertTrue("Emulator only",Build.MODEL.contains("sdk_gphone") || Build.HARDWARE.contains("ranchu"));
        assertTrue("Current photo-picker integration test requires API 33+",Build.VERSION.SDK_INT>=33);
        AtomicInteger launches=new AtomicInteger();AtomicBoolean cancel=new AtomicBoolean();
        AtomicReference<Intent> launched=new AtomicReference<>();
        AtomicBoolean listening=new AtomicBoolean(true),speaking=new AtomicBoolean(true);
        Field voiceField=MainActivity.class.getDeclaredField("voice"),speechField=MainActivity.class.getDeclaredField("speech");
        voiceField.setAccessible(true);speechField.setAccessible(true);
        Object previousVoice=voiceField.get(activity),previousSpeech=speechField.get(activity);
        AtomicReference<VoiceBridge> fakeVoice=new AtomicReference<>();AtomicReference<SpeechBridge> fakeSpeech=new AtomicReference<>();
        try(PhotoPickerFixture fixture=new PhotoPickerFixture(instrumentation.getTargetContext(),true)) {
            Instrumentation.ActivityMonitor monitor=new Instrumentation.ActivityMonitor() {
                @Override public Instrumentation.ActivityResult onStartActivity(Intent intent) {
                    if(MediaStore.ACTION_PICK_IMAGES.equals(intent.getAction()) || Intent.ACTION_OPEN_DOCUMENT.equals(intent.getAction())) {
                        launched.set(new Intent(intent));launches.incrementAndGet();
                        assertFalse("A microphone must stop before the photo picker opens",listening.get());
                        assertFalse("Reply speech must stop before the photo picker opens",speaking.get());
                        return new Instrumentation.ActivityResult(cancel.get()?Activity.RESULT_CANCELED:Activity.RESULT_OK,
                                cancel.get()?null:new Intent().setData(fixture.uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
                    }
                    return null;
                }
            };
            instrumentation.runOnMainSync(()->{
                fakeVoice.set(new VoiceBridge(activity,web,(context,listener)->new VoiceBridge.Engine() {
                    public void start(String session) {listening.set(true);}
                    public void stop() {listening.set(false);}
                    public void close() {listening.set(false);}
                }));
                fakeSpeech.set(new SpeechBridge(activity,web,(context,listener)->new SpeechBridge.Engine() {
                    public void enable(String session,String voice) {speaking.set(true);}
                    public void speak(String session,String utterance,String text) {speaking.set(true);}
                    public void stop() {speaking.set(false);}
                    public void disable(String message) {speaking.set(false);}
                    public void close() {speaking.set(false);}
                }));
                try {voiceField.set(activity,fakeVoice.get());speechField.set(activity,fakeSpeech.get());}
                catch(IllegalAccessException error) {throw new AssertionError(error);}
                assertFalse("Ordinary content:// navigation must stay disabled",web.getSettings().getAllowContentAccess());
            });
            instrumentation.addMonitor(monitor);
            try {
                js("(()=>{window.pickerSmoke={changes:0};const box=document.createElement('div');box.id='picker-smoke-box';box.style='position:fixed;left:16px;top:150px;z-index:2147483647;background:#222;padding:12px;';box.innerHTML='<input id=picker-smoke-draft value=保留头像草稿><input id=picker-smoke-input type=file accept=image/* style=display:block;width:240px;height:50px>';document.body.appendChild(box);document.querySelector('#picker-smoke-input').onchange=()=>{const f=document.querySelector('#picker-smoke-input').files[0];if(!f)return;pickerSmoke.changes++;pickerSmoke.type=f.type;pickerSmoke.size=f.size;const reader=new FileReader();reader.onerror=()=>pickerSmoke.error='file-reader';reader.onload=()=>{const image=new Image();image.onerror=()=>pickerSmoke.error='image';image.onload=()=>{const c=document.createElement('canvas');c.width=image.width;c.height=image.height;c.getContext('2d').drawImage(image,0,0);pickerSmoke.width=image.width;pickerSmoke.height=image.height;pickerSmoke.pixel=[...c.getContext('2d').getImageData(0,0,1,1).data];pickerSmoke.read=true;};image.src=reader.result;};reader.readAsDataURL(f);};return true;})()");
                tap("#picker-smoke-input");
                waitJs("pickerSmoke.read===true",10000);
                assertEquals(1,launches.get());assertEquals(MediaStore.ACTION_PICK_IMAGES,launched.get().getAction());
                assertEquals("image/*",launched.get().getType());
                assertEquals("true",js("pickerSmoke.width===8 && pickerSmoke.height===6 && JSON.stringify(pickerSmoke.pixel)==='[136,102,34,255]' && pickerSmoke.type==='image/png' && pickerSmoke.changes===1"));
                cancel.set(true);listening.set(true);speaking.set(true);
                tap("#picker-smoke-input");
                long until=SystemClock.elapsedRealtime()+3000;
                while(launches.get()<2 && SystemClock.elapsedRealtime()<until) SystemClock.sleep(25);
                assertEquals(2,launches.get());instrumentation.waitForIdleSync();SystemClock.sleep(100);
                assertEquals("Cancel must preserve the edited draft and selected preview","true",js("document.querySelector('#picker-smoke-draft').value==='保留头像草稿' && pickerSmoke.read===true && pickerSmoke.changes===1"));
                AtomicInteger rejected=new AtomicInteger();
                instrumentation.runOnMainSync(()->{
                    WebView foreign=new WebView(activity);
                    try {web.getWebChromeClient().onShowFileChooser(foreign,uris->{assertNull(uris);rejected.incrementAndGet();},WebFilePickerTest.params("image/*"));}
                    finally {foreign.destroy();}
                });
                assertEquals("A different/untrusted WebView must be rejected",1,rejected.get());assertEquals(2,launches.get());
                assertEquals("true",js("location.hostname==='127.0.0.1' && document.querySelector('[data-coyote-estop]')!==null"));
            } finally {
                instrumentation.removeMonitor(monitor);
                js("document.querySelector('#picker-smoke-box')?.remove();delete window.pickerSmoke;true");
            }
        } finally {
            instrumentation.runOnMainSync(()->{
                try {voiceField.set(activity,previousVoice);speechField.set(activity,previousSpeech);}
                catch(IllegalAccessException error) {throw new AssertionError(error);}
                if(fakeVoice.get()!=null) fakeVoice.get().close();
                if(fakeSpeech.get()!=null) fakeSpeech.get().close();
            });
        }
    }
    private String js(String script) throws Exception {
        CountDownLatch done=new CountDownLatch(1);AtomicReference<String> value=new AtomicReference<>();
        instrumentation.runOnMainSync(()->web.evaluateJavascript(script,result->{value.set(result);done.countDown();}));
        assertTrue(done.await(5,TimeUnit.SECONDS));return value.get();
    }
    private void waitJs(String script,long timeout) throws Exception {
        long until=SystemClock.elapsedRealtime()+timeout;
        do {if("true".equals(js(script)))return;SystemClock.sleep(50);} while(SystemClock.elapsedRealtime()<until);
        fail("Picker did not finish: "+js("JSON.stringify(window.pickerSmoke)"));
    }
    private void tap(String selector) throws Exception {
        JSONObject rect=new JSONObject(js("(()=>{const r=document.querySelector("+JSONObject.quote(selector)+").getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()"));
        double scale=Double.parseDouble(js("devicePixelRatio"));AtomicReference<int[]> origin=new AtomicReference<>();
        instrumentation.runOnMainSync(()->{int[] location=new int[2];web.getLocationOnScreen(location);origin.set(location);});
        float x=(float)(origin.get()[0]+rect.getDouble("x")*scale),y=(float)(origin.get()[1]+rect.getDouble("y")*scale);
        long down=SystemClock.uptimeMillis();
        for(int action:new int[]{MotionEvent.ACTION_DOWN,MotionEvent.ACTION_UP}) {
            MotionEvent event=MotionEvent.obtain(down,SystemClock.uptimeMillis(),action,x,y,0);event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
            try {instrumentation.sendPointerSync(event);} finally {event.recycle();}
            SystemClock.sleep(80);
        }
    }
}
