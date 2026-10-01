package org.coyote.mobile;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.ClipData;
import android.content.ClipDescription;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.PersistableBundle;
import android.os.PowerManager;
import android.webkit.WebView;
import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.Collections;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;
import org.json.JSONObject;

/** Copy a checked pairing payload, optionally launch exactly DG-LAB 4. No arbitrary Intent API. */
final class PairingBridge implements AutoCloseable {
    static final String NAME="CoyotePairing", PACKAGE="com.bjsm.dungeonlabs4";
    private static final int MAX_REQUESTS=512;
    interface Actions {
        void copy(String pairUrl);
        boolean openDgLab();
    }
    private static final class Request {
        final String id,type,url;
        JSONObject result;
        Request(String id,String type,String url) {this.id=id;this.type=type;this.url=url;}
    }
    private final Activity activity;
    private final WebView web;
    private final Actions actions;
    private final Handler main=new Handler(Looper.getMainLooper());
    private final Map<String,Request> requests=new HashMap<>();
    private String origin;
    private long pageGeneration;
    private boolean installed,resumed,ready,closed;

    PairingBridge(Activity activity,WebView web) {
        this(activity,web,new Actions() {
            public void copy(String pairUrl) {
                ClipboardManager clipboard=activity.getSystemService(ClipboardManager.class);
                if(clipboard==null) throw new IllegalStateException("Clipboard unavailable");
                ClipData clip=ClipData.newPlainText("DG-LAB 4 配对链接",pairUrl);
                if(Build.VERSION.SDK_INT>=33) {
                    PersistableBundle extras=new PersistableBundle();
                    extras.putBoolean(ClipDescription.EXTRA_IS_SENSITIVE,true);
                    clip.getDescription().setExtras(extras);
                }
                clipboard.setPrimaryClip(clip);
            }
            public boolean openDgLab() {
                Intent launcher=activity.getPackageManager().getLaunchIntentForPackage(PACKAGE);
                if(launcher==null) return false;
                activity.startActivity(launchIntent(launcher.getComponent()));
                return true;
            }
        });
    }
    PairingBridge(Activity activity,WebView web,Actions actions) {
        this.activity=activity;this.web=web;this.actions=actions;
    }

    static Intent launchIntent(ComponentName component) {
        if(component==null || !PACKAGE.equals(component.getPackageName())) throw new IllegalArgumentException("Unexpected pairing application");
        return Intent.makeMainActivity(component).setPackage(PACKAGE);
    }

