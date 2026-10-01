package org.coyote.mobile;

import android.content.Context;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.NoiseSuppressor;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.util.Log;
import com.k2fsa.sherpa.onnx.FeatureConfig;
import com.k2fsa.sherpa.onnx.OfflineModelConfig;
import com.k2fsa.sherpa.onnx.OfflineRecognizer;
import com.k2fsa.sherpa.onnx.OfflineRecognizerConfig;
import com.k2fsa.sherpa.onnx.OfflineSenseVoiceModelConfig;
import com.k2fsa.sherpa.onnx.OfflineStream;
import com.k2fsa.sherpa.onnx.SileroVadModelConfig;
import com.k2fsa.sherpa.onnx.Vad;
import com.k2fsa.sherpa.onnx.VadModelConfig;
import java.io.IOException;
import java.util.Arrays;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Continuous local microphone capture with Silero segmentation and offline SenseVoice ASR. */
public final class ContinuousVoiceRecognizer implements AutoCloseable {
    public interface Listener {
        void onState(String sessionId, String status, int progress, String message);
        void onSentence(String sessionId, String utteranceId, String text);
        default void onLevel(String sessionId, float level) { }
        default void onPreview(String sessionId, String text) { }
        default void onDuplex(String sessionId, boolean enabled) { }
        default void onSpeechStart(String sessionId) { }
    }

    static final int SAMPLE_RATE=16000, VAD_WINDOW=512, MAX_SENTENCE_SAMPLES=16 * SAMPLE_RATE;
    private static final int MAX_PENDING_SENTENCES=3;
    private final Object lock=new Object();
    private final Listener listener;
    private final VoiceModel model;
    private final ThreadPoolExecutor worker;
    private final PlaybackGate playback=new PlaybackGate();
    private static final AtomicBoolean PREVERIFY_RUNNING=new AtomicBoolean();
    private Session current;
    private boolean closed, playbackActive, duplex;

    private static final class Session {
        final String id;
        final VoiceModel.CancelToken token=new VoiceModel.CancelToken();
        final ArrayBlockingQueue<Utterance> sentences=new ArrayBlockingQueue<>(MAX_PENDING_SENTENCES);
        final Object recorderLock=new Object();
        AudioRecord recorder;
        AcousticEchoCanceler aec;
        NoiseSuppressor suppressor;
        Thread capture;
        PreviewRequest preview;
        long previewSegment, previewAfter;
        boolean speechActive;
        Session(String id) { this.id=id; }
    }

    private static final class Utterance {
        final float[] samples;
        final long generation;
        final long segment;
        Utterance(float[] samples,long generation,long segment) { this.samples=samples; this.generation=generation; this.segment=segment; }
    }

    private static final class PreviewRequest {
        final float[] samples;
        final long generation, segment;
        PreviewRequest(float[] samples,long generation,long segment) { this.samples=samples;this.generation=generation;this.segment=segment; }
    }

    /** Capture-thread-only rolling window. It contains no microphone or JNI ownership. */
    static final class PreviewBuffer {
        static final int MAX_SAMPLES=4*SAMPLE_RATE, MIN_SAMPLES=4*SAMPLE_RATE/5, PRE_ROLL=2*SAMPLE_RATE/5;
        private final float[] ring=new float[MAX_SAMPLES];
        private int position, count, speechSamples;
        private long segment, lastSnapshot=-1000;
        private boolean speaking;
        static final class Frame {
            final long segment;
            final boolean speaking, changed;
            final float[] snapshot;
            Frame(long segment,boolean speaking,boolean changed,float[] snapshot) {
                this.segment=segment;this.speaking=speaking;this.changed=changed;this.snapshot=snapshot;
            }
        }
        Frame offer(float[] samples,boolean speech,long now) {
            for(float value:samples) { ring[position]=value;position=(position+1)%ring.length;count=Math.min(count+1,ring.length); }
            boolean changed=speaking!=speech;
            if(changed) {
                speaking=speech;
                if(speech) { segment++;speechSamples=0;lastSnapshot=now-1000; }
            }
            float[] snapshot=null;
            if(speech) {
                speechSamples=Math.min(MAX_SAMPLES,speechSamples+samples.length);
                if(speechSamples>=MIN_SAMPLES && now-lastSnapshot>=1000) {
                    int length=Math.min(count,Math.min(MAX_SAMPLES,speechSamples+PRE_ROLL));
                    snapshot=new float[length];
                    int first=(position-length+ring.length)%ring.length;
                    int chunk=Math.min(length,ring.length-first);
                    System.arraycopy(ring,first,snapshot,0,chunk);
                    System.arraycopy(ring,0,snapshot,chunk,length-chunk);
                    lastSnapshot=now;
                }
            }
            return new Frame(segment,speaking,changed,snapshot);
        }
        void reset() { position=count=speechSamples=0;speaking=false;segment++;lastSnapshot=-1000; }
    }

