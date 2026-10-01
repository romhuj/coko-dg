package org.coyote.mobile;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.Notification;
import android.app.NotificationManager;
import android.content.Intent;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.service.notification.StatusBarNotification;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import junit.framework.Assert;

/** Exercises actual microphone FGS lifecycle with an injected non-recording engine. */
final class VoiceServiceSmoke extends Assert {
    static void verify(Instrumentation instrumentation, Activity activity) throws Exception {
        assertTrue("Voice service smoke must run only on an emulator", android.os.Build.MODEL.contains("sdk_gphone")
                || android.os.Build.HARDWARE.contains("ranchu"));
        instrumentation.getUiAutomation().grantRuntimePermission(activity.getPackageName(), "android.permission.RECORD_AUDIO");
        if (android.os.Build.VERSION.SDK_INT >= 33)
            instrumentation.getUiAutomation().grantRuntimePermission(activity.getPackageName(), "android.permission.POST_NOTIFICATIONS");
        instrumentation.runOnMainSync(() -> VoiceCaptureService.stopActive("测试准备"));
        await(() -> VoiceCaptureService.instanceForTests() == null, "Previous voice service did not stop");
        AtomicInteger starts=new AtomicInteger(), stops=new AtomicInteger();
        CopyOnWriteArrayList<String> states=new CopyOnWriteArrayList<>();
        CopyOnWriteArrayList<String> sentences=new CopyOnWriteArrayList<>();
        CopyOnWriteArrayList<String> previews=new CopyOnWriteArrayList<>();
        CopyOnWriteArrayList<Float> levels=new CopyOnWriteArrayList<>();
        AtomicBoolean enginePaused=new AtomicBoolean(), startedPaused=new AtomicBoolean();
        AtomicReference<ContinuousVoiceRecognizer.Listener> callbacks=new AtomicReference<>();
        VoiceCaptureService.testFactory=(context, listener) -> new VoiceCaptureService.Engine() {
            String session;
            { callbacks.set(listener); }
            public void start(String id) {
                session=id; starts.incrementAndGet(); startedPaused.set(enginePaused.get());
                listener.onState(id, "listening", 100, "");
            }
            public void setPlaybackActive(boolean suspended) {
                enginePaused.set(suspended);
                if(session!=null) listener.onState(session,"listening",100,suspended?"正在朗读":"正在聆听");
            }
            public void stop() { stops.incrementAndGet(); }
            public void close() { stop(); }
        };
        VoiceCaptureService.Client client=new VoiceCaptureService.Client(activity, new ContinuousVoiceRecognizer.Listener() {
            public void onState(String id, String status, int progress, String message) { states.add(id + ":" + status); }
            public void onSentence(String id, String utterance, String text) { sentences.add(utterance); }
            public void onLevel(String id,float level) { levels.add(level); }
            public void onPreview(String id,String text) { previews.add(text); }
        });
        boolean paused=false;
        try {
            instrumentation.runOnMainSync(() -> { VoiceCaptureService.suspendForPlayback(true); client.start("voice_service_001"); });
            await(() -> states.contains("voice_service_001:listening"), "User start did not start the microphone FGS");
            assertEquals(1, starts.get());
            assertTrue("An engine started during playback must inherit the pause before start",startedPaused.get());
            instrumentation.runOnMainSync(()->{
                callbacks.get().onSentence("voice_service_001","during-playback","应丢弃");
                callbacks.get().onLevel("voice_service_001",.8f);
                callbacks.get().onPreview("voice_service_001","播放期间旧文字");
                callbacks.get().onLevel("voice_service_001",0);
                callbacks.get().onPreview("voice_service_001","");
            });
            instrumentation.waitForIdleSync();
            assertTrue("Sentences cannot pass while the speaker is playing",sentences.isEmpty());
            assertFalse(levels.contains(.8f));assertFalse(previews.contains("播放期间旧文字"));
            assertTrue(levels.contains(0f));assertTrue(previews.contains(""));
            instrumentation.runOnMainSync(()->{
                VoiceCaptureService.suspendForPlayback(false);
                callbacks.get().onSentence("voice_service_001","after-playback","新的语音");
                callbacks.get().onLevel("voice_service_001",.3f);
                callbacks.get().onPreview("voice_service_001","临时预览");
            });
            await(()->sentences.contains("after-playback"),"Microphone replies did not resume after playback");
            await(()->levels.contains(.3f)&&previews.contains("临时预览"),"Live progress did not reach the current session");
            assertEquals("Live previews must never become chat sentences",1,sentences.size());
            instrumentation.runOnMainSync(()->{
                // Queue a result, then pause AND resume before MAIN can deliver that result.
                callbacks.get().onSentence("voice_service_001","queued-before-playback","过期语音");
                callbacks.get().onLevel("voice_service_001",.7f);
                callbacks.get().onPreview("voice_service_001","排队中的旧预览");
                VoiceCaptureService.suspendForPlayback(true);
                VoiceCaptureService.suspendForPlayback(false);
            });
            instrumentation.waitForIdleSync();
            assertFalse("Playback generation must discard already-posted old words",sentences.contains("queued-before-playback"));
            assertFalse(levels.contains(.7f));assertFalse(previews.contains("排队中的旧预览"));
            assertEquals("Playback must keep the same engine/session",1,starts.get());
            assertFalse(enginePaused.get());
            instrumentation.runOnMainSync(()->{
                callbacks.get().onDuplex("voice_service_001",true);
                // These were recorded before playback begins and must survive a full-duplex transition.
                callbacks.get().onSentence("voice_service_001","duplex-before-playback","不要截断我的开头");
                callbacks.get().onPreview("voice_service_001","完整的语音开头");
                VoiceCaptureService.setPlaybackActive(true);
                callbacks.get().onLevel("voice_service_001",.6f);
                callbacks.get().onSentence("voice_service_001","duplex-during-playback","播放期间说话");
                VoiceCaptureService.setPlaybackActive(false);
            });
            await(()->sentences.contains("duplex-before-playback") && sentences.contains("duplex-during-playback"),
                    "Full duplex must retain both prior and concurrent user speech");
            assertTrue(previews.contains("完整的语音开头"));assertTrue(levels.contains(.6f));
            assertTrue(VoiceCaptureService.isFullDuplex("voice_service_001"));
            instrumentation.runOnMainSync(()->{
                VoiceCaptureService.setPlaybackActive(true);
                callbacks.get().onSentence("voice_service_001","before-aec-lost","排队语音");
                callbacks.get().onDuplex("voice_service_001",false);
                callbacks.get().onSentence("voice_service_001","without-aec","不应当发送扬声器回声");
                callbacks.get().onPreview("voice_service_001","回声文字");
                assertTrue(VoiceCaptureService.isPlaybackSuspended());
            });
            instrumentation.waitForIdleSync();
            assertFalse(sentences.contains("before-aec-lost"));assertFalse(sentences.contains("without-aec"));
            assertFalse(previews.contains("回声文字"));
            assertFalse(VoiceCaptureService.isFullDuplex("voice_service_001"));
            instrumentation.runOnMainSync(()->VoiceCaptureService.setPlaybackActive(false));
            await(() -> notification(activity) != null, "Ongoing voice notification is missing");
            int stopped=stops.get();
            try (ParcelFileDescriptor command=instrumentation.getUiAutomation().executeShellCommand("input keyevent 3");
                 java.io.InputStream output=new ParcelFileDescriptor.AutoCloseInputStream(command)) {
                byte[] buffer=new byte[256]; while(output.read(buffer)!=-1) { }
            }
            paused=true;
            await(() -> !activity.hasWindowFocus(), "Home did not background the Activity");
            SystemClock.sleep(700);
            assertNotNull("Switching away must keep the voice service", VoiceCaptureService.instanceForTests());
            assertEquals("Activity pause must not stop capture", stopped, stops.get());
            Notification note=notification(activity);
            assertNotNull(note);
            assertTrue(note.actions != null && note.actions.length > 0);
            note.actions[0].actionIntent.send();
            await(() -> states.contains("voice_service_001:off"), "Notification stop did not publish off");
            await(() -> VoiceCaptureService.instanceForTests() == null, "Notification stop left the service running");
            assertTrue(stops.get() > stopped);

            returnToApp(instrumentation, activity); paused=false;
            instrumentation.runOnMainSync(() -> client.start("voice_service_002"));
            await(() -> states.contains("voice_service_002:listening"), "Second user start failed");
            instrumentation.runOnMainSync(() -> VoiceCaptureService.instanceForTests().onScreenOff());
            await(() -> states.contains("voice_service_002:off"), "Screen-off path did not publish off");
            await(() -> VoiceCaptureService.instanceForTests() == null, "Screen-off left the service running");
            int afterLock=starts.get();
            SystemClock.sleep(200);
            assertEquals("Screen-off must never restart capture", afterLock, starts.get());

            instrumentation.runOnMainSync(() -> client.start("voice_service_003"));
            await(() -> states.contains("voice_service_003:listening"), "Restart fixture did not start");
            instrumentation.runOnMainSync(() -> { client.stop(); client.start("voice_service_004"); });
            await(() -> states.contains("voice_service_004:listening"), "Old service teardown revoked the new start");
            assertFalse("Old teardown must not send off for the new session", states.contains("voice_service_004:off"));
            instrumentation.runOnMainSync(() -> VoiceCaptureService.instanceForTests().onTaskRemoved(new Intent()));
            await(() -> states.contains("voice_service_004:off"), "Task removal did not stop capture");
            await(() -> VoiceCaptureService.instanceForTests() == null, "Task removal left the voice service running");
        } finally {
            if (paused) returnToApp(instrumentation, activity);
            instrumentation.runOnMainSync(() -> {
                client.close(); VoiceCaptureService.stopActive("测试结束"); VoiceCaptureService.suspendForPlayback(false);
            });
            await(() -> VoiceCaptureService.instanceForTests() == null, "Voice test cleanup failed");
            VoiceCaptureService.testFactory=null;
        }
    }

    private static void returnToApp(Instrumentation instrumentation, Activity activity) throws Exception {
        instrumentation.runOnMainSync(() -> activity.startActivity(new Intent(activity, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)));
        await(activity::hasWindowFocus, "Could not return to the voice app");
    }

    private static Notification notification(Activity activity) {
        for (StatusBarNotification item : activity.getSystemService(NotificationManager.class).getActiveNotifications())
            if (item.getId() == VoiceCaptureService.NOTIFICATION) return item.getNotification();
        return null;
    }
    private static void await(BooleanSupplier condition, String message) throws Exception {
        long until=SystemClock.elapsedRealtime() + 12000;
        while (SystemClock.elapsedRealtime() < until) {
            if (condition.getAsBoolean()) return;
            SystemClock.sleep(50);
        }
        fail(message);
    }
}
