package org.coyote.mobile;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.*;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;

/** Local-origin UI with a restricted, main-frame-only speech message channel. */
public final class MainActivity extends Activity implements RuntimeService.Listener {
    private static final int OPEN_FILE=70, SAVE_QR=71, NOTIFICATIONS=72;
    private WebView web;
    private VoiceBridge voice;
    private SpeechBridge speech;
    private PairingBridge pairing;
    private TextView message;
    private LinearLayout statusPanel;
    private ImageButton fallbackEstop;
    private AlertDialog exitDialog;
    private final Handler main = new Handler(Looper.getMainLooper());
    private RuntimeService service;
    private boolean bound, systemPicker, closing, destroyed, rendererGone, pageReady, backPending;
    private long cookieGeneration, pageGeneration, backGeneration;
    private volatile String origin;
    private boolean pageFailed, voiceCachePreverified;
    private ValueCallback<Uri[]> fileCallback;
    private String pendingDownload;
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service=((RuntimeService.LocalBinder)binder).getService(); service.addListener(MainActivity.this);
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            service=null; showStatus("本地服务已断开。请重新启动。", true);
        }
    };

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        getWindow().setStatusBarColor(Color.rgb(12,12,13));
        getWindow().setNavigationBarColor(Color.rgb(12,12,13));
        getWindow().getDecorView().setSystemUiVisibility(0);
        if (Build.VERSION.SDK_INT >= 29) getWindow().setStatusBarContrastEnforced(true);
        makeUi();
        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(0, this::handleBack);
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATIONS);
        }
        startRuntime();
    }
    private int dp(int n) { return Math.round(n * getResources().getDisplayMetrics().density); }
    private void makeUi() {
        LinearLayout root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(Color.rgb(12,12,13));
        if (Build.VERSION.SDK_INT >= 30) root.setOnApplyWindowInsetsListener((view, insets) -> {
            android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
            int keyboard = insets.getInsets(WindowInsets.Type.ime()).bottom;
            view.setPadding(bars.left,bars.top,bars.right,Math.max(bars.bottom,keyboard)); return insets;
        });
        FrameLayout body = new FrameLayout(this); root.addView(body,new LinearLayout.LayoutParams(-1,0,1));
        web = new WebView(this); web.setBackgroundColor(Color.rgb(12,12,13)); body.addView(web,new FrameLayout.LayoutParams(-1,-1));
        statusPanel = new LinearLayout(this); statusPanel.setOrientation(LinearLayout.VERTICAL); statusPanel.setGravity(Gravity.CENTER); statusPanel.setPadding(dp(28),dp(28),dp(28),dp(28));
        statusPanel.setBackgroundColor(Color.rgb(12,12,13));
        message = new TextView(this); message.setTextSize(17); message.setTextColor(Color.rgb(252,252,252)); message.setGravity(Gravity.CENTER); statusPanel.addView(message);
        Button retry=button("启动本地服务",this::startRuntime); retry.setTag("retry"); statusPanel.addView(retry);
        body.addView(statusPanel,new FrameLayout.LayoutParams(-1,-1));
        fallbackEstop=new ImageButton(this); fallbackEstop.setImageResource(R.drawable.ic_estop);
        fallbackEstop.setBackgroundResource(R.drawable.estop_background); fallbackEstop.setContentDescription("紧急停止并清零");
        fallbackEstop.setPadding(dp(12),dp(12),dp(12),dp(12)); fallbackEstop.setTag("native-estop");
        fallbackEstop.setOnClickListener(v -> { if(voice!=null) voice.cancel(""); if(speech!=null) speech.cancel(""); if(service!=null) service.requestEstop(); else signalStop(); });
        FrameLayout.LayoutParams stopPosition=new FrameLayout.LayoutParams(dp(48),dp(48),Gravity.TOP|Gravity.END);
        stopPosition.topMargin=dp(6); stopPosition.rightMargin=dp(12); body.addView(fallbackEstop,stopPosition);
        setContentView(root); configureWebView();
        showStatus("正在启动本地服务…",false);
    }
    private Button button(String text, Runnable action) {
        Button b=new Button(this); b.setText(text); b.setTextSize(12); b.setMinWidth(0); b.setMinimumWidth(0); b.setPadding(dp(8),0,dp(8),0);
        b.setOnClickListener(v -> action.run()); return b;
    }
    @SuppressWarnings("SetJavaScriptEnabled")
    private void configureWebView() {
        voice = new VoiceBridge(this, web);
        speech = new SpeechBridge(this, web);
        pairing = new PairingBridge(this, web);
        WebSettings settings=web.getSettings(); settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false); settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false); settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setJavaScriptCanOpenWindowsAutomatically(false); settings.setSupportMultipleWindows(true);
        settings.setMediaPlaybackRequiresUserGesture(true); settings.setGeolocationEnabled(false);
        if (Build.VERSION.SDK_INT>=26) settings.setSafeBrowsingEnabled(true);
        CookieManager.getInstance().setAcceptCookie(true); CookieManager.getInstance().setAcceptThirdPartyCookies(web,false);
        WebView.setWebContentsDebuggingEnabled(false);
        web.setWebViewClient(new WebViewClient() {
            @Override public void onPageStarted(WebView view,String url,android.graphics.Bitmap icon) {
                if(voice!=null) voice.onPageStarted();
                if(speech!=null) speech.onPageStarted();
                if(pairing!=null) pairing.onPageStarted();
                pageReady=false; pageGeneration++;
                if(isLocal(Uri.parse(url))) showStatus("正在加载…",false);
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (isLocal(request.getUrl())) return false;
                if (request.isForMainFrame() && request.hasGesture()) openExternal(request.getUrl());
                return true;
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                if (isLocal(request.getUrl())) return null;
                return new WebResourceResponse("text/plain","UTF-8",403,"Blocked",null,
                    new ByteArrayInputStream("Only the local app is allowed in this view.".getBytes(StandardCharsets.UTF_8)));
            }
            @Override public void onPageFinished(WebView view,String url) {
                if (!pageFailed && isLocal(Uri.parse(url))) verifyPageReady(view,pageGeneration,0);
            }
            @Override public void onReceivedError(WebView view,WebResourceRequest request,WebResourceError error) {
                if(request.isForMainFrame()) { pageFailed=true; showStatus("本地页面暂时无法加载。请重试加载。",true); }
            }
            @Override public void onReceivedHttpError(WebView view,WebResourceRequest request,WebResourceResponse response) {
                if(request.isForMainFrame() && response.getStatusCode()>=400) { pageFailed=true; showStatus("本地服务返回错误（"+response.getStatusCode()+"）。请重试加载。",true); }
            }
            @Override public boolean onRenderProcessGone(WebView view,RenderProcessGoneDetail detail) {
                if(voice!=null) { voice.close(); voice=null; }
                if(speech!=null) { speech.close(); speech=null; }
                if(pairing!=null) { pairing.close(); pairing=null; }
                rendererGone=true; cookieGeneration++; pageGeneration++; origin=null;
                if(view.getParent() instanceof ViewGroup) ((ViewGroup)view.getParent()).removeView(view);
                view.destroy(); web=null;
                signalStop(); showStatus("页面进程已结束，本地服务正在停止。请关闭后重新打开应用。",false); return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onPermissionRequest(PermissionRequest request) { request.deny(); }
            @Override public void onGeolocationPermissionsShowPrompt(String site,GeolocationPermissions.Callback callback) { callback.invoke(site,false,false); }
            @Override public boolean onShowFileChooser(WebView view,ValueCallback<Uri[]> callback,FileChooserParams params) {
                if(fileCallback!=null) fileCallback.onReceiveValue(null);
                fileCallback=callback; systemPicker=true;
                Intent intent=new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE,params.getMode()==FileChooserParams.MODE_OPEN_MULTIPLE);
                try { startActivityForResult(intent,OPEN_FILE); return true; }
                catch(ActivityNotFoundException e) { systemPicker=false; fileCallback=null; callback.onReceiveValue(null); return false; }
            }
            @Override public boolean onCreateWindow(WebView view,boolean dialog,boolean gesture,Message result) {
                if(!gesture) return false;
                WebView popup=new WebView(MainActivity.this);
                popup.getSettings().setJavaScriptEnabled(false); popup.getSettings().setAllowFileAccess(false); popup.getSettings().setAllowContentAccess(false);
                popup.setWebViewClient(new WebViewClient() {
                    @Override public boolean shouldOverrideUrlLoading(WebView v,WebResourceRequest request) {
                        Uri uri=request.getUrl(); if(isLocal(uri)) web.loadUrl(uri.toString()); else openExternal(uri);
                        popup.destroy(); return true;
                    }
                    @Override public WebResourceResponse shouldInterceptRequest(WebView v,WebResourceRequest request) {
                        return new WebResourceResponse("text/plain","UTF-8",new ByteArrayInputStream(new byte[0]));
                    }
                });
                ((WebView.WebViewTransport)result.obj).setWebView(popup); result.sendToTarget(); return true;
            }
        });
        if(Build.VERSION.SDK_INT>=29) web.setWebViewRenderProcessClient(new WebViewRenderProcessClient() {
            @Override public void onRenderProcessUnresponsive(WebView view,WebViewRenderProcess renderer) {
                pageGeneration++; showStatus("页面暂时无响应，仍可使用右上角急停。",true);
            }
            @Override public void onRenderProcessResponsive(WebView view,WebViewRenderProcess renderer) {
                verifyPageReady(view,pageGeneration,0);
            }
        });
        web.setDownloadListener((url,userAgent,disposition,mime,size) -> saveQr(url,mime));
    }
    private boolean isLocal(Uri uri) {
        return origin!=null && "http".equalsIgnoreCase(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
            && uri.getUserInfo()==null && Uri.parse(origin).getPort()==uri.getPort();
    }
    private void openExternal(Uri uri) {
        String scheme=uri.getScheme();
        if(!"https".equalsIgnoreCase(scheme) && !"http".equalsIgnoreCase(scheme)) return;
        if(uri.getHost()==null || uri.getUserInfo()!=null) return;
        try { startActivity(new Intent(Intent.ACTION_VIEW,uri).addCategory(Intent.CATEGORY_BROWSABLE)); }
        catch(ActivityNotFoundException ignored) { ToastMessage.show(this,"没有可打开网页的应用"); }
    }
    private void verifyPageReady(WebView view,long generation,int attempt) {
        if(destroyed || rendererGone || web!=view || pageFailed || generation!=pageGeneration || view.getUrl()==null || !isLocal(Uri.parse(view.getUrl()))) return;
        // Keep a native emergency action visible until React has mounted its own.
        view.evaluateJavascript("document.querySelector('[data-coyote-estop]')!==null",result -> {
            if(destroyed || rendererGone || web!=view || pageFailed || generation!=pageGeneration) return;
            if("true".equals(result)) {
                pageReady=true; statusPanel.setVisibility(View.GONE); fallbackEstop.setVisibility(View.GONE); web.setVisibility(View.VISIBLE);
                if(pairing!=null) pairing.onPageReady();
                if(!voiceCachePreverified) {
                    voiceCachePreverified=true;
                    // Existing private cache only; this method starts its own low-priority worker.
                    ContinuousVoiceRecognizer.preverifyCachedModels(getApplicationContext());
                }
            } else if(attempt<40) main.postDelayed(()->verifyPageReady(view,generation,attempt+1),200);
            else showStatus("页面未能完整加载。请重试加载。",true);
        });
    }
    private void startRuntime() {
        if(rendererGone) { showStatus("页面进程已结束，请关闭后重新打开应用。",false); return; }
        closing=false; showStatus("正在启动本地服务…",false);
        if(service!=null && "ready".equals(service.getSnapshot().status)) {
            origin=null; onStateChanged(service.getSnapshot()); return;
        }
        try {
            Intent intent=new Intent(this,RuntimeService.class).setAction(RuntimeService.START);
            if(Build.VERSION.SDK_INT>=26) startForegroundService(intent); else startService(intent);
            if(!bound) bound=bindService(new Intent(this,RuntimeService.class),connection,Context.BIND_AUTO_CREATE);
        } catch(Exception failure) { showStatus("系统无法启动本地服务。请检查应用权限后重试。",true); }
    }
    @Override public void onStateChanged(RuntimeService.Snapshot state) {
        if(destroyed) return;
        if(rendererGone) { showStatus("页面进程已结束，请关闭后重新打开应用。",false); return; }
        if(!"ready".equals(state.status)) {
            cookieGeneration++; pageGeneration++; origin=null; web.stopLoading(); web.loadUrl("about:blank"); web.setVisibility(View.INVISIBLE);
            showStatus(state.message,"error".equals(state.status)||"stopped".equals(state.status)); return;
        }
        if(state.origin().equals(origin)) return;
        origin=state.origin(); pageFailed=false; final String expected=origin; final long generation=++cookieGeneration;
        if(voice!=null) voice.installOrigin(origin);
        if(speech!=null) speech.installOrigin(origin);
        if(pairing!=null) pairing.installOrigin(origin);
        CookieManager manager=CookieManager.getInstance();
        // Queue writes directly in main-thread session order. No delayed clear/set chain
        // can overwrite a newer token; superseded callbacks must never load a page.
        manager.setCookie(expected,
            "coyote_session="+state.token+"; Path=/; HttpOnly; SameSite=Strict",ok -> {
                if(!destroyed && !rendererGone && generation==cookieGeneration && expected.equals(origin)) {
                    if(Boolean.TRUE.equals(ok)) web.loadUrl(expected+"/");
                    else showStatus("本地会话无法建立，请重新启动。",true);
                }
            });
    }
    private void showStatus(String text,boolean retry) {
        if(voice!=null) voice.cancel("");
        if(speech!=null) speech.cancel("");
        if(pairing!=null) pairing.onPageStarted();
        if(destroyed) return; pageReady=false; message.setText(text); statusPanel.setVisibility(View.VISIBLE); fallbackEstop.setVisibility(View.VISIBLE);
        statusPanel.findViewWithTag("retry").setVisibility(retry?View.VISIBLE:View.GONE);
    }
    private void handleBack() {
        if(backPending || closing || destroyed) return;
        if(web==null || !pageReady || web.getUrl()==null || !isLocal(Uri.parse(web.getUrl()))) { chooseClose(); return; }
        backPending=true; final long generation=++backGeneration;
        // This invokes a fixed, local UI navigation hook; no JavascriptInterface is exposed.
        main.postDelayed(()->completeBack(generation,false),600);
        web.evaluateJavascript("(()=>{try{return typeof window.coyoteHandleBack==='function' && window.coyoteHandleBack()===true;}catch(_){return false;}})()",
            result->completeBack(generation,"true".equals(result)));
    }
    private void completeBack(long generation,boolean handled) {
        if(destroyed || !backPending || generation!=backGeneration) return;
        backPending=false; if(!handled) chooseClose();
    }
    private void chooseClose() {
        if(closing || (exitDialog!=null && exitDialog.isShowing())) return;
        exitDialog=new AlertDialog.Builder(this).setTitle("关闭页面")
            .setMessage("亮屏切换应用时服务会继续运行，息屏会停止。")
            .setPositiveButton("停止并退出",(d,w)->{ closing=true; signalStop(); finish(); })
            .setNegativeButton("保持后台",(d,w)->{
                if(service==null) { ToastMessage.show(this,"请等待本地服务就绪后再保持后台"); return; }
                moveTaskToBack(true);
            })
            .setNeutralButton("取消",null).create();
        exitDialog.setOnDismissListener(dialog->exitDialog=null); exitDialog.show();
    }
    @Override public void onBackPressed() { handleBack(); }
    private void signalStop() {
        if(voice!=null) voice.cancel("");
        if(speech!=null) speech.cancel("");
        if(pairing!=null) pairing.onPageStarted();
        if(service!=null) service.requestStop();
        else {
            // Also cancel a start which hasn't delivered its local Binder yet.
            try { startService(new Intent(this,RuntimeService.class).setAction(RuntimeService.STOP)); }
            catch(IllegalStateException ignored) { stopService(new Intent(this,RuntimeService.class)); }
        }
    }
    @Override protected void onResume() {
        super.onResume();
        if(voice!=null) voice.onResume();
        if(speech!=null) speech.onResume();
        if(pairing!=null) pairing.onResume();
    }
    @Override protected void onPause() {
        if(voice!=null) voice.onPause();
        if(speech!=null) speech.onPause();
        if(pairing!=null) pairing.onPause();
        super.onPause();
    }
    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if(request==VoiceBridge.MICROPHONE_PERMISSION && voice!=null) voice.onPermissionResult(results);
    }
    @Override protected void onDestroy() {
        destroyed=true;
        if(voice!=null) { voice.close(); voice=null; }
        if(speech!=null) { speech.close(); speech=null; }
        if(pairing!=null) { pairing.close(); pairing=null; }
        main.removeCallbacksAndMessages(null);
        if(fileCallback!=null) fileCallback.onReceiveValue(null);
        if(service!=null) service.removeListener(this);
        if(bound) unbindService(connection);
        cookieGeneration++;
        if(web!=null) { web.stopLoading(); web.destroy(); }
        super.onDestroy();
    }
    private void saveQr(String url,String mime) {
        Uri uri=Uri.parse(url);
        if(!isLocal(uri) || !"/api/qrcode.png".equals(uri.getPath())) { ToastMessage.show(this,"仅支持保存本地配对二维码"); return; }
        pendingDownload=url; systemPicker=true;
        Intent intent=new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
            .setType("image/png").putExtra(Intent.EXTRA_TITLE,"coko DG-配对二维码.png");
        try { startActivityForResult(intent,SAVE_QR); }
        catch(ActivityNotFoundException e) { systemPicker=false; pendingDownload=null; ToastMessage.show(this,"系统文件保存器不可用"); }
    }
    @Override protected void onActivityResult(int request,int result,Intent data) {
        super.onActivityResult(request,result,data); systemPicker=false;
        if(request==OPEN_FILE && fileCallback!=null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result,data)); fileCallback=null;
        }
        if(request==SAVE_QR) {
            String download=pendingDownload; pendingDownload=null;
            if(result!=RESULT_OK || data==null || data.getData()==null || download==null || !isLocal(Uri.parse(download))) return;
            Uri destination=data.getData(); String cookie=CookieManager.getInstance().getCookie(download);
            java.util.concurrent.ExecutorService io=Executors.newSingleThreadExecutor();
            io.execute(() -> {
                boolean success=false; HttpURLConnection connection=null;
                try {
                    connection=(HttpURLConnection)new URL(download).openConnection(); connection.setInstanceFollowRedirects(false);
                    connection.setConnectTimeout(5000); connection.setReadTimeout(5000);
                    if(cookie!=null) connection.setRequestProperty("Cookie",cookie);
                    if(connection.getResponseCode()!=200 || !"image/png".equalsIgnoreCase(connection.getContentType().split(";")[0])) throw new IOException("QR unavailable");
                    ByteArrayOutputStream bytes=new ByteArrayOutputStream();
                    try(InputStream input=connection.getInputStream()) {
                        byte[] buffer=new byte[8192]; int count;
                        while((count=input.read(buffer))!=-1) { if(bytes.size()+count>1024*1024) throw new IOException("QR too large"); bytes.write(buffer,0,count); }
                    }
                    try(OutputStream output=getContentResolver().openOutputStream(destination,"w")) {
                        if(output==null) throw new IOException("no output"); bytes.writeTo(output);
                    }
                    success=true;
                } catch(Exception ignored) {} finally { if(connection!=null) connection.disconnect(); }
                final boolean saved=success; runOnUiThread(()->ToastMessage.show(this,saved?"二维码已保存":"二维码保存失败，请刷新配对二维码后重试"));
            }); io.shutdown();
        }
    }
}
