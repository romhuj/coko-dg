package org.coyote.mobile;

import android.Manifest;
import android.app.KeyguardManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import java.util.concurrent.CopyOnWriteArrayList;

/** Explicitly started, non-restarting microphone service. No exported IPC. */
public final class VoiceCaptureService extends Service {
    static final String START="org.coyote.mobile.VOICE_START";
    static final String STOP="org.coyote.mobile.VOICE_STOP";
    static final int NOTIFICATION=42;
    private static final String CHANNEL="coyote_voice";
    private static final Object GUARD=new Object();
    private static final Handler MAIN=new Handler(Looper.getMainLooper());
    private static final CopyOnWriteArrayList<ContinuousVoiceRecognizer.Listener> LISTENERS=new CopyOnWriteArrayList<>();
    private static long nextEpoch;
    private static long playbackEpoch;
    private static boolean playbackActive;
    private static Request requested;
    private static VoiceCaptureService running;
    private Request active;
    private long lifecycleEpoch;
    private Engine engine;
    private boolean destroyed, receiverRegistered;

    interface Engine {
        void start(String sessionId);
        void setPlaybackActive(boolean playing);
        default void refreshDuplex() { }
        void stop();
        void close();
    }

    /** Main-thread barrier. Full-duplex playback must not invalidate ongoing speech/preview. */
    public static void setPlaybackActive(boolean playing) {
        if(Looper.myLooper()!=Looper.getMainLooper())
            throw new IllegalStateException("Playback transitions must run on the main thread");
        VoiceCaptureService service;
        synchronized(GUARD) {
            if(playbackActive==playing) return;
            boolean before=suspendedLocked();
            playbackActive=playing;
            if(before!=suspendedLocked()) playbackEpoch++;
            service=running;
        }
        // Engine callbacks take GUARD; never hold it while entering the engine lock.
        if(service!=null && !service.destroyed && service.engine!=null)
            service.engine.setPlaybackActive(playing);
    }

    public static void suspendForPlayback(boolean playing) {setPlaybackActive(playing);}
    private static boolean suspendedLocked() {return playbackActive && (requested==null || !requested.duplex);}
    public static boolean isPlaybackSuspended() { synchronized(GUARD) { return suspendedLocked(); } }
    public static boolean isFullDuplex(String session) {
        synchronized(GUARD) {return requested!=null && requested.session.equals(session) && requested.duplex;}
    }
    interface EngineFactory { Engine create(Context context, ContinuousVoiceRecognizer.Listener listener); }
    // Package-private instrumentation injection; no JavaScript/Binder entry point exists.
    static volatile EngineFactory testFactory;
    private static final class Request {
        final long epoch;
        final String session;
        boolean duplex;
        String status="preparing", message="正在准备离线语音识别";
        int progress;
        Request(long epoch, String session) { this.epoch=epoch; this.session=session; }
    }

    /** Call start only after a visible Activity has obtained microphone permission. */
    public static final class Client implements AutoCloseable {
        private final Context context;
        private final ContinuousVoiceRecognizer.Listener listener;
        private Request owned;
        private boolean closed;
        public Client(Context context, ContinuousVoiceRecognizer.Listener listener) {
            this.context=context.getApplicationContext(); this.listener=listener;
            LISTENERS.add(listener);
        }
        public void start(String sessionId) {
            if (sessionId == null || !sessionId.matches("[A-Za-z0-9_-]{8,160}"))
                throw new IllegalArgumentException("Invalid voice session");
            Request request;
            synchronized (GUARD) {
                if (closed) throw new IllegalStateException("Voice client closed");
                if (owned != null && requested == owned && owned.session.equals(sessionId)) return;
                request=new Request(++nextEpoch, sessionId); owned=request; requested=request;
            }
            Intent intent=new Intent(context, VoiceCaptureService.class).setAction(START)
                    .putExtra("voice_session", sessionId).putExtra("voice_epoch", request.epoch);
            try {
                if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(intent);
                else context.startService(intent);
            } catch (RuntimeException failure) {
                synchronized (GUARD) { if (requested == request) { requested=null; nextEpoch++; } }
                notifyState(request.session, "error", 0, "无法启动后台语音服务，请回到应用并允许麦克风权限后重试");
                stopThrough(request.epoch);
                throw failure;
            }
        }
        public void stop() {
            Request stopped;
            synchronized (GUARD) {
                stopped=owned; owned=null;
                if (stopped == null || requested != stopped) return;
                requested=null; nextEpoch++;
            }
            notifyState(stopped.session, "off", 0, "语音输入已关闭");
            stopThrough(stopped.epoch);
        }
        @Override public void close() {
            synchronized (GUARD) { if (closed) return; closed=true; }
            stop(); LISTENERS.remove(listener);
        }
    }

