package org.coyote.mobile;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.os.*;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import java.io.*;
import java.security.SecureRandom;
import android.util.Base64;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Owns Python independently of the WebView. No exported IPC or persistent session token. */
public final class RuntimeService extends Service {
    public static final String START = "org.coyote.mobile.START";
    public static final String STOP = "org.coyote.mobile.STOP";
    public static final String ESTOP = "org.coyote.mobile.ESTOP";
    private static final String CHANNEL = "coyote_runtime";
    private static final int NOTIFICATION = 41;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final CopyOnWriteArrayList<Listener> listeners = new CopyOnWriteArrayList<>();
    private final LocalBinder binder = new LocalBinder();
    private volatile Snapshot snapshot = new Snapshot("stopped", "本地服务未启动", 0, null);
    private volatile boolean stopRequested;
    private volatile boolean starting;
    private PowerManager.WakeLock stoppingWakeLock;
    private final BroadcastReceiver screenOff = new BroadcastReceiver() {
        @Override public void onReceive(Context context, Intent intent) {
            if (Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) {
                // Only finish shutdown while the CPU is awake; never keep playback awake.
                PowerManager power = getSystemService(PowerManager.class);
                if (stoppingWakeLock == null) {
                    stoppingWakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, getPackageName()+":finish-stop");
                    stoppingWakeLock.setReferenceCounted(false);
                }
                stoppingWakeLock.acquire(20_000L);
                requestStop();
            }
        }
    };

    public interface Listener { void onStateChanged(Snapshot state); }
    public static final class Snapshot {
        public final String status, message;
        public final int port;
        // This field only crosses our in-process Binder; never an Intent or log.
        final String token;
        Snapshot(String status, String message, int port, String token) {
            this.status=status; this.message=message; this.port=port; this.token=token;
        }
        public String origin() { return "http://127.0.0.1:" + port; }
    }
    public final class LocalBinder extends Binder { RuntimeService getService() { return RuntimeService.this; } }
    @Override public IBinder onBind(Intent intent) { return binder; }
    public void addListener(Listener listener) { listeners.add(listener); listener.onStateChanged(snapshot); }
    public void removeListener(Listener listener) { listeners.remove(listener); }
    public Snapshot getSnapshot() { return snapshot; }
    /** Bright-screen app switching is always allowed; screen-off still stops the session. */
    public boolean isBackgroundAllowed() { return !stopRequested && (starting || "ready".equals(snapshot.status)); }

    @Override public void onCreate() {
        super.onCreate();
        IntentFilter filter = new IntentFilter(Intent.ACTION_SCREEN_OFF);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(screenOff, filter, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(screenOff, filter);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL, "本地设备服务", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("本地服务运行状态，以及急停和停止入口");
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        // Android must never recreate a previous control session on its own.
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }
        String action = intent.getAction();
        if (STOP.equals(action)) { requestStop(); return START_NOT_STICKY; }
        if (ESTOP.equals(action)) { requestEstop(); return START_NOT_STICKY; }
        if (!START.equals(action)) { stopSelf(); return START_NOT_STICKY; }
        Notification notification = notification("ready".equals(snapshot.status) ? snapshot.message : "正在启动本地服务");
        if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        else startForeground(NOTIFICATION, notification);
        if (!starting && !"ready".equals(snapshot.status) && !"stopping".equals(snapshot.status)
                && !"stop_error".equals(snapshot.status)) startRuntime();
        else notifyStatus();
        return START_NOT_STICKY;
    }
    private void startRuntime() {
        starting=true; stopRequested=false;
        update(new Snapshot("starting", "正在准备本地服务，首次启动可能需要一些时间…", 0, null));
        worker.execute(() -> {
            try {
                File seed = prepareSeed();
                File data = new File(getFilesDir(), "runtime");
                if (!data.isDirectory() && !data.mkdirs()) throw new IOException("data directory");
                if (stopRequested) return;
                if (!Python.isStarted()) Python.start(new AndroidPlatform(getApplicationContext()));
                byte[] bytes = new byte[32]; new SecureRandom().nextBytes(bytes);
                String token = Base64.encodeToString(bytes, Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
                int port = Python.getInstance().getModule("backend.mobile_runtime")
                    .callAttr("start", data.getAbsolutePath(), seed.getAbsolutePath(), token).toInt();
                if (port < 1 || port > 65535) throw new IOException("invalid server port");
                if (!stopRequested) update(new Snapshot("ready", "本地服务运行中", port, token));
            } catch (Exception failure) {
                boolean stopped = safePythonStop();
                if (!stopRequested) {
                    update(new Snapshot(stopped ? "error" : "stop_error", "本地服务启动失败（" + failure.getClass().getSimpleName()
                        + (stopped ? "）。请停止后重新启动；若仍失败，请检查安装包与系统可用空间。"
                        : "），停止尚未确认。请通过通知重试停止，并在 DG-LAB 中确认设备状态。"), 0, null));
                    if (stopped) main.post(() -> stopForeground(STOP_FOREGROUND_REMOVE));
                }
            } finally { starting=false; }
        });
    }
    public void requestEstop() {
        ReplySpeech.stopActive("已请求急停，朗读已关闭");
        VoiceCaptureService.stopActive("已请求急停，麦克风已关闭");
        worker.execute(() -> {
            if (Python.isStarted()) {
                try {
                    Python.getInstance().getModule("backend.mobile_runtime").callAttr("estop");
                    main.post(() -> ToastMessage.show(this, "已请求急停，请查看设备状态"));
                } catch (Exception failure) {
                    main.post(() -> ToastMessage.show(this, "急停未确认，正在停止本地服务"));
                    requestStop();
                }
            }
        });
    }
    public void requestStop() {
        ReplySpeech.stopActive("本地服务正在停止，朗读已关闭");
        VoiceCaptureService.stopActive("本地服务正在停止，麦克风已关闭");
        if (stopRequested && !"stop_error".equals(snapshot.status)) return;
        stopRequested=true;
        update(new Snapshot("stopping", "正在停止本地服务…", 0, null));
        worker.execute(() -> {
            if (safePythonStop()) {
                update(new Snapshot("stopped", "本地服务已停止", 0, null));
                main.post(() -> { releaseStoppingWakeLock(); stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); });
            } else {
                update(new Snapshot("stop_error", "停止尚未确认。请通过通知重试停止，并在 DG-LAB 中确认设备状态。", 0, null));
                main.post(this::releaseStoppingWakeLock);
            }
        });
    }
    private boolean safePythonStop() {
        if (!Python.isStarted()) return true;
        try { Python.getInstance().getModule("backend.mobile_runtime").callAttr("stop"); return true; }
        catch (Exception ignored) { return false; /* Never expose Python exceptions containing user configuration. */ }
    }
    @Override public void onTaskRemoved(Intent intent) { if (!isBackgroundAllowed()) requestStop(); }
    @Override public void onDestroy() {
        ReplySpeech.stopActive("本地服务已结束，朗读已关闭");
        VoiceCaptureService.stopActive("本地服务已结束，麦克风已关闭");
        stopRequested=true;
        unregisterReceiver(screenOff); releaseStoppingWakeLock();
        worker.execute(() -> { safePythonStop(); }); worker.shutdown();
        listeners.clear();
        super.onDestroy();
    }
    private void releaseStoppingWakeLock() {
        if (stoppingWakeLock != null && stoppingWakeLock.isHeld()) stoppingWakeLock.release();
    }
    private void update(Snapshot state) {
        snapshot=state;
        main.post(() -> {
            for (Listener listener : listeners) listener.onStateChanged(state);
            notifyStatus();
        });
    }
    private void notifyStatus() {
        if ("ready".equals(snapshot.status) || "starting".equals(snapshot.status) || "stop_error".equals(snapshot.status)) {
            String label = snapshot.message + (isBackgroundAllowed() ? " · 亮屏后台保持，息屏停止" : "");
            getSystemService(NotificationManager.class).notify(NOTIFICATION, notification(label));
        }
    }
    private PendingIntent action(String action, int code) {
        return PendingIntent.getService(this, code, new Intent(this, RuntimeService.class).setAction(action), PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
    }
    private Notification notification(String text) {
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent content = PendingIntent.getActivity(this, 1, open, PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        return builder.setSmallIcon(R.drawable.ic_coyote).setContentTitle("coko DG")
            .setContentText(text).setContentIntent(content).setOngoing(true).setOnlyAlertOnce(true)
            .addAction(new Notification.Action.Builder(null, "急停", action(ESTOP, 2)).build())
            .addAction(new Notification.Action.Builder(null, "停止服务", action(STOP, 3)).build()).build();
    }
    private File prepareSeed() throws Exception {
        long installed = getPackageManager().getPackageInfo(getPackageName(), 0).lastUpdateTime;
        File root = new File(getNoBackupFilesDir(), "seed-" + installed);
        File complete = new File(root, ".complete");
        if (!complete.isFile()) {
            copyAsset("runtime_seed", root);
            if (!new File(root, "config/config.yaml").isFile() || !new File(root, "frontend/dist/index.html").isFile())
                throw new IOException("runtime assets missing");
            try (FileOutputStream stream = new FileOutputStream(complete)) { stream.write(1); }
        }
        return root;
    }
    private void copyAsset(String path, File destination) throws IOException {
        String[] children = getAssets().list(path);
        if (children != null && children.length > 0) {
            if (!destination.isDirectory() && !destination.mkdirs()) throw new IOException("asset directory");
            for (String child : children) copyAsset(path + "/" + child, new File(destination, child));
        } else {
            try (InputStream source = getAssets().open(path); OutputStream target = new FileOutputStream(destination)) {
                byte[] buffer = new byte[32768]; int count;
                while ((count=source.read(buffer)) != -1) target.write(buffer,0,count);
            }
        }
    }
}
