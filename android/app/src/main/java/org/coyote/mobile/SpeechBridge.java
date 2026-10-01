package org.coyote.mobile;

import android.app.Activity;
import android.content.Context;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.webkit.WebView;
import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import org.json.JSONObject;
import java.util.Collections;
import java.util.HashSet;
import java.util.Iterator;
import java.util.Set;

/** A text-only reply playback channel restricted to this local page and its main frame. */
final class SpeechBridge implements AutoCloseable {
    static final String NAME = "CoyoteSpeech";
    interface Engine extends AutoCloseable {
        void enable(String session, String voiceId);
        void speak(String session, String utterance, String text);
        default void speak(String session,String utterance,String text,boolean replace) {speak(session,utterance,text);}
        void stop();
        void disable(String message);
        void close();
    }
    interface EngineFactory { Engine create(Context context, ReplySpeech.Listener listener); }
    private final Activity activity;
    private final WebView web;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Engine engine;
    private final Set<String> usedSessions = new HashSet<>();
    private String origin, sessionId, voiceId;
    private JavaScriptReplyProxy reply;
    private volatile long sessionGeneration;
    private long pageGeneration, speechPage;
    private boolean installed, resumed, closed;
    private JSONObject lastState;

    SpeechBridge(Activity activity, WebView web) {
        this(activity, web, (context, listener) -> {
            ReplySpeech speech = new ReplySpeech(context, listener);
            return new Engine() {
                public void enable(String session, String voiceId) { speech.enable(session, voiceId); }
                public void speak(String session, String utterance, String text) { speech.speak(session, utterance, text); }
                public void speak(String session,String utterance,String text,boolean replace) {speech.speak(session,utterance,text,replace);}
                public void stop() { speech.stop(); }
                public void disable(String message) { speech.disable(message); }
                public void close() { speech.close(); }
            };
        });
    }

    SpeechBridge(Activity activity, WebView web, EngineFactory factory) {
        this.activity = activity;
        this.web = web;
        engine = factory.create(activity.getApplicationContext(), (session, status, utterance, message) -> {
            final long generation = sessionGeneration;
            onMain(() -> {
                if (generation != sessionGeneration || !matches(session)) return;
                if ("interrupted".equals(status)) {
                    JSONObject event=new JSONObject();
                    put(event,"type","interrupted");put(event,"sessionId",session);put(event,"utteranceId",utterance);
                    lastState=state(session,"ready","");
                    deliver(event);return;
                }
                if (!"off".equals(status) && !"preparing".equals(status) && !"ready".equals(status)
                        && !"speaking".equals(status) && !"error".equals(status)) return;
                JSONObject event = state(session, status, message);
                if (utterance != null) put(event, "utteranceId", utterance);
                lastState = event;
                deliver(event);
                if ("off".equals(status) || "error".equals(status)) {
                    sessionGeneration++;
                    sessionId = null;
                }
            });
        });
    }

    void installOrigin(String next) {
        if (closed || next == null || next.equals(origin)) return;
        Uri expected = Uri.parse(next);
        if (!"http".equalsIgnoreCase(expected.getScheme()) || !"127.0.0.1".equals(expected.getHost())
                || expected.getUserInfo() != null || expected.getPort() < 1 || expected.getPort() > 65535) return;
        cancel("页面已切换，朗读已关闭");
        if (installed) WebViewCompat.removeWebMessageListener(web, NAME);
        installed = false;
        origin = next;
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return;
        WebViewCompat.addWebMessageListener(web, NAME, Collections.singleton(next),
            (view, message, source, topFrame, proxy) -> {
                if (closed || view != web || !topFrame || !sameOrigin(source, origin) || !pageIsLocal()) return;
                if (message.getType() != WebMessageCompat.TYPE_STRING) return;
                String raw = message.getData();
                if (raw == null || raw.length() > 100000) return;
                try {
                    JSONObject command = new JSONObject(raw);
                    if (!validCommand(command)) return;
                    String type = command.getString("type"), session = command.getString("sessionId");
                    if ("enable".equals(type)) {
                        if (!resumed || !screenInteractive()) return;
                        String requestedVoice = command.getString("voiceId");
                        if (session.equals(sessionId)) {
                            if (!requestedVoice.equals(voiceId)) return;
                            reply = proxy; deliver(lastState); return;
                        }
                        // A canceled page/session identifier cannot be reused to admit a delayed callback.
                        if (usedSessions.contains(session)) return;
                        cancel("");
                        reply = proxy;
                        sessionId = session;
                        voiceId = requestedVoice;
                        usedSessions.add(session);
                        speechPage = pageGeneration;
                        lastState = state(session, "preparing", "正在准备回复朗读");
                        deliver(lastState);
                        engine.enable(session, requestedVoice);
                    } else if (session.equals(sessionId)) {
                        reply = proxy;
                        if ("disable".equals(type)) cancel("");
                        else if ("stop".equals(type)) engine.stop();
                        else if ("speak".equals(type)) {
                            if (!matches(session)) return;
                            if (!screenInteractive()) { cancel("屏幕已锁定，朗读已关闭"); return; }
                            engine.speak(session, command.getString("utteranceId"), command.getString("text").trim(),command.optBoolean("replace",false));
                        }
                    }
                } catch (org.json.JSONException ignored) {
                    // Invalid local data has no effect on playback or device APIs.
                } catch (RuntimeException failure) {
                    String old = sessionId;
                    sessionGeneration++;
                    sessionId = null;
                    engine.disable("");
                    if (old != null) deliver(state(old, "error", "朗读启动失败，请重试"));
                }
            });
        installed = true;
    }

