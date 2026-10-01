package org.coyote.mobile;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioRouting;
import android.media.AudioTrack;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import com.k2fsa.sherpa.onnx.OfflineTts;
import java.io.IOException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executor;
import java.util.concurrent.Executors;

/** Downloads/loads/synthesizes off the UI thread. Stop never releases an in-flight JNI model. */
final class OfflineReplyVoice implements ReplySpeech.Driver {
    // A process-wide serial worker also protects the phonemizer's global native state when
    // switching voices. Old release is queued before a newly selected model is initialized.
    private static final ExecutorService WORKER=Executors.newSingleThreadExecutor(task->{
        Thread thread=new Thread(task,"CoyoteOfflineReplyVoice"); thread.setDaemon(true); return thread;
    });
    private static final ExecutorService PLAYER=Executors.newSingleThreadExecutor(task->{
        Thread thread=new Thread(task,"CoyoteReplyAudioPlayer");thread.setDaemon(true);return thread;
    });
    private static final Handler MAIN=new Handler(Looper.getMainLooper());
    private final Object lock=new Object();
    private final ReplySpeech.Callbacks callbacks;
    private final Executor worker;
    private final VoiceModel.CancelToken preparing=new VoiceModel.CancelToken();
    private OfflineTts tts; // Accessed only by WORKER, including release.
    private AudioTrack track; // Published and stopped under lock; writes never block under lock.
    private Utterance trackUtterance;
    private int speakerId,sampleRate;
    private long generation;
    private boolean closed,ready;
    private Utterance current;

    private static final class Utterance {
        final String id,text;
        final long generation;
        final int rate;
        final ReplyAudioBuffer pcm;
        long lastProgress;
        Utterance(String id,String text,long generation,int rate) {
            this.id=id;this.text=text;this.generation=generation;this.rate=rate;
            pcm=new ReplyAudioBuffer(rate*3,rate/4);
        }
    }

    OfflineReplyVoice(Context context,String voiceId,ReplySpeech.Callbacks callbacks) {
        this(context,voiceId,callbacks,WORKER);
    }

    /** Controllable executor lets cancellation-before-initialization be tested without I/O. */
    OfflineReplyVoice(Context context,String voiceId,ReplySpeech.Callbacks callbacks,Executor worker) {
        this.callbacks=callbacks; this.worker=worker;
        Context app=context.getApplicationContext();
        worker.execute(()->initialize(app,voiceId));
    }

    private void initialize(Context context,String voiceId) {
        try {
            preparing.check();
            Process.setThreadPriority(Process.THREAD_PRIORITY_BACKGROUND);
            TtsModel model=new TtsModel(context,voiceId);
            model.prepare(preparing,message->{if(!preparing.isCancelled())callbacks.preparing(message);},true);
            preparing.check();
            callbacks.preparing("正在加载离线音色，首次加载可能稍慢");
            OfflineTts loaded=model.create();
            if(preparing.isCancelled()) { loaded.release(); return; }
            tts=loaded; speakerId=model.speakerId;sampleRate=loaded.sampleRate();
            if(sampleRate<8000||sampleRate>96000)throw new IOException("音色采样率无效");
            boolean abandoned;
            synchronized(lock) {
                abandoned=closed;
                if(!abandoned)ready=true;
            }
            if(abandoned) {tts.release();tts=null;return;}
            Process.setThreadPriority(Process.THREAD_PRIORITY_DEFAULT);
            callbacks.ready(null);
        } catch(IOException failure) {
            if(!preparing.isCancelled()) callbacks.ready(safeError(failure));
        } catch(RuntimeException | LinkageError failure) {
            if(!preparing.isCancelled()) callbacks.ready("离线音色加载失败，请重新选择音色后重试");
        } catch(OutOfMemoryError failure) {
            if(!preparing.isCancelled()) callbacks.ready("内存不足，建议选择资源较小的 Melo 中文女声");
        } finally {Process.setThreadPriority(Process.THREAD_PRIORITY_DEFAULT);}
    }

    @Override public int limit() { return 16000; }

