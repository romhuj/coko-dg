package org.coyote.mobile;

import android.content.Context;
import android.os.SystemClock;
import android.test.InstrumentationTestCase;
import android.test.InstrumentationTestRunner;
import com.k2fsa.sherpa.onnx.GeneratedAudio;
import com.k2fsa.sherpa.onnx.OfflineTts;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicBoolean;

/** Explicit opt-in, public fixture text only. Model fixtures must be preinstalled; no microphone. */
@SuppressWarnings("deprecation")
public final class OfflineReplyVoiceTest extends InstrumentationTestCase {
    private static final String TEXT="你好，我是郊狼。这是一段离线中文朗读测试。";

    private boolean enabled(String name) {
        return "true".equals(((InstrumentationTestRunner)getInstrumentation()).getArguments().getString(name,"false"));
    }

    public void testManifestVoiceIdsAndSpeakerMapping() throws Exception {
        Context context=getInstrumentation().getTargetContext();
        TtsModel female=new TtsModel(context,"kokoro-zf_001"), male=new TtsModel(context,"kokoro-zm_010");
        assertEquals(3,female.speakerId);assertEquals(59,male.speakerId);assertEquals(female.directory,male.directory);
        assertEquals(0,new TtsModel(context,"melo-zh").speakerId);
        try {new TtsModel(context,"unknown-voice");fail("Unknown voice accepted");}
        catch(java.io.IOException expected) { }
        try {female.resolve("../outside");fail("Path escape accepted");}
        catch(java.io.IOException expected) { }
    }

    public void testBundledSmallResourcesVerifyWithoutLargeModelsOrNetwork() throws Exception {
        Context context=getInstrumentation().getTargetContext();
        File root=File.createTempFile("tts-data-test-","",context.getCacheDir());
        assertTrue(root.delete());assertTrue(root.mkdir());
        try {
            for(String voice:new String[]{"kokoro-zf_001","melo-zh"}) {
                TtsModel model=new TtsModel(context,voice,root);
                model.installBundled(new VoiceModel.CancelToken(),message->{});
                assertFalse(new File(model.directory,"model.int8.onnx").exists());
                assertFalse(new File(model.directory,"voices.bin").exists());
                JSONObject manifest;
                try(InputStream input=context.getAssets().open("tts/"+model.pack+".json")) {
                    ByteArrayOutputStream data=new ByteArrayOutputStream();byte[] buffer=new byte[8192];int count;
                    while((count=input.read(buffer))!=-1)data.write(buffer,0,count);
                    manifest=new JSONObject(data.toString(StandardCharsets.UTF_8.name()));
                }
                JSONArray files=manifest.getJSONArray("files");int checked=0;
                for(int i=0;i<files.length();i++) {
                    JSONObject item=files.getJSONObject(i);String path=item.getString("path");
                    if(path.equals("model.int8.onnx")||path.equals("voices.bin"))continue;
                    VoiceModel.Spec spec=new VoiceModel.Spec(path,"https://example.invalid/unused",item.getLong("bytes"),item.getString("sha256"));
                    assertTrue("Bundled SHA mismatch: "+path,VoiceModel.verified(new File(model.directory,path),spec,new VoiceModel.CancelToken()));
                    checked++;
                }
                assertTrue(checked>0);
            }
        } finally {removeTestFiles(root);}
    }

    public void testCloseBeforeInitializationSuppressesReadyAndDoesNoIo() {
        List<Runnable> work=new ArrayList<>();AtomicInteger callbacks=new AtomicInteger();
        ReplySpeech.Callbacks events=new ReplySpeech.Callbacks(){
            public void ready(String error){callbacks.incrementAndGet();}
            public void started(String id){callbacks.incrementAndGet();}
            public void done(String id){callbacks.incrementAndGet();}
            public void failed(String id,String error){callbacks.incrementAndGet();}
            public void preparing(String message){callbacks.incrementAndGet();}
        };
        OfflineReplyVoice voice=new OfflineReplyVoice(getInstrumentation().getTargetContext(),"melo-zh",events,work::add);
        voice.close();for(Runnable pending:new ArrayList<>(work))pending.run();
        assertEquals(0,callbacks.get());assertFalse(voice.speak(TEXT,"closed"));
    }