    static boolean validCommand(JSONObject command) {
        Object type = command.opt("type"), session = command.opt("sessionId");
        if (!(type instanceof String) || !(session instanceof String)
                || !((String) session).matches("[A-Za-z0-9_-]{8,96}")) return false;
        boolean speak = "speak".equals(type);
        boolean enable = "enable".equals(type);
        if (!speak && !"enable".equals(type) && !"disable".equals(type) && !"stop".equals(type)) return false;
        Iterator<String> keys = command.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            if (!"type".equals(key) && !"sessionId".equals(key) && !(enable && "voiceId".equals(key))
                    && !(speak && ("utteranceId".equals(key) || "text".equals(key) || "replace".equals(key)))) return false;
        }
        if (enable) return command.opt("voiceId") instanceof String && ReplySpeech.validVoice(command.optString("voiceId"));
        if (!speak) return true;
        if(command.has("replace")&&!(command.opt("replace") instanceof Boolean))return false;
        Object utterance = command.opt("utteranceId"), text = command.opt("text");
        return utterance instanceof String && ((String) utterance).matches("[A-Za-z0-9_-]{1,160}")
            && text instanceof String && !((String) text).trim().isEmpty() && ((String) text).length() <= 16000;
    }

    static boolean sameOrigin(Uri source, String origin) {
        if (source == null || origin == null || source.getUserInfo() != null) return false;
        Uri expected = Uri.parse(origin);
        return "http".equalsIgnoreCase(source.getScheme()) && "127.0.0.1".equals(source.getHost())
            && "http".equalsIgnoreCase(expected.getScheme()) && "127.0.0.1".equals(expected.getHost())
            && expected.getUserInfo() == null && expected.getPort() > 0 && expected.getPort() <= 65535 && expected.getPort() == source.getPort();
    }
    void onPageStarted() { cancel(""); pageGeneration++; reply = null; }
    void onResume() { resumed = true; }
    void onPause() { resumed = false; /* Already-enabled playback continues during bright-screen app switches. */ }
    void cancel(String message) {
        String old = sessionId;
        sessionGeneration++;
        sessionId = null;
        engine.disable(message);
        if (old != null) {
            lastState = state(old, "off", message);
            deliver(lastState);
        }
    }
    private boolean matches(String session) {
        return !closed && sessionId != null && sessionId.equals(session) && speechPage == pageGeneration && pageIsLocal();
    }
    private boolean pageIsLocal() { return web.getUrl() != null && sameOrigin(Uri.parse(web.getUrl()), origin); }
    private boolean screenInteractive() {
        PowerManager power = activity.getSystemService(PowerManager.class);
        return power != null && power.isInteractive();
    }
    private void onMain(Runnable work) { if (Looper.myLooper() == Looper.getMainLooper()) work.run(); else main.post(work); }
    private void deliver(JSONObject event) {
        if (closed || reply == null || event == null || !pageIsLocal()) return;
        try { reply.postMessage(event.toString()); } catch (RuntimeException ignored) { }
    }
    private static JSONObject state(String session, String status, String message) {
        JSONObject value = new JSONObject();
        put(value, "type", "state"); put(value, "sessionId", session); put(value, "status", status); put(value, "message", message == null ? "" : message);
        return value;
    }
    private static void put(JSONObject value, String key, Object data) {
        try { value.put(key, data); } catch (org.json.JSONException impossible) { throw new IllegalStateException(impossible); }
    }
    @Override public void close() {
        if (closed) return;
        cancel(""); closed = true;
        if (installed) WebViewCompat.removeWebMessageListener(web, NAME);
        installed = false; engine.close(); main.removeCallbacksAndMessages(null); reply = null;
    }
}