    /** Device-service emergency stop, notification stop and screen-off share this path. */
    public static void stopActive(String reason) {
        Request wanted, captured;
        long cutoff;
        synchronized (GUARD) {
            wanted=requested; requested=null; cutoff=++nextEpoch;
            captured=running == null ? null : running.active;
        }
        String message=reason == null || reason.isEmpty() ? "语音输入已关闭" : reason;
        if (wanted != null) notifyState(wanted.session, "off", 0, message);
        if (captured != null && captured != wanted) notifyState(captured.session, "off", 0, message);
        stopThrough(cutoff);
    }

    private static void stopThrough(long cutoff) {
        Runnable stop=() -> {
            VoiceCaptureService service;
            synchronized (GUARD) { service=running; }
            if (service != null) service.stopThroughOnMain(cutoff);
        };
        if (Looper.myLooper() == Looper.getMainLooper()) stop.run(); else MAIN.post(stop);
    }

    private void stopThroughOnMain(long cutoff) {
        boolean stopEngine=false, stopService;
        synchronized (GUARD) {
            if (active != null && active.epoch <= cutoff) { active=null; stopEngine=true; }
            stopService=requested == null && active == null;
        }
        // Never take an engine lock while holding GUARD: engine callbacks may arrive on its worker.
        if (stopEngine && engine != null) engine.stop();
        if(stopEngine || stopService) releaseAudioRoute();
        if (stopService && !destroyed) { stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); }
    }

    private final BroadcastReceiver screenOff=new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) onScreenOff();
        }
    };

    void onScreenOff() { stopActive("已锁屏，麦克风已关闭"); }
    static VoiceCaptureService instanceForTests() { synchronized (GUARD) { return running; } }

    @Override public void onCreate() {
        super.onCreate();
        synchronized (GUARD) { running=this; lifecycleEpoch=nextEpoch; }
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel=new NotificationChannel(CHANNEL, "持续语音输入", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("切换应用继续监听，锁屏停止");
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
        IntentFilter filter=new IntentFilter(Intent.ACTION_SCREEN_OFF);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(screenOff, filter, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(screenOff, filter);
        receiverRegistered=true;
        ContinuousVoiceRecognizer.Listener listener=new ContinuousVoiceRecognizer.Listener() {
            public void onState(String session, String status, int progress, String message) {
                Request source=requestForCallback(session);
                if (source == null) return;
                MAIN.post(() -> {
                    if (!isCurrent(source)) return;
                    if (screenLocked()) { onScreenOff(); return; }
                    synchronized(GUARD) {source.status=status;source.progress=progress;source.message=message;}
                    String currentMessage=message;
                    if("listening".equals(status)) {
                        if(isPlaybackSuspended()) currentMessage=fallbackMessage();
                        else if(message!=null && (message.startsWith("正在朗读") || message.startsWith("当前音频路径"))) currentMessage="正在聆听，停顿后自动发送";
                    }
                    notifyState(session, status, progress, currentMessage);
                    if ("off".equals(status) || "error".equals(status)) {
                        synchronized (GUARD) { if (requested == source) { requested=null; nextEpoch++; } }
                        stopThrough(source.epoch);
                    }
                });
            }
            public void onSentence(String session, String utterance, String text) {
                Request source;
                long acceptedPlayback;
                synchronized(GUARD) {
                    source=requestForCallback(session);
                    if(source==null || suspendedLocked()) return;
                    acceptedPlayback=playbackEpoch;
                }
                MAIN.post(() -> {
                    synchronized(GUARD) {
                        if(!isCurrent(source) || suspendedLocked() || acceptedPlayback!=playbackEpoch) return;
                    }
                    if (screenLocked()) { onScreenOff(); return; }
                    for (ContinuousVoiceRecognizer.Listener target : LISTENERS) {
                        try { target.onSentence(session, utterance, text); } catch (RuntimeException ignored) { }
                    }
                });
            }
            public void onLevel(String session,float level) {
                if(!Float.isFinite(level)) return;
                Request source;
                long acceptedPlayback;
                float bounded=Math.max(0,Math.min(1,level));
                synchronized(GUARD) {
                    source=requestForCallback(session);
                    if(source==null || (suspendedLocked() && bounded!=0)) return;
                    acceptedPlayback=playbackEpoch;
                }
                MAIN.post(()->{
                    synchronized(GUARD) {
                        if(!isCurrent(source) || acceptedPlayback!=playbackEpoch || (suspendedLocked() && bounded!=0)) return;
                    }
                    if(screenLocked()) {onScreenOff();return;}
                    for(ContinuousVoiceRecognizer.Listener target:LISTENERS) {
                        try {target.onLevel(session,bounded);} catch(RuntimeException ignored) { }
                    }
                });
            }
            public void onPreview(String session,String text) {
                if(text==null || text.length()>8000) return;
                Request source;
                long acceptedPlayback;
                synchronized(GUARD) {
                    source=requestForCallback(session);
                    if(source==null || (suspendedLocked() && !text.isEmpty())) return;
                    acceptedPlayback=playbackEpoch;
                }
                MAIN.post(()->{
                    synchronized(GUARD) {
                        if(!isCurrent(source) || acceptedPlayback!=playbackEpoch || (suspendedLocked() && !text.isEmpty())) return;
                    }
                    if(screenLocked()) {onScreenOff();return;}
                    for(ContinuousVoiceRecognizer.Listener target:LISTENERS) {
                        try {target.onPreview(session,text);} catch(RuntimeException ignored) { }
                    }
                });
            }
            public void onDuplex(String session,boolean enabled) {
                Request source;
                synchronized(GUARD) {
                    source=requestForCallback(session);
                    if(source==null) return;
                    boolean before=suspendedLocked();
                    source.duplex=enabled;
                    if(before!=suspendedLocked()) playbackEpoch++;
                }
                MAIN.post(()->{
                    if(!isCurrent(source)) return;
                    if(screenLocked()) {onScreenOff();return;}
                    String message=source.message;
                    if("listening".equals(source.status)) message=isPlaybackSuspended() ? fallbackMessage() : "正在聆听，停顿后自动发送";
                    notifyState(session,source.status,source.progress,message);
                });
            }
            public void onSpeechStart(String session) {
                Request source;
                long token=ReplySpeech.currentPlaybackToken();
                synchronized(GUARD) {
                    source=requestForCallback(session);
                    if(source==null || !source.duplex || suspendedLocked() || token==0) return;
                }
                MAIN.post(()->{
                    synchronized(GUARD) {
                        if(!isCurrent(source) || !source.duplex || suspendedLocked()) return;
                    }
                    if(screenLocked()) {onScreenOff();return;}
                    // The speech owner validates the captured utterance token again atomically.
                    ReplySpeech.interruptForUserSpeech(token);
                });
            }
        };
        EngineFactory factory=testFactory;
        if (factory != null) engine=factory.create(this, listener);
        else {
            ContinuousVoiceRecognizer recognizer=new ContinuousVoiceRecognizer(this, listener);
            engine=new Engine() {
                public void start(String session) { recognizer.start(session); }
                public void setPlaybackActive(boolean playing) { recognizer.setPlaybackActive(playing); }
                public void refreshDuplex() {recognizer.refreshDuplex();}
                public void stop() { recognizer.stop(); }
                public void close() { recognizer.close(); }
            };
        }
    }

    private Request requestForCallback(String session) {
        synchronized (GUARD) {
            return !destroyed && active != null && requested == active && active.session.equals(session) ? active : null;
        }
    }
    private boolean isCurrent(Request request) {
        synchronized (GUARD) { return !destroyed && active == request && requested == request; }
    }
    private boolean screenLocked() {
        PowerManager power=getSystemService(PowerManager.class);
        KeyguardManager keyguard=getSystemService(KeyguardManager.class);
        return power == null || !power.isInteractive() || (keyguard != null && keyguard.isKeyguardLocked());
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopActive("语音服务已停止，请手动重新开启"); stopSelf(); return START_NOT_STICKY; }
        if (STOP.equals(intent.getAction())) { stopActive("已从通知关闭麦克风"); return START_NOT_STICKY; }
        if (!START.equals(intent.getAction())) { stopThroughOnMain(Long.MIN_VALUE); return START_NOT_STICKY; }
        Request request;
        synchronized (GUARD) {
            request=requested;
            if (request == null || request.epoch != intent.getLongExtra("voice_epoch", -1)
                    || !request.session.equals(intent.getStringExtra("voice_session"))) {
                if (requested == null && active == null) stopSelfResult(startId);
                return START_NOT_STICKY;
            }
        }
        if (screenLocked()) { onScreenOff(); stopSelfResult(startId); return START_NOT_STICKY; }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            notifyState(request.session, "error", 0, "需要麦克风权限才能开启持续语音输入");
            stopActive(""); stopSelfResult(startId); return START_NOT_STICKY;
        }
        try {
            if (Build.VERSION.SDK_INT >= 30) startForeground(NOTIFICATION, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            else startForeground(NOTIFICATION, notification());
        } catch (RuntimeException failure) {
            notifyState(request.session, "error", 0, "后台语音服务启动失败，请回到应用后重试");
            stopActive(""); stopSelfResult(startId); return START_NOT_STICKY;
        }
        boolean stale;
        synchronized (GUARD) {
            stale=requested != request;
            if (!stale) { active=request; lifecycleEpoch=request.epoch; }
        }
        if (stale) { stopThrough(request.epoch); return START_NOT_STICKY; }
        try {
            if(!CommunicationAudioSession.acquire(this,this)) {
                notifyState(request.session,"error",0,"无法取得语音通话音频路径，请结束其他通话后重试");
                stopActive("");return START_NOT_STICKY;
            }
            CommunicationAudioSession.addListener(this,()->{if(!destroyed && engine!=null) engine.refreshDuplex();});
            boolean playing;
            synchronized(GUARD) {playing=playbackActive;}
            engine.setPlaybackActive(playing);engine.start(request.session);
        }
        catch (RuntimeException failure) {
            notifyState(request.session, "error", 0, "语音引擎启动失败，请重新开启");
            stopActive("");
        }
        return START_NOT_STICKY;
    }

    private Notification notification() {
        Intent open=new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent show=PendingIntent.getActivity(this, 420, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop=PendingIntent.getService(this, 421, new Intent(this, VoiceCaptureService.class).setAction(STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder=Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        return builder.setSmallIcon(R.drawable.ic_coyote).setContentTitle("coko DG · 持续语音输入")
                .setContentText("切换应用继续监听，锁屏停止").setContentIntent(show)
                .setOngoing(true).setOnlyAlertOnce(true).setCategory(Notification.CATEGORY_SERVICE)
                .addAction(new Notification.Action.Builder(R.drawable.ic_estop, "关闭麦克风", stop).build()).build();
    }

    private static void notifyState(String session, String status, int progress, String message) {
        for (ContinuousVoiceRecognizer.Listener listener : LISTENERS) {
            try { listener.onState(session, status, progress, message); } catch (RuntimeException ignored) { }
        }
    }
    private static String fallbackMessage() {return "当前音频路径不支持回声消除，朗读时暂停识别";}
    private void releaseAudioRoute() {
        CommunicationAudioSession.removeListener(this);
        CommunicationAudioSession.release(this);
    }
    @Override public IBinder onBind(Intent intent) { return null; }
    @Override public void onTaskRemoved(Intent rootIntent) {
        stopActive("应用任务已关闭，麦克风已关闭"); super.onTaskRemoved(rootIntent);
    }
    @Override public void onDestroy() {
        Request old;
        synchronized (GUARD) {
            destroyed=true; old=active; active=null;
            if (running == this) {
                running=null;
                // A new start may be queued after this instance called stopSelf.
                // Old teardown must not revoke that newer, not-yet-accepted request.
                if (requested != null && requested.epoch <= lifecycleEpoch) {
                    if (old == null) old=requested;
                    requested=null; nextEpoch++;
                }
            }
        }
        if (receiverRegistered) { unregisterReceiver(screenOff); receiverRegistered=false; }
        if (engine != null) engine.close();
        releaseAudioRoute();
        if (old != null) notifyState(old.session, "off", 0, "语音服务已停止");
        stopForeground(STOP_FOREGROUND_REMOVE);
        super.onDestroy();
    }
}