    private static void removeTestFiles(File file) {
        File[] children=file.listFiles();if(children!=null)for(File child:children)removeTestFiles(child);
        file.delete();
    }

    public void testPinnedVoicesSynthesizeChineseOffline() throws Exception {
        if(!enabled("allow_offline_tts_test"))return;
        Context context=getInstrumentation().getTargetContext();
        File out=new File(context.getExternalFilesDir(null),"tts-generated");
        assertTrue(out.isDirectory()||out.mkdirs());
        JSONArray entries=new JSONArray();
        TtsModel female=new TtsModel(context,"kokoro-zf_001");
        female.prepare(new VoiceModel.CancelToken(),message->{},false);
        long started=SystemClock.elapsedRealtime();
        OfflineTts kokoro=female.create(); long loadMs=SystemClock.elapsedRealtime()-started;
        try {
            assertEquals(103,kokoro.numSpeakers());
            GeneratedAudio a=synthesize(kokoro,3,"kokoro-zf_001",out,entries,loadMs);
            GeneratedAudio b=synthesize(kokoro,59,"kokoro-zm_010",out,entries,0);
            assertFalse("Female and male speaker IDs produced identical PCM",Arrays.equals(a.getSamples(),b.getSamples()));
        } finally {kokoro.release();}
        TtsModel meloModel=new TtsModel(context,"melo-zh");
        meloModel.prepare(new VoiceModel.CancelToken(),message->{},false);
        started=SystemClock.elapsedRealtime();OfflineTts melo=meloModel.create();loadMs=SystemClock.elapsedRealtime()-started;
        try {synthesize(melo,0,"melo-zh",out,entries,loadMs);}
        finally {melo.release();}
        JSONObject report=new JSONObject().put("status","passed").put("text",TEXT)
                .put("network_used",false).put("microphone_used",false).put("voices",entries);
        try(FileOutputStream file=new FileOutputStream(new File(out,"report.json"))) {
            file.write(report.toString(2).getBytes(StandardCharsets.UTF_8));
        }
        android.util.Log.i("CoyoteOfflineTtsTest",report.toString());
    }

    private GeneratedAudio synthesize(OfflineTts tts,int sid,String voice,File out,JSONArray report,long loadMs) throws Exception {
        long started=SystemClock.elapsedRealtime();GeneratedAudio audio=tts.generate(TEXT,sid,1.0f);
        long elapsed=SystemClock.elapsedRealtime()-started;
        float[] samples=audio.getSamples();int rate=audio.getSampleRate();
        assertTrue("Unexpected sample rate",rate==24000||rate==44100);
        assertTrue("Missing Chinese audio",samples.length>rate/2&&samples.length<rate*60);
        double energy=0;
        for(float value:samples) {assertFalse("Invalid PCM",Float.isNaN(value)||Float.isInfinite(value));energy+=(double)value*value;}
        assertTrue("Silent Chinese audio",energy/samples.length>0.000001);
        File file=new File(out,voice+".wav");assertTrue("WAV save failed",audio.save(file.getAbsolutePath()));
        report.put(new JSONObject().put("voice",voice).put("sid",sid).put("sample_rate",rate)
                .put("samples",samples.length).put("duration_ms",samples.length*1000L/rate)
                .put("load_ms",loadMs).put("generate_ms",elapsed).put("mean_square",energy/samples.length));
        return audio;
    }

