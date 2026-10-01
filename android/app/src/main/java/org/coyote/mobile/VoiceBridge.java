package org.coyote.mobile;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.webkit.WebView;
import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import androidx.webkit.WebMessageCompat;
import org.json.JSONObject;
import java.util.Collections;

/** A voice-only channel restricted to the current local origin and top frame. */
final class VoiceBridge implements AutoCloseable {
    static final int MICROPHONE_PERMISSION = 73;
    static final String NAME = "CoyoteVoice";
    interface Engine extends AutoCloseable {
        void start(String sessionId);
        void stop();
        void close();
    }
    interface EngineFactory {
        Engine create(Context context, ContinuousVoiceRecognizer.Listener listener);
    }
    private final Activity activity;
    private final WebView web;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Engine engine;
    private String origin, sessionId, grantedSession;
    private JavaScriptReplyProxy reply;
    private long pageGeneration, capturePage;
    private boolean installed, resumed, closed, permissionPending;
    private JSONObject lastState;

    VoiceBridge(Activity activity, WebView web) {
        this(activity, web, (context, listener) -> {
            VoiceCaptureService.Client recognizer = new VoiceCaptureService.Client(context, listener);
            return new Engine() {
                public void start(String session) { recognizer.start(session); }
                public void stop() { recognizer.stop(); }
                public void close() { recognizer.close(); }
            };
        });
    }

    VoiceBridge(Activity activity, WebView web, EngineFactory factory) {
        this.activity = activity;
        this.web = web;
        engine = factory.create(activity.getApplicationContext(), new ContinuousVoiceRecognizer.Listener() {
            public void onState(String session, String status, int progress, String message) {
                onMain(() -> {
                    if (!matches(session)) return;
                    JSONObject state = event("state", session);
                    put(state, "status", status);
                    put(state, "progress", Math.max(0, Math.min(100, progress)));
                    put(state, "message", message == null ? "" : message);
                    put(state, "duplex", VoiceCaptureService.isFullDuplex(session));
                    if("listening".equals(status) && VoiceCaptureService.isPlaybackSuspended()) put(state,"reason","playback");
                    lastState = state;
                    deliver(state);
                    if ("error".equals(status) || "off".equals(status)) {
                        sessionId = null;
                        grantedSession = null;
                        if ("error".equals(status)) engine.stop();
                    }
                });
            }
            public void onSentence(String session, String utterance, String text) {
                onMain(() -> {
                    if (!matches(session) || text == null || text.trim().isEmpty()
                            || text.length() > 8000 || utterance == null || utterance.length() > 160) return;
                    JSONObject event = event("sentence", session);
                    put(event, "utteranceId", utterance);
                    put(event, "text", text.trim());
                    deliver(event);
                });
            }
            public void onLevel(String session,float level) {
                onMain(()->{
                    if(!matches(session) || !Float.isFinite(level)) return;
                    JSONObject event=event("level",session);
                    put(event,"level",Math.max(0,Math.min(1,level)));
                    deliver(event);
                });
            }
            public void onPreview(String session,String text) {
                onMain(()->{
                    if(!matches(session) || text==null || text.length()>8000) return;
                    JSONObject event=event("preview",session);
                    put(event,"text",text);
                    deliver(event);
                });
            }
        });
    }