    @Override public boolean speak(String text,String id) {
        final Utterance utterance;
        synchronized(lock) {
            if(closed||!ready||text==null||text.isEmpty()||text.length()>limit()) return false;
            stopLocked();
            utterance=new Utterance(id,text,generation,sampleRate);current=utterance;
        }
        reportCurrentRoute();
        // Consumption is independent: the next native batch is synthesized while PCM from
        // the previous batch is playing. Bounded PCM keeps prefetch memory fixed.
        PLAYER.execute(()->play(utterance));
        worker.execute(()->produce(utterance));
        return true;
    }

    private boolean active(Utterance utterance) {
        synchronized(lock) {return !closed&&current==utterance&&generation==utterance.generation;}
    }

    private void produce(Utterance utterance) {
        try {
            if(!active(utterance)||tts==null)return;
            for(String piece:ReplyAudioBuffer.split(utterance.text)) {
                if(!active(utterance)||!utterance.pcm.accepting())return;
                // JNI requires the concrete invoke(float[]) -> Integer signature. Do not
                // replace this with an erased Java lambda (which aborts in sherpa 1.13.8).
                tts.generateWithCallback(piece,speakerId,1.0f,
                        new kotlin.jvm.functions.Function1<float[],Integer>() {
                            @Override public Integer invoke(float[] samples) {
                                if(!active(utterance))return 0;
                                try {
                                    progress(utterance);
                                    float[] audible=ReplyAudioBuffer.trimEdges(samples,utterance.rate);
                                    return utterance.pcm.put(audible)&&active(utterance)?1:0;
                                } catch(InterruptedException cancelled) {
                                    Thread.currentThread().interrupt();utterance.pcm.cancel();return 0;
                                }
                            }
                        });
                // GeneratedAudio repeats the callback PCM. Deliberately do not enqueue it.
            }
            if(active(utterance))utterance.pcm.finish();
        } catch(RuntimeException | LinkageError failure) {
            if(active(utterance))utterance.pcm.fail("离线语音合成失败，请重新开启后重试");
        } catch(OutOfMemoryError failure) {
            if(active(utterance))utterance.pcm.fail("内存不足，建议选择资源较小的 Melo 中文女声");
        } finally {if(!active(utterance))utterance.pcm.cancel();}
    }

    private void play(Utterance utterance) {
        AudioTrack output=null;
        try {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
            long written=0,lastData=SystemClock.elapsedRealtime();
            while(active(utterance)) {
                float[] samples=utterance.pcm.take(250);
                if(!active(utterance))return;
                if(utterance.pcm.error()!=null)throw new IOException(utterance.pcm.error());
                if(samples==null) {
                    if(utterance.pcm.drained())break;
                    if(SystemClock.elapsedRealtime()-lastData>120000)throw new IOException("离线语音合成超时");
                    continue;
                }
                lastData=SystemClock.elapsedRealtime();
                progress(utterance);
                if(output==null) {
                    output=createOutput(utterance.rate);
                    output.addOnRoutingChangedListener((AudioRouting.OnRoutingChangedListener)routing->reportCurrentRoute(),MAIN);
                    synchronized(lock) {
                        if(!active(utterance))return;
                        track=output;trackUtterance=utterance;output.play();callbacks.started(utterance.id);
                    }
                    reportCurrentRoute();
                }
                int offset=0;long lastWrite=SystemClock.elapsedRealtime();
                while(offset<samples.length) {
                    int count;
                    synchronized(lock) {
                        if(!active(utterance))return;
                        count=output.write(samples,offset,Math.min(2048,samples.length-offset),AudioTrack.WRITE_NON_BLOCKING);
                    }
                    if(count<0)throw new IOException("手机音频输出已中断");
                    if(count>0) {offset+=count;written+=count;lastWrite=SystemClock.elapsedRealtime();}
                    else {
                        if(SystemClock.elapsedRealtime()-lastWrite>10000)throw new IOException("音频播放超时，已停止");
                        Thread.sleep(5);
                    }
                }
            }
            if(!active(utterance))return;
            if(output==null||written==0)throw new IOException("这段内容未生成可播放的语音，请换一段文字");
            // One AudioTrack spans the whole reply. Only EOF plus the last played frame
            // completes it; waiting for the next synthesis chunk never emits done.
            long deadline=SystemClock.elapsedRealtime()+10000;
            while(active(utterance)&&Integer.toUnsignedLong(output.getPlaybackHeadPosition())<written) {
                if(SystemClock.elapsedRealtime()>deadline) throw new IOException("音频播放超时，已停止");
                Thread.sleep(5);
            }
            releaseOutput(output); output=null;
            synchronized(lock) {if(active(utterance))callbacks.done(utterance.id);}
        } catch(InterruptedException cancelled) {
            Thread.currentThread().interrupt();utterance.pcm.cancel();
        } catch(IOException | RuntimeException | LinkageError failure) {
            utterance.pcm.cancel();
            releaseOutput(output); output=null;
            synchronized(lock) {if(active(utterance))callbacks.failed(utterance.id,"离线朗读失败，请重新开启后重试");}
        } catch(OutOfMemoryError failure) {
            utterance.pcm.cancel();
            releaseOutput(output); output=null;
            synchronized(lock) {if(active(utterance))callbacks.failed(utterance.id,"内存不足，建议选择资源较小的 Melo 中文女声");}
        } finally { releaseOutput(output); }
    }