    /** Optional speaker-output smoke; cancellation suppresses the old result and allows reuse. */
    public void testAudioTrackCancellationAfterPlaybackStarts() throws Exception {
        if(!enabled("allow_offline_tts_playback_test"))return;
        Context context=getInstrumentation().getTargetContext();
        // Full offline preflight prevents the driver below from initiating a download.
        new TtsModel(context,"melo-zh").prepare(new VoiceModel.CancelToken(),message->{},false);
        CountDownLatch ready=new CountDownLatch(1),playing=new CountDownLatch(1),completed=new CountDownLatch(1);
        AtomicReference<String> error=new AtomicReference<>();
        AtomicInteger done=new AtomicInteger(),staleDone=new AtomicInteger();
        OfflineReplyVoice voice=new OfflineReplyVoice(context,"melo-zh",new ReplySpeech.Callbacks(){
            public void ready(String message){if(message!=null)error.set(message);ready.countDown();}
            public void started(String id){if("cancel-fixture".equals(id))playing.countDown();}
            public void done(String id){
                if("completed-fixture".equals(id)){done.incrementAndGet();completed.countDown();}
                else staleDone.incrementAndGet();
            }
            public void failed(String id,String message){error.set(message);playing.countDown();completed.countDown();}
        });
        try {
            assertTrue("TTS load timed out",ready.await(120,TimeUnit.SECONDS));assertNull(error.get());
            assertTrue(voice.speak(TEXT+TEXT,"cancel-fixture"));
            assertTrue("TTS playback timed out",playing.await(120,TimeUnit.SECONDS));assertNull(error.get());
            long started=SystemClock.elapsedRealtime();voice.stop();
            assertTrue("Stop blocked on JNI or playback",SystemClock.elapsedRealtime()-started<1000);
            Thread.sleep(600);assertEquals(0,staleDone.get());assertEquals(0,done.get());assertNull(error.get());
            assertTrue("Canceled driver could not be reused",voice.speak("你好。","completed-fixture"));
            assertTrue("Resumed playback did not finish",completed.await(20,TimeUnit.SECONDS));
            assertNull(error.get());assertEquals(1,done.get());assertEquals(0,staleDone.get());
            Thread.sleep(200);assertEquals("Duplicate completion callback",1,done.get());assertEquals(0,staleDone.get());
        } finally {voice.close();}
    }

    public void testStreamingLongReplyReplacementCompletesOnlyTheNewWholeReply() throws Exception {
        if(!enabled("allow_offline_tts_playback_test"))return;
        Context context=getInstrumentation().getTargetContext();
        new TtsModel(context,"melo-zh").prepare(new VoiceModel.CancelToken(),message->{},false);
        CountDownLatch ready=new CountDownLatch(1),oldStarted=new CountDownLatch(1),complete=new CountDownLatch(1);
        AtomicReference<String> error=new AtomicReference<>();
        AtomicInteger staleDone=new AtomicInteger(),newDone=new AtomicInteger(),newStarts=new AtomicInteger();
        AtomicLong startedAt=new AtomicLong(),doneAt=new AtomicLong();
        OfflineReplyVoice voice=new OfflineReplyVoice(context,"melo-zh",new ReplySpeech.Callbacks(){
            public void ready(String message){if(message!=null)error.set(message);ready.countDown();}
            public void started(String id){
                if("old-long".equals(id))oldStarted.countDown();
                else if("replacement".equals(id)){newStarts.incrementAndGet();startedAt.set(SystemClock.elapsedRealtime());}
            }
            public void done(String id){
                if("replacement".equals(id)){newDone.incrementAndGet();doneAt.set(SystemClock.elapsedRealtime());complete.countDown();}
                else staleDone.incrementAndGet();
            }
            public void failed(String id,String message){error.set(message);oldStarted.countDown();complete.countDown();}
        });
        try {
            assertTrue(ready.await(120,TimeUnit.SECONDS));assertNull(error.get());
            StringBuilder old=new StringBuilder();for(int i=0;i<12;i++)old.append(TEXT);
            assertTrue("Whole replies must be accepted",old.length()>60&&voice.speak(old.toString(),"old-long"));
            assertTrue("First PCM never started",oldStarted.await(120,TimeUnit.SECONDS));assertNull(error.get());
            long replacing=SystemClock.elapsedRealtime();
            assertTrue(voice.speak("你好。第二句也需要完整播放。","replacement"));
            assertTrue("Replacement blocked on synthesis",SystemClock.elapsedRealtime()-replacing<1000);
            assertTrue("Replacement did not finish",complete.await(60,TimeUnit.SECONDS));assertNull(error.get());
            assertEquals(0,staleDone.get());assertEquals(1,newStarts.get());assertEquals(1,newDone.get());
            assertTrue("Completion arrived after only the short first sentence",doneAt.get()-startedAt.get()>=1500);
            Thread.sleep(200);assertEquals(0,staleDone.get());assertEquals(1,newDone.get());
        } finally {voice.close();}
    }