    static final class LevelMeter {
        private float smoothed;
        private long lastReport=-1000;
        float offer(float[] samples,long now) {
            double sum=0;
            for(float value:samples) sum+=(double)value*value;
            float level=samples.length==0 ? 0 : (float)Math.min(1,Math.sqrt(sum/samples.length)*8);
            float weight=level>smoothed ? .6f : .25f;
            smoothed+=weight*(level-smoothed);
            if(now-lastReport<100) return -1;
            lastReport=now;
            return smoothed<.005f ? 0 : smoothed;
        }
        void reset() { smoothed=0;lastReport=-1000; }
    }

    /** Independent of the platform/VAD warm-up: require 250 ms of consecutive detected speech. */
    static final class SpeechStartGate {
        private int samples;
        private boolean emitted;
        boolean offer(boolean speech,int count) {
            if(!speech) {reset();return false;}
            samples=Math.min(SAMPLE_RATE,samples+Math.max(0,count));
            if(emitted || samples<SAMPLE_RATE/4) return false;
            emitted=true;return true;
        }
        void reset() {samples=0;emitted=false;}
    }

    /** Kept separate from JNI so playback cancellation and the tail window can be tested. */
    static final class PlaybackGate {
        private long generation, discardUntil;
        private boolean suspended;
        synchronized boolean set(boolean value,long now) {
            if(suspended==value) return false;
            suspended=value; generation++;
            discardUntil=value ? Long.MAX_VALUE : now+350;
            return true;
        }
        synchronized long generation() { return generation; }
        synchronized boolean accepts(long expected,long now) {
            return expected==generation && !suspended && now>=discardUntil;
        }
        synchronized boolean suspended() { return suspended; }
        synchronized boolean playback(boolean playing,boolean duplex,long now) {return set(playing && !duplex,now);}
    }

    /** A low-priority preflight of existing private files only; no network, JNI or microphone. */
    public static void preverifyCachedModels(Context context) {
        if(!PREVERIFY_RUNNING.compareAndSet(false,true)) return;
        Context app=context.getApplicationContext();
        Thread thread=new Thread(()->{
            try {
                Process.setThreadPriority(Process.THREAD_PRIORITY_BACKGROUND);
                VoiceModel.PreparationStats stats=new VoiceModel(app).preverifyCached(new VoiceModel.CancelToken());
                Log.i("CoyoteVoiceTiming","cached_preflight="+stats.json());
            } catch(IOException | RuntimeException error) {
                // A subsequent explicit start performs the normal checked preparation again.
                Log.i("CoyoteVoiceTiming","cached_preflight_unavailable");
            } finally { PREVERIFY_RUNNING.set(false); }
        },"CoyoteVoicePreverify");
        thread.setDaemon(true); thread.start();
    }

    public ContinuousVoiceRecognizer(Context context, Listener listener) {
        this.listener=listener;
        model=new VoiceModel(context.getApplicationContext());
        worker=new ThreadPoolExecutor(1, 1, 0, TimeUnit.MILLISECONDS, new ArrayBlockingQueue<>(1), task -> {
            Thread thread=new Thread(task, "CoyoteVoiceRecognizer"); thread.setDaemon(true); return thread;
        });
    }