    private static AudioTrack createOutput(int rate) throws IOException {
        int minimum=AudioTrack.getMinBufferSize(rate,AudioFormat.CHANNEL_OUT_MONO,AudioFormat.ENCODING_PCM_FLOAT);
        if(minimum<=0)throw new IOException("手机音频输出暂不可用，请重试");
        AudioTrack output=new AudioTrack.Builder()
                .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(rate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).setEncoding(AudioFormat.ENCODING_PCM_FLOAT).build())
                .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(Math.max(minimum,rate/5*4)).build();
        if(output.getState()!=AudioTrack.STATE_INITIALIZED) {output.release();throw new IOException("手机音频输出初始化失败");}
        return output;
    }

    private void stopTrackLocked() {
        if(track!=null) {
            try { track.pause(); } catch(IllegalStateException ignored) { }
            try { track.flush(); } catch(IllegalStateException ignored) { }
        }
    }
    private void releaseOutput(AudioTrack output) {
        if(output==null) return;
        synchronized(lock) {
            if(track==output) {track=null;trackUtterance=null;}
            try { output.pause(); } catch(IllegalStateException ignored) { }
            try { output.flush(); } catch(IllegalStateException ignored) { }
            output.release();
        }
        reportCurrentRoute();
    }

    private void progress(Utterance utterance) {
        synchronized(lock) {
            long now=SystemClock.elapsedRealtime();
            if(active(utterance)&&now-utterance.lastProgress>=1000) {
                utterance.lastProgress=now;callbacks.progress(utterance.id);
            }
        }
    }

    /** Evaluate the current route on main, rather than posting a potentially stale device. */
    private void reportCurrentRoute() {
        MAIN.post(()->{
            AudioDeviceInfo device=null;
            synchronized(lock) {
                if(track!=null&&trackUtterance==current&&current!=null&&active(current)) {
                    try {device=track.getRoutedDevice();}catch(IllegalStateException ignored) { }
                }
            }
            // The manager may notify microphone listeners. Never hold the track lock here.
            CommunicationAudioSession.reportOutputDevice(this,device);
        });
    }

    private void stopLocked() {
        generation++;
        if(current!=null){current.pcm.cancel();current=null;}
        stopTrackLocked();
    }
    @Override public void stop() { synchronized(lock) {stopLocked();}reportCurrentRoute(); }

    @Override public void close() {
        synchronized(lock) {
            if(closed)return;
            closed=true;ready=false;stopLocked();
        }
        reportCurrentRoute();
        preparing.cancel();
        worker.execute(()->{if(tts!=null){tts.release();tts=null;}});
    }

    private static String safeError(IOException failure) {
        String message=failure.getMessage();
        if(message!=null && (message.startsWith("离线音色")||message.startsWith("无法")
                ||message.startsWith("存储空间")||message.startsWith("音色"))) return message;
        return "离线音色下载中断，请检查网络后重试；已下载内容会保留";
    }
}