    void installOrigin(String next) {
        if (closed || next.equals(origin)) return;
        cancel("页面已切换，麦克风已关闭");
        if (installed) WebViewCompat.removeWebMessageListener(web, NAME);
        installed = false;
        origin = next;
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return;
        WebViewCompat.addWebMessageListener(web, NAME, Collections.singleton(next),
            (view, message, source, topFrame, proxy) -> {
                if (closed || view != web || !topFrame || !sameOrigin(source) || !pageIsLocal()) return;
                if (message.getType() != WebMessageCompat.TYPE_STRING) return;
                String data = message.getData();
                if (data == null || data.length() > 512) return;
                try {
                    JSONObject command = new JSONObject(data);
                    String type = command.optString("type");
                    String session = command.optString("sessionId");
                    if (!session.matches("[A-Za-z0-9_-]{8,96}")) return;
                    if ("start".equals(type)) {
                        if (!resumed) return;
                        if (session.equals(sessionId)) { reply = proxy; deliver(lastState); return; }
                        cancel("");
                        reply = proxy;
                        sessionId = session;
                        capturePage = pageGeneration;
                        if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                            permissionPending = true;
                            lastState = state(session, "preparing", "请允许麦克风权限");
                            put(lastState, "reason", "permission");
                            deliver(lastState);
                            activity.requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MICROPHONE_PERMISSION);
                        } else startEngine(session);
                    } else if ("stop".equals(type) && session.equals(sessionId)) {
                        reply = proxy;
                        cancel("");
                    } else if ("status".equals(type)) {
                        if (session.equals(sessionId)) { reply = proxy; deliver(lastState); }
                        else proxy.postMessage(state(session, "off", "").toString());
                    }
                } catch (RuntimeException | org.json.JSONException ignored) {
                    // Malformed local messages never reach microphone or device APIs.
                }
            });
        installed = true;
    }

    void onPageStarted() { cancel(""); pageGeneration++; reply = null; }
    void onResume() {
        resumed = true;
        if (grantedSession != null && matches(grantedSession)) {
            String session = grantedSession;
            grantedSession = null;
            startEngine(session);
        }
    }
    void onPause() {
        resumed = false;
        // A user-started microphone foreground service continues across bright-screen app switches.
        // New starts still require onResume; screen-off and notification stop invalidate the service session.
    }
    void onPermissionResult(int[] grants) {
        if (!permissionPending) return;
        permissionPending = false;
        if (sessionId == null || !pageIsLocal()) return;
        if (grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED) {
            if (resumed) startEngine(sessionId); else grantedSession = sessionId;
        } else {
            lastState = state(sessionId, "error", "未获得麦克风权限，请允许权限后重试");
            deliver(lastState);
            sessionId = null;
        }
    }
    void cancel(String message) {
        String old = sessionId;
        sessionId = null;
        grantedSession = null;
        // A pending permission dialog may still finish; its callback cannot restart.
        engine.stop();
        if (old != null) {
            lastState = state(old, "off", message);
            deliver(lastState);
        }
    }
    private void startEngine(String session) {
        if (!resumed || !matches(session)) return;
        lastState = state(session, "preparing", "正在准备离线语音识别");
        deliver(lastState);
        try { engine.start(session); }
        catch (RuntimeException failure) {
            lastState = state(session, "error", "麦克风启动失败，请重试");
            deliver(lastState);
            sessionId = null;
            engine.stop();
        }
    }
    private boolean matches(String session) {
        return !closed && sessionId != null && sessionId.equals(session)
            && capturePage == pageGeneration && pageIsLocal();
    }
    private void onMain(Runnable task) {
        if (Looper.myLooper() == Looper.getMainLooper()) task.run(); else main.post(task);
    }
    private boolean pageIsLocal() { return web.getUrl() != null && sameOrigin(Uri.parse(web.getUrl())); }
    private boolean sameOrigin(Uri uri) {
        if (origin == null || uri == null || uri.getUserInfo() != null) return false;
        Uri expected = Uri.parse(origin);
        return "http".equalsIgnoreCase(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
            && expected.getPort() == uri.getPort();
    }
    private void deliver(JSONObject event) {
        if (closed || reply == null || event == null || !pageIsLocal()) return;
        try { reply.postMessage(event.toString()); } catch (RuntimeException ignored) { }
    }
    private static JSONObject event(String type, String session) {
        JSONObject value = new JSONObject();
        put(value, "type", type); put(value, "sessionId", session);
        return value;
    }
    private static JSONObject state(String session, String status, String message) {
        JSONObject value = event("state", session);
        put(value, "status", status); put(value, "message", message);
        put(value, "duplex", VoiceCaptureService.isFullDuplex(session));
        return value;
    }
    private static void put(JSONObject object, String key, Object value) {
        try { object.put(key, value); } catch (org.json.JSONException impossible) { throw new IllegalStateException(impossible); }
    }
    @Override public void close() {
        if (closed) return;
        cancel("");
        closed = true;
        if (installed) WebViewCompat.removeWebMessageListener(web, NAME);
        installed = false;
        engine.close();
        main.removeCallbacksAndMessages(null);
        reply = null;
    }
}