    public void testFourSentencePlaybackStartsBeforeProducerFinishesAndKeepsProgress() throws Exception {
        if(!enabled("allow_offline_tts_playback_test"))return;
        Context context=getInstrumentation().getTargetContext();
        new TtsModel(context,"melo-zh").prepare(new VoiceModel.CancelToken(),message->{},false);
        String text="这是连续朗读测试的第一句话，请保持自然语速。"
                +"这是第二句话，它需要紧接着前面的声音继续播放。"
                +"第三句话用于检查合成和播放是否可以同时进行。"
                +"第四句话结束以后，整条回复才应该报告播放完成。";
        CountDownLatch ready=new CountDownLatch(1),complete=new CountDownLatch(1);
        AtomicReference<String> error=new AtomicReference<>();AtomicReference<OfflineReplyVoice> holder=new AtomicReference<>();
        AtomicInteger starts=new AtomicInteger(),done=new AtomicInteger(),progress=new AtomicInteger();
        AtomicLong firstAt=new AtomicLong(),doneAt=new AtomicLong(),lastProgress=new AtomicLong(),largestGap=new AtomicLong();
        AtomicBoolean producerPendingAtFirstPlayback=new AtomicBoolean();
        OfflineReplyVoice voice=new OfflineReplyVoice(context,"melo-zh",new ReplySpeech.Callbacks(){
            public void ready(String message){if(message!=null)error.set(message);ready.countDown();}
            public void started(String id){
                starts.incrementAndGet();firstAt.compareAndSet(0,SystemClock.elapsedRealtime());
                try {
                    // Observe this implementation invariant without adding a production test API:
                    // first playback must happen while further PCM is still being produced.
                    java.lang.reflect.Field current=OfflineReplyVoice.class.getDeclaredField("current");current.setAccessible(true);
                    Object utterance=current.get(holder.get());
                    java.lang.reflect.Field pcm=utterance.getClass().getDeclaredField("pcm");pcm.setAccessible(true);
                    producerPendingAtFirstPlayback.set(((ReplyAudioBuffer)pcm.get(utterance)).accepting());
                } catch(ReflectiveOperationException failure){error.set(failure.toString());}
            }
            public void progress(String id){
                long now=SystemClock.elapsedRealtime(),previous=lastProgress.getAndSet(now);
                if(previous!=0)largestGap.set(Math.max(largestGap.get(),now-previous));
                progress.incrementAndGet();
            }
            public void done(String id){done.incrementAndGet();doneAt.set(SystemClock.elapsedRealtime());complete.countDown();}
            public void failed(String id,String message){error.set(message);complete.countDown();}
        });
        holder.set(voice);
        try {
            assertTrue(ready.await(120,TimeUnit.SECONDS));assertNull(error.get());
            long requested=SystemClock.elapsedRealtime();assertTrue(voice.speak(text,"four-sentences"));
            assertTrue("Four-sentence reply did not complete",complete.await(120,TimeUnit.SECONDS));
            assertNull(error.get());assertEquals(1,starts.get());assertEquals(1,done.get());
            assertTrue("Playback waited for the complete reply to be synthesized",producerPendingAtFirstPlayback.get());
            assertTrue("Missing progress during a long reply",progress.get()>=3);
            assertTrue("Progress would exceed the idle watchdog",largestGap.get()<60000);
            assertTrue("Four sentences were truncated to the first",doneAt.get()-firstAt.get()>6000);
            JSONObject report=new JSONObject().put("status","passed").put("network_used",false).put("microphone_used",false)
                    .put("first_playback_ms",firstAt.get()-requested).put("whole_reply_ms",doneAt.get()-requested)
                    .put("progress_callbacks",progress.get()).put("largest_progress_gap_ms",largestGap.get())
                    .put("producer_pending_at_first_playback",producerPendingAtFirstPlayback.get()).put("done_callbacks",done.get());
            File out=new File(context.getExternalFilesDir(null),"tts-generated");assertTrue(out.isDirectory()||out.mkdirs());
            try(FileOutputStream file=new FileOutputStream(new File(out,"streaming-report.json"))) {
                file.write(report.toString(2).getBytes(StandardCharsets.UTF_8));
            }
            android.util.Log.i("CoyoteOfflineTtsStream",report.toString());
        } finally {voice.close();}
    }
}