    void installOrigin(String next) {
        if(closed || next==null || next.equals(origin) || !SpeechBridge.sameOrigin(Uri.parse(next),next)) return;
        onPageStarted();
        if(installed) WebViewCompat.removeWebMessageListener(web,NAME);
        installed=false;origin=next;
        if(!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return;
        WebViewCompat.addWebMessageListener(web,NAME,Collections.singleton(next),(view,message,source,top,reply)->{
            if(closed || view!=web || !top || !SpeechBridge.sameOrigin(source,origin) || !pageIsLocal()) return;
            if(message.getType()!=WebMessageCompat.TYPE_STRING) return;
            String raw=message.getData();
            if(raw==null || raw.length()>16384) return;
            try {
                JSONObject command=new JSONObject(raw);
                Object id=command.opt("requestId");
                if(!(id instanceof String) || !validId((String)id)) return;
                String requestId=(String)id;
                if(!validCommand(command)) {
                    deliver(reply,result(requestId,"error",false,false,"配对链接或操作无效，请刷新后重试"),pageGeneration);return;
                }
                if(!foregroundReady()) {
                    deliver(reply,result(requestId,"error",false,false,"请回到应用的配对界面后重试"),pageGeneration);return;
                }
                String type=command.getString("type"),url=command.getString("pairUrl");
                Request old=requests.get(requestId);
                if(old!=null) {
                    if(old.type.equals(type) && old.url.equals(url)) deliver(reply,old.result,pageGeneration);
                    else deliver(reply,result(requestId,"error",false,false,"操作标识已使用，请重新点击"),pageGeneration);
                    return;
                }
                if(requests.size()>=MAX_REQUESTS) {
                    deliver(reply,result(requestId,"error",false,false,"请重新打开页面后重试"),pageGeneration);return;
                }
                Request request=new Request(requestId,type,url);requests.put(requestId,request);
                long acceptedPage=pageGeneration;
                // A navigation/pause before this queued operation executes cancels its side effects.
                main.post(()->perform(request,reply,acceptedPage));
            } catch(org.json.JSONException ignored) { }
        });
        installed=true;
    }

    private void perform(Request request,JavaScriptReplyProxy reply,long expectedPage) {
        if(closed || expectedPage!=pageGeneration || requests.get(request.id)!=request) return;
        if(!foregroundReady()) {
            request.result=result(request.id,"error",false,false,"请回到应用的配对界面后重试");
            deliver(reply,request.result,expectedPage);return;
        }
        boolean copied=false;
        try {
            actions.copy(request.url);copied=true;
            if("copy".equals(request.type)) request.result=result(request.id,"copied",true,false,"配对链接已复制");
            else if(actions.openDgLab()) request.result=result(request.id,"opened",true,true,"配对链接已复制，已打开 DG-LAB 4");
            else request.result=result(request.id,"notInstalled",true,false,"配对链接已复制，未找到 DG-LAB 4，请先安装该应用");
        } catch(RuntimeException failure) {
            request.result=result(request.id,"error",copied,false,copied
                    ? "配对链接已复制，暂时无法打开 DG-LAB 4，请手动打开"
                    : "复制失败，请重试或手动复制配对链接");
        }
        // A successful launch may pause this Activity; its result still belongs to the same page.
        deliver(reply,request.result,expectedPage);
    }

    static boolean validCommand(JSONObject command) {
        Object id=command.opt("requestId"),type=command.opt("type"),url=command.opt("pairUrl");
        if(!(id instanceof String) || !validId((String)id) || !(type instanceof String)
                || !("copy".equals(type) || "copyAndOpen".equals(type)) || !(url instanceof String)) return false;
        Iterator<String> keys=command.keys();
        while(keys.hasNext()) {
            String key=keys.next();
            if(!"requestId".equals(key) && !"type".equals(key) && !"pairUrl".equals(key)) return false;
        }
        return validPairUrl((String)url);
    }
    private static boolean validId(String id) {return id.matches("[A-Za-z0-9_-]{8,96}");}

    /** Mirrors backend.pairing.build_pair_url; the copied URL is never dispatched as an Intent. */
    static boolean validPairUrl(String value) {
        if(value==null || value.length()>8192) return false;
        try {
            URI parsed=new URI(value);
            if(!"https".equals(parsed.getScheme()) || !"dungeon-lab.cn".equals(parsed.getHost())
                    || parsed.getRawUserInfo()!=null || parsed.getPort()!=-1 || parsed.getRawFragment()!=null
                    || !"/s/".equals(parsed.getRawPath())) return false;
            Uri outer=Uri.parse(value);
            if(outer.getQueryParameterNames().size()!=3 || !once(outer,"v","1") || !once(outer,"action","socket")
                    || outer.getQueryParameters("url").size()!=1) return false;
            String socket=outer.getQueryParameter("url");
            URI ws=new URI(socket);
            if(!("ws".equals(ws.getScheme()) || "wss".equals(ws.getScheme())) || ws.getHost()==null
                    || ws.getRawUserInfo()!=null || ws.getRawFragment()!=null || ws.getPort() < -1 || ws.getPort()>65535 || ws.getPort()==0) return false;
            Uri inner=Uri.parse(socket);
            if(inner.getQueryParameters("tid").size()!=1 || inner.getQueryParameterNames().contains("targetId")) return false;
            String id=inner.getQueryParameter("tid");
            return id!=null && id.matches("[A-Za-z0-9_-]{1,160}");
        } catch(URISyntaxException | IllegalArgumentException | NullPointerException invalid) {return false;}
    }
    private static boolean once(Uri uri,String key,String expected) {
        return uri.getQueryParameters(key).size()==1 && expected.equals(uri.getQueryParameter(key));
    }
    private boolean pageIsLocal() {return web.getUrl()!=null && SpeechBridge.sameOrigin(Uri.parse(web.getUrl()),origin);}
    private boolean foregroundReady() {
        PowerManager power=activity.getSystemService(PowerManager.class);
        KeyguardManager keyguard=activity.getSystemService(KeyguardManager.class);
        return !closed && ready && resumed && !activity.isFinishing() && !activity.isDestroyed() && pageIsLocal()
                && power!=null && power.isInteractive() && (keyguard==null || !keyguard.isKeyguardLocked());
    }
    void onPageStarted() {ready=false;pageGeneration++;requests.clear();main.removeCallbacksAndMessages(null);}
    void onPageReady() {if(!closed && pageIsLocal()) ready=true;}
    void onResume() {resumed=true;}
    void onPause() {resumed=false;}
    private void deliver(JavaScriptReplyProxy reply,JSONObject value,long expectedPage) {
        if(closed || value==null || expectedPage!=pageGeneration || !pageIsLocal()) return;
        try {reply.postMessage(value.toString());} catch(RuntimeException ignored) { }
    }
    private static JSONObject result(String id,String status,boolean copied,boolean opened,String message) {
        JSONObject event=new JSONObject();
        try {
            event.put("type","result").put("requestId",id).put("status",status)
                    .put("copied",copied).put("opened",opened).put("message",message);
        } catch(org.json.JSONException impossible) {throw new IllegalStateException(impossible);}
        return event;
    }
    @Override public void close() {
        if(closed) return;
        onPageStarted();closed=true;resumed=false;
        if(installed) WebViewCompat.removeWebMessageListener(web,NAME);
        installed=false;
    }
}