    /** MainActivity grants RECORD_AUDIO before this call. Repeating an active id is idempotent. */
    public void start(String sessionId) {
        if (sessionId == null || sessionId.isEmpty() || sessionId.length() > 160)
            throw new IllegalArgumentException("Invalid voice session id");
        synchronized (lock) {
            if (closed) return;
            if (current != null && current.id.equals(sessionId) && !current.token.isCancelled()) return;
            cancelLocked();
            duplex=false;
            playback.set(playbackActive,SystemClock.elapsedRealtime());
            Session session=new Session(sessionId); current=session;
            try { listener.onDuplex(session.id,false); } catch(RuntimeException ignored) { }
            state(session, "preparing", 0, "正在准备离线语音识别");
            try { worker.execute(() -> runSession(session)); }
            catch (RejectedExecutionException error) { fail(session, "语音引擎未能启动，请重试"); }
        }
    }

    /** Stops the microphone promptly and invalidates queued/in-flight recognition results. */
    public void stop() {
        Session stopped;
        synchronized (lock) { stopped=current; cancelLocked(); }
        if (stopped != null) notifyState(stopped.id, "off", 0, "语音输入已关闭");
    }

    /** Playback is independent of capture when controlled AEC or actual headphones isolate it. */
    public void setPlaybackActive(boolean playing) {
        synchronized(lock) {
            if(closed) return;
            playbackActive=playing;
            applyPlaybackLocked();
        }
    }

    /** Compatibility for existing callers; capability still decides whether capture pauses. */
    public void suspendForPlayback(boolean playing) { setPlaybackActive(playing); }

    static boolean supportsDuplex(boolean aecEnabled,boolean aecControlled,int actualOutputType) {
        return (aecEnabled && aecControlled) || CommunicationAudioSession.isHeadset(actualOutputType);
    }

    /** A route/effect change never requires reloading the model or resetting a full-duplex segment. */
    public void refreshDuplex() {
        Session session;
        synchronized(lock) {session=current;}
        if(session==null) return;
        boolean enabled=false, controlled=false, recording=false;
        synchronized(session.recorderLock) {
            try {
                recording=session.recorder!=null && session.recorder.getRecordingState()==AudioRecord.RECORDSTATE_RECORDING;
                if(session.aec!=null) {enabled=session.aec.getEnabled();controlled=session.aec.hasControl();}
            } catch(RuntimeException ignored) { }
        }
        boolean next=recording && supportsDuplex(enabled,controlled,CommunicationAudioSession.outputDeviceType());
        synchronized(lock) {
            if(!active(session) || duplex==next) return;
            duplex=next;
            // Notify before live callbacks so the service can update its matching fallback gate.
            try {listener.onDuplex(session.id,next);} catch(RuntimeException ignored) { }
            applyPlaybackLocked();
            if(session.capture!=null) state(session,"listening",100,listeningMessage());
        }
        Log.i("CoyoteVoiceTiming","duplex="+next+" aec_enabled="+enabled+" aec_control="+controlled
                +" output_type="+CommunicationAudioSession.outputDeviceType());
    }

    private void applyPlaybackLocked() {
        if(!playback.playback(playbackActive,duplex,SystemClock.elapsedRealtime())) return;
        Session session=current;
        if(session!=null) {
            session.sentences.clear();session.preview=null;session.speechActive=false;session.previewSegment++;
            clearLive(session);
            if(session.capture!=null) state(session,"listening",100,listeningMessage());
        }
    }

    private String listeningMessage() {
        return playback.suspended() ? "当前音频路径不支持回声消除，朗读时暂停识别" : "正在聆听，停顿后自动发送";
    }

    @Override public void close() {
        synchronized (lock) {
            if (closed) return;
            closed=true; cancelLocked(); worker.shutdownNow();
        }
    }

