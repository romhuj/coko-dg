package org.coyote.mobile;

import android.app.Activity;
import android.content.ClipData;
import android.content.ContentResolver;
import android.content.Intent;
import android.content.res.AssetFileDescriptor;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Build;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient.FileChooserParams;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.BooleanSupplier;

/** One chooser per trusted document. Returned URI grants never become a general file bridge. */
final class WebFilePicker implements AutoCloseable {
    interface Launcher { void launch(Intent intent, int requestCode); }
    interface Events {
        void pendingChanged(boolean awaitingResult);
        void failed(String message);
    }
    private static final int FIRST_REQUEST=0x4300, LAST_REQUEST=0x7fff, MAX_FILES=64;
    private final ContentResolver resolver;
    private final Launcher launcher;
    private final Events events;
    private final int sdk;
    private final Handler main=new Handler(Looper.getMainLooper());
    private final ExecutorService validation=Executors.newSingleThreadExecutor(r -> {
        Thread thread=new Thread(r,"coko-image-check"); thread.setDaemon(true); return thread;
    });
    private Request active;
    private int nextRequest=FIRST_REQUEST;
    private boolean closed;

    private static final class Request {
        final int code;
        final long page;
        final boolean images,multiple;
        final ValueCallback<Uri[]> callback;
        final CancellationSignal cancellation=new CancellationSignal();
        boolean awaiting=true;
        Future<?> validation;
        Runnable timeout;
        Request(int code,long page,boolean images,boolean multiple,ValueCallback<Uri[]> callback) {
            this.code=code;this.page=page;this.images=images;this.multiple=multiple;this.callback=callback;
        }
    }

    WebFilePicker(ContentResolver resolver,Launcher launcher,Events events) {
        this(resolver,launcher,events,Build.VERSION.SDK_INT);
    }
    WebFilePicker(ContentResolver resolver,Launcher launcher,Events events,int sdk) {
        this.resolver=resolver;this.launcher=launcher;this.events=events;this.sdk=sdk;
    }

    boolean open(ValueCallback<Uri[]> callback,FileChooserParams params,boolean trusted,long page) {
        cancel();
        if(closed || !trusted || params==null || nextRequest>LAST_REQUEST
                || (params.getMode()!=FileChooserParams.MODE_OPEN && params.getMode()!=FileChooserParams.MODE_OPEN_MULTIPLE)) {
            deliver(callback,null);return true;
        }
        boolean images=imageInput(params.getAcceptTypes()),multiple=params.getMode()==FileChooserParams.MODE_OPEN_MULTIPLE;
        Request request=new Request(nextRequest++,page,images,multiple,callback);
        active=request;events.pendingChanged(true);
        try {
            launcher.launch(intent(images,multiple,sdk),request.code);
        } catch(android.content.ActivityNotFoundException | SecurityException first) {
            if(images && !multiple && sdk>=33) {
                try {launcher.launch(document(true,false),request.code);return true;}
                catch(android.content.ActivityNotFoundException | SecurityException ignored) { }
            }
            finish(request,null);events.failed(images?"系统照片选择器不可用，请稍后重试":"系统文件选择器不可用");
        }
        return true;
    }

    boolean result(int requestCode,int resultCode,Intent data,long page,BooleanSupplier stillAllowed) {
        if(requestCode<FIRST_REQUEST || requestCode>LAST_REQUEST) return false;
        Request request=active;
        // Codes are never reused within this Activity; a replaced chooser cannot satisfy a new callback.
        if(request==null || request.code!=requestCode) return true;
        request.awaiting=false;events.pendingChanged(false);
        if(closed || request.page!=page || !stillAllowed.getAsBoolean() || resultCode!=Activity.RESULT_OK) {
            finish(request,null);return true;
        }
        Uri[] uris=contentUris(data,request.multiple);
        if(uris==null) {finish(request,null);events.failed("未选择可读取的文件");return true;}
        if(!request.images) {finish(request,uris);return true;}
        request.timeout=()->{
            if(active==request) {finish(request,null);events.failed("图片读取超时，请重新选择");}
        };
        main.postDelayed(request.timeout,10000);
        request.validation=validation.submit(()->{
            boolean valid=true;
            for(Uri uri:uris) if(!validImage(resolver,uri,request.cancellation)) {valid=false;break;}
            final boolean accepted=valid;
            main.post(()->{
                if(active!=request || closed) return;
                if(!stillAllowed.getAsBoolean()) {finish(request,null);return;}
                finish(request,accepted?uris:null);
                if(!accepted) events.failed("请选择不超过 12 MB、3200 万像素的有效图片");
            });
        });
        return true;
    }