    private void cancelLocked() {
        Session session=current; current=null;
        worker.getQueue().clear();
        if (session != null) {
            session.preview=null; session.speechActive=false;
            clearLive(session);
            session.token.cancel(); session.sentences.clear(); stopRecorder(session);
        }
    }

    private boolean active(Session session) {
        synchronized (lock) { return !closed && current == session && !session.token.isCancelled(); }
    }

    private void state(Session session, String status, int progress, String message) {
        synchronized (lock) {
            if("listening".equals(status) && playback.suspended()) message=listeningMessage();
            if (active(session)) notifyState(session.id, status, progress, message);
        }
    }

    private void notifyState(String id, String status, int progress, String message) {
        try { listener.onState(id, status, progress, message); }
        catch (RuntimeException ignored) { /* A detached UI must not terminate microphone cleanup. */ }
    }

    private void sentence(Session session, long generation, String text) {
        synchronized (lock) {
            if (!active(session) || !playback.accepts(generation,SystemClock.elapsedRealtime())) return;
            try { listener.onSentence(session.id, UUID.randomUUID().toString(), text); }
            catch (RuntimeException ignored) { }
        }
    }

    private void clearLive(Session session) {
        try { listener.onLevel(session.id,0); listener.onPreview(session.id,""); }
        catch(RuntimeException ignored) { }
    }

    private void level(Session session,long generation,float value) {
        synchronized(lock) {
            if(!active(session) || !playback.accepts(generation,SystemClock.elapsedRealtime())) return;
            try { listener.onLevel(session.id,value); } catch(RuntimeException ignored) { }
        }
    }

    private void preview(Session session,PreviewRequest request,String text) {
        synchronized(lock) {
            if(!previewCurrent(session,request)) return;
            try { listener.onPreview(session.id,text); } catch(RuntimeException ignored) { }
        }
    }

    private boolean previewCurrent(Session session,PreviewRequest request) {
        synchronized(lock) {
            return active(session) && session.speechActive && request.segment==session.previewSegment
                    && playback.accepts(request.generation,SystemClock.elapsedRealtime());
        }
    }

    private String decode(OfflineRecognizer recognizer,float[] samples) {
        OfflineStream stream=recognizer.createStream();
        try {
            stream.acceptWaveform(samples,SAMPLE_RATE);recognizer.decode(stream);
            return cleanText(recognizer.getResult(stream).getText());
        } finally { stream.release(); }
    }

    private void fail(Session session, String message) {
        synchronized (lock) {
            if (!active(session)) return;
            notifyState(session.id, "error", 0, message);
            if (current == session) current=null;
            session.token.cancel(); session.sentences.clear(); stopRecorder(session);
        }
    }