    void cancel() {if(active!=null) finish(active,null);}
    boolean awaitingResult() {return active!=null && active.awaiting;}
    @Override public void close() {closed=true;cancel();validation.shutdownNow();}
    private void finish(Request request,Uri[] value) {
        if(active!=request) return;
        active=null;
        if(request.timeout!=null) main.removeCallbacks(request.timeout);
        request.cancellation.cancel();
        if(request.validation!=null) request.validation.cancel(true);
        events.pendingChanged(false);
        deliver(request.callback,value);
    }
    private static void deliver(ValueCallback<Uri[]> callback,Uri[] value) {
        try {callback.onReceiveValue(value);} catch(RuntimeException ignored) { }
    }

    static boolean imageInput(String[] accepts) {
        if(accepts==null || accepts.length==0) return false;
        boolean found=false;
        for(String raw:accepts) {
            if(raw==null) continue;
            for(String token:raw.split(",")) {
                String type=token.trim().toLowerCase(Locale.ROOT);
                if(type.isEmpty()) continue;
                if(!type.matches("image/(?:\\*|[a-z0-9.+-]+)")
                        && !type.matches("\\.(?:png|jpe?g|gif|webp|bmp|heic|heif|avif|tiff?)")) return false;
                found=true;
            }
        }
        return found;
    }
    static Intent intent(boolean images,boolean multiple,int sdk) {
        if(images && !multiple && sdk>=33) return new Intent(MediaStore.ACTION_PICK_IMAGES).setType("image/*");
        return document(images,multiple);
    }
    private static Intent document(boolean images,boolean multiple) {
        return new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                .setType(images?"image/*":"*/*").putExtra(Intent.EXTRA_ALLOW_MULTIPLE,multiple)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
    }
    static boolean localPage(Uri uri,String origin) {
        if(uri==null || origin==null) return false;
        Uri base=Uri.parse(origin);
        return "http".equals(uri.getScheme()) && "127.0.0.1".equals(uri.getHost())
                && uri.getUserInfo()==null && uri.getPort()>0 && uri.getPort()==base.getPort()
                && "http".equals(base.getScheme()) && "127.0.0.1".equals(base.getHost()) && base.getUserInfo()==null;
    }
    static Uri[] contentUris(Intent data,boolean multiple) {
        if(data==null) return null;
        ArrayList<Uri> uris=new ArrayList<>();
        ClipData clip=data.getClipData();
        if(clip!=null && clip.getItemCount()>0) {
            if(clip.getItemCount()>(multiple?MAX_FILES:1)) return null;
            for(int i=0;i<clip.getItemCount();i++) uris.add(clip.getItemAt(i).getUri());
            if(data.getData()!=null && !data.getData().equals(uris.get(0))) return null;
        } else if(data.getData()!=null) uris.add(data.getData());
        if(uris.isEmpty()) return null;
        for(Uri uri:uris) if(uri==null || !"content".equals(uri.getScheme()) || !uri.isHierarchical()
                || uri.getAuthority()==null || uri.getAuthority().isEmpty() || uri.getUserInfo()!=null) return null;
        return uris.toArray(new Uri[0]);
    }
    static boolean validImage(ContentResolver resolver,Uri uri,CancellationSignal cancellation) {
        try {
            cancellation.throwIfCanceled();
            String mime=resolver.getType(uri);
            if(mime!=null && !mime.toLowerCase(Locale.ROOT).startsWith("image/")) return false;
            try(AssetFileDescriptor descriptor=resolver.openAssetFileDescriptor(uri,"r",cancellation)) {
                if(descriptor==null || descriptor.getLength()>12L*1024*1024) return false;
                BitmapFactory.Options bounds=new BitmapFactory.Options();bounds.inJustDecodeBounds=true;
                try(InputStream input=descriptor.createInputStream()) {BitmapFactory.decodeStream(input,null,bounds);}
                cancellation.throwIfCanceled();
                return bounds.outWidth>0 && bounds.outHeight>0 && (long)bounds.outWidth*bounds.outHeight<=32_000_000L
                        && bounds.outMimeType!=null && bounds.outMimeType.startsWith("image/");
            }
        } catch(Exception invalid) {return false;}
    }
}