    private void runSession(Session session) {
        OfflineRecognizer recognizer=null;
        Vad vad=null;
        boolean captureOwnsVad=false;
        try {
            if (!active(session)) return;
            long preparationStarted=SystemClock.elapsedRealtime();
            VoiceModel.PreparationStats prepared=model.prepare(session.token, (progress, downloading) -> state(session,
                    downloading ? "downloading" : "preparing", progress,
                    downloading ? "正在下载离线语音模型（约240MB），可取消后继续下载" : "正在校验离线语音模型"));
            Log.i("CoyoteVoiceTiming","model_files="+prepared.json());
            if (!active(session)) return;
            state(session, "preparing", 100, "正在加载离线中文语音模型");
            long loading=SystemClock.elapsedRealtime();
            recognizer=createRecognizer();
            Log.i("CoyoteVoiceTiming","recognizer_init_ms="+(SystemClock.elapsedRealtime()-loading));
            if (!active(session)) return;
            loading=SystemClock.elapsedRealtime();
            vad=createVad();
            Log.i("CoyoteVoiceTiming","vad_init_ms="+(SystemClock.elapsedRealtime()-loading)
                    +" ready_ms="+(SystemClock.elapsedRealtime()-preparationStarted));
            if (!active(session)) return;
            int minimum=AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (minimum <= 0) throw new IOException("设备不支持16kHz麦克风录音");
            AudioRecord recorder=new AudioRecord.Builder()
                    .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(SAMPLE_RATE)
                            .setChannelMask(AudioFormat.CHANNEL_IN_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                    .setBufferSizeInBytes(Math.max(minimum * 2, SAMPLE_RATE * 2)).build();
            synchronized (session.recorderLock) { session.recorder=recorder; }
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) throw new IOException("麦克风初始化失败，请关闭占用麦克风的应用后重试");
            configureEffects(session,recorder);
            recorder.addOnRoutingChangedListener(router->refreshDuplex(),new Handler(Looper.getMainLooper()));
            // Cancellation and opening the microphone are one lifecycle step.
            // A stop/onPause cannot return and then have this generation start recording.
            synchronized (lock) {
                if (!active(session)) return;
                recorder.startRecording();
            }
            if (recorder.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IOException("麦克风未能开始录音，请重试");
            refreshDuplex();
            Vad captureVad=vad;
            session.capture=new Thread(() -> capture(session, recorder, captureVad), "CoyoteVoiceCapture");
            session.capture.setDaemon(true);
            session.capture.start(); captureOwnsVad=true;
            state(session, "listening", 100, listeningMessage());
            while (active(session)) {
                Utterance utterance=session.sentences.poll(100, TimeUnit.MILLISECONDS);
                if(utterance!=null) {
                    if(!active(session) || !playback.accepts(utterance.generation,SystemClock.elapsedRealtime())) continue;
                    synchronized(lock) {
                        if(session.preview!=null && session.preview.segment<=utterance.segment) session.preview=null;
                    }
                    state(session,"listening",100,"正在转写，同时继续聆听");
                    String text=decode(recognizer,utterance.samples);
                    if(hasWords(text)) sentence(session,utterance.generation,text);
                    state(session,"listening",100,listeningMessage());
                    continue;
                }
                PreviewRequest request=null;
                synchronized(lock) {
                    if(active(session) && session.sentences.isEmpty() && SystemClock.elapsedRealtime()>=session.previewAfter) {
                        request=session.preview; session.preview=null;
                    }
                }
                if(request==null || !previewCurrent(session,request) || !session.sentences.isEmpty()) continue;
                long previewStarted=SystemClock.elapsedRealtime();
                String text;
                try { text=decode(recognizer,request.samples); }
                catch(RuntimeException optionalPreviewFailure) {
                    synchronized(lock) { session.previewAfter=SystemClock.elapsedRealtime()+5000; }
                    Log.i("CoyoteVoiceTiming","preview_unavailable; final_recognition_retained");
                    continue;
                }
                long elapsed=SystemClock.elapsedRealtime()-previewStarted;
                synchronized(lock) {
                    // Leave at least as much time between previews as the last computation cost.
                    session.previewAfter=SystemClock.elapsedRealtime()+Math.max(700,elapsed);
                }
                if(hasWords(text)) preview(session,request,text);
            }
        } catch (SecurityException error) {
            fail(session, "需要麦克风权限才能开启语音输入");
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        } catch (IOException error) {
            if (active(session)) fail(session, safeDownloadError(error));
        } catch (OutOfMemoryError error) {
            fail(session, "设备内存不足，请关闭其他应用后重新开启语音");
        } catch (RuntimeException | LinkageError error) {
            fail(session, "离线语音引擎初始化或识别失败，请重新开启语音");
        } finally {
            session.token.cancel(); session.sentences.clear(); stopRecorder(session);
            Thread capture=session.capture;
            if (capture != null) {
                try { capture.join(2000); }
                catch (InterruptedException error) { Thread.currentThread().interrupt(); }
            }
            if (!captureOwnsVad) {
                if (vad != null) vad.release();
                releaseRecorder(session);
            }
            if (recognizer != null) recognizer.release();
        }
    }

    OfflineRecognizer createRecognizer() {
        OfflineSenseVoiceModelConfig sense=new OfflineSenseVoiceModelConfig();
        sense.setModel(model.model().getAbsolutePath()); sense.setLanguage("zh");
        sense.setUseInverseTextNormalization(true);
        OfflineModelConfig modelConfig=new OfflineModelConfig();
        modelConfig.setSenseVoice(sense); modelConfig.setTokens(model.tokens().getAbsolutePath());
        modelConfig.setNumThreads(Math.min(2, Math.max(1, Runtime.getRuntime().availableProcessors() / 2)));
        modelConfig.setProvider("cpu");
        FeatureConfig feature=new FeatureConfig(); feature.setSampleRate(SAMPLE_RATE); feature.setFeatureDim(80);
        OfflineRecognizerConfig config=new OfflineRecognizerConfig();
        config.setModelConfig(modelConfig); config.setFeatConfig(feature); config.setDecodingMethod("greedy_search");
        return new OfflineRecognizer(null, config);
    }

    Vad createVad() {
        SileroVadModelConfig silero=new SileroVadModelConfig();
        silero.setModel(model.vad().getAbsolutePath()); silero.setThreshold(0.5f);
        silero.setMinSilenceDuration(0.9f); silero.setMinSpeechDuration(0.25f);
        silero.setWindowSize(VAD_WINDOW); silero.setMaxSpeechDuration(15.0f);
        VadModelConfig config=new VadModelConfig(); config.setSileroVadModelConfig(silero);
        config.setSampleRate(SAMPLE_RATE); config.setNumThreads(1); config.setProvider("cpu");
        return new Vad(null, config);
    }

    private void capture(Session session, AudioRecord recorder, Vad vad) {
        short[] pcm=new short[VAD_WINDOW];
        int filled=0, emptyReads=0;
        long vadGeneration=-1;
        PreviewBuffer previews=new PreviewBuffer();
        LevelMeter meter=new LevelMeter();
        SpeechStartGate speechStart=new SpeechStartGate();
        try {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
            while (active(session)) {
                long readGeneration=playback.generation();
                boolean readAllowed=playback.accepts(readGeneration,SystemClock.elapsedRealtime());
                int count=recorder.read(pcm, filled, pcm.length-filled, AudioRecord.READ_BLOCKING);
                if (!active(session)) break;
                if (count < 0) throw new IOException("麦克风录音已中断，请重新开启语音");
                if (count == 0) {
                    if (++emptyReads >= 20) throw new IOException("麦克风暂不可用，请重新开启语音");
                    continue;
                }
                emptyReads=0; filled+=count;
                long generation=playback.generation();
                if(generation!=vadGeneration) { vad.reset(); previews.reset();meter.reset();speechStart.reset();filled=0;vadGeneration=generation; }
                // Continue draining AudioRecord while speaking; never feed those samples to VAD.
                if(!readAllowed || readGeneration!=generation || !playback.accepts(generation,SystemClock.elapsedRealtime())) {
                    filled=0; continue;
                }
                if (filled < pcm.length) continue;
                float[] samples=new float[VAD_WINDOW];
                for (int i=0; i<samples.length; i++) samples[i]=pcm[i] / 32768f;
                filled=0;
                long now=SystemClock.elapsedRealtime();
                float currentLevel=meter.offer(samples,now);
                if(currentLevel>=0) level(session,generation,currentLevel);
                vad.acceptWaveform(samples);
                PreviewBuffer.Frame previewFrame=previews.offer(samples,vad.isSpeechDetected(),now);
                boolean userStarted=speechStart.offer(previewFrame.speaking,samples.length);
                synchronized(lock) {
                    if(active(session) && playback.accepts(generation,SystemClock.elapsedRealtime())) {
                        if(previewFrame.changed) {
                            session.previewSegment=previewFrame.segment;session.speechActive=previewFrame.speaking;session.preview=null;
                            try { listener.onPreview(session.id,""); } catch(RuntimeException ignored) { }
                        }
                        if(userStarted && duplex) {
                            try {listener.onSpeechStart(session.id);} catch(RuntimeException ignored) { }
                        }
                        if(previewFrame.snapshot!=null) session.preview=new PreviewRequest(previewFrame.snapshot,generation,previewFrame.segment);
                    }
                }
                while (!vad.empty()) {
                    float[] segment=vad.front().getSamples();
                    vad.pop();
                    if (!active(session)) break;
                    if (segment.length < SAMPLE_RATE / 5 || !hasEnergy(segment)) continue;
                    // Silero normally caps at 15 seconds; guard the JNI output boundary too.
                    if (segment.length > MAX_SENTENCE_SAMPLES) segment=Arrays.copyOf(segment, MAX_SENTENCE_SAMPLES);
                    synchronized(lock) {
                        if(!active(session) || !playback.accepts(generation,SystemClock.elapsedRealtime())) break;
                        if (!session.sentences.offer(new Utterance(segment,generation,previewFrame.segment))) {
                            fail(session, "语音转写积压，尚未完成的语句未发送。请重新开启语音后重复");
                            break;
                        }
                    }
                }
            }
        } catch (IOException | RuntimeException error) {
            if (active(session)) fail(session, "麦克风录音已中断，请重新开启语音");
        } catch (LinkageError error) {
            fail(session, "语音检测引擎不可用，请重新开启语音");
        } catch (OutOfMemoryError error) {
            fail(session, "设备内存不足，请关闭其他应用后重新开启语音");
        } finally {
            releaseRecorder(session); vad.release();
        }
    }

    private void configureEffects(Session session,AudioRecord recorder) {
        synchronized(session.recorderLock) {
            try {
                if(AcousticEchoCanceler.isAvailable()) session.aec=AcousticEchoCanceler.create(recorder.getAudioSessionId());
                if(session.aec!=null) {
                    session.aec.setControlStatusListener((effect,control)->refreshDuplex());
                    session.aec.setEnableStatusListener((effect,enabled)->refreshDuplex());
                    if(session.aec.hasControl()) session.aec.setEnabled(true);
                }
            } catch(RuntimeException unavailable) {
                if(session.aec!=null) {session.aec.release();session.aec=null;}
            }
            try {
                if(NoiseSuppressor.isAvailable()) session.suppressor=NoiseSuppressor.create(recorder.getAudioSessionId());
                if(session.suppressor!=null && session.suppressor.hasControl()) session.suppressor.setEnabled(true);
            } catch(RuntimeException unavailable) {
                if(session.suppressor!=null) {session.suppressor.release();session.suppressor=null;}
            }
        }
    }

    private static void stopRecorder(Session session) {
        synchronized (session.recorderLock) {
            if (session.recorder != null) {
                try { session.recorder.stop(); }
                catch (IllegalStateException ignored) { }
            }
        }
    }
    private static void releaseRecorder(Session session) {
        synchronized (session.recorderLock) {
            if(session.aec!=null) {session.aec.release();session.aec=null;}
            if(session.suppressor!=null) {session.suppressor.release();session.suppressor=null;}
            if (session.recorder != null) {
                try { session.recorder.stop(); } catch (IllegalStateException ignored) { }
                session.recorder.release(); session.recorder=null;
            }
        }
    }
    static boolean hasEnergy(float[] samples) {
        double energy=0;
        for (float value : samples) energy+=(double)value * value;
        return samples.length > 0 && energy / samples.length > 0.00000025;
    }
    static String cleanText(String text) {
        return text == null ? "" : text.replaceAll("<\\|[^<>]*\\|>", "").trim();
    }
    static boolean hasWords(String text) {
        return text != null && text.codePoints().anyMatch(Character::isLetterOrDigit);
    }
    private static String safeDownloadError(IOException error) {
        String message=error.getMessage();
        if (message != null && (message.startsWith("语音模型") || message.startsWith("存储空间")
                || message.startsWith("设备不支持") || message.startsWith("麦克风") || message.startsWith("无法")))
            return message;
        return "语音模型下载中断，请检查网络后重试；已下载内容会保留";
    }
}
