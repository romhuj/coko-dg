package org.coyote.mobile;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicLong;

/** Owns one current reply; replacement and user speech invalidate all earlier audio callbacks. */
final class ReplySpeech implements AutoCloseable {
    interface Listener { void state(String session, String status, String utterance, String message); }
    interface Driver {
        int limit();
        boolean speak(String text, String id);
        void stop();
        void close();
    }
    interface Callbacks {
        default void preparing(String message) {}
        void ready(String error);
        void started(String id);
        default void progress(String id) {}
        void done(String id);
        void failed(String id, String error);
    }
    interface Factory { Driver create(Context context, String voiceId, Callbacks callbacks); }
    interface PlaybackGate { boolean begin(); void end(); }
    private static final CopyOnWriteArrayList<ReplySpeech> INSTANCES=new CopyOnWriteArrayList<>();
    private static final Handler MAIN=new Handler(Looper.getMainLooper());
    private static final AtomicLong PLAYBACK_SEQUENCE=new AtomicLong();
    private final Context context;
    private final Listener listener;
    private final Factory factory;
    private final PlaybackGate gate;
    private Driver driver;
    private String session, utterance, pieceId, driverVoice;
    private List<String> pieces=Collections.emptyList();
    private int position;
    private long generation, driverGeneration;
    private volatile long playbackToken;
    private boolean ready, closed, gated;
    private final Runnable timeout=() -> fail("朗读超时，已停止播放");
    private final Runnable initTimeout=() -> {fail("系统朗读准备超时，请检查文字转语音设置后重试");disposeDriver();};

    ReplySpeech(Context context, Listener listener) {
        this(context,listener,(app,voice,callback)->"system-default".equals(voice)
                ? new SystemDriver(app,callback) : new OfflineReplyVoice(app,voice,callback),null);
    }
    ReplySpeech(Context context, Listener listener, Factory factory, PlaybackGate gate) {
        this.context=context.getApplicationContext(); this.listener=listener; this.factory=factory;
        this.gate=gate==null ? new SystemGate(this.context,()->disable("其他音频已中断朗读")) : gate;
        INSTANCES.add(this);
    }
    static void stopActive(String reason) {
        onMain(()->{for(ReplySpeech speech:INSTANCES) speech.disable(reason);});
    }
    static long currentPlaybackToken() {
        for(ReplySpeech speech:INSTANCES) {long token=speech.playbackToken;if(token!=0)return token;}
        return 0;
    }
    static void interruptForUserSpeech(long token) {
        if(token==0)return;
        onMain(()->{for(ReplySpeech speech:INSTANCES) if(speech.playbackToken==token) {
            String interrupted=speech.utterance;
            speech.clearPlayback();
            speech.emit("interrupted",interrupted,"");
        }});
    }
    static boolean validVoice(String voice) {
        return "system-default".equals(voice)||"kokoro-zf_001".equals(voice)||"kokoro-zm_010".equals(voice)||"melo-zh".equals(voice);
    }
    void enable(String id) {enable(id,"system-default");}
    void enable(String id,String voice) {
        if(closed) return;
        if(!validVoice(voice))return;
        if(id.equals(session)&&voice.equals(driverVoice)) { emit(ready ? utterance==null ? "ready" : "speaking" : "preparing",utterance,""); return; }
        disable(""); session=id;
        if(!voice.equals(driverVoice))disposeDriver();
        if(driver!=null && ready) {emit("ready",null,"");return;}
        driverVoice=voice;
        emit("preparing",null,"正在准备回复朗读");
        long initializing=++driverGeneration;
        long initLimit="system-default".equals(voice)?15000:120000;
        MAIN.postDelayed(initTimeout,initLimit);
        Callbacks callbacks=new Callbacks() {
            public void preparing(String message) {MAIN.post(()->{
                if(closed||initializing!=driverGeneration||ready)return;
                MAIN.removeCallbacks(initTimeout);MAIN.postDelayed(initTimeout,initLimit);
                emit("preparing",null,message);
            });}
            public void ready(String error) { MAIN.post(()->{
                if(closed||initializing!=driverGeneration) return;
                MAIN.removeCallbacks(initTimeout);
                if(error!=null) {fail(error);disposeDriver();return;}
                ready=true; if(session!=null) emit("ready",null,"");
            }); }
            public void started(String id) {onMain(()->{if(matches(id))emit("speaking",utterance,"");});}
            public void progress(String id) {onMain(()->{if(matches(id)) {
                MAIN.removeCallbacks(timeout);MAIN.postDelayed(timeout,60000);
            }});}
            public void done(String id) {onMain(()->{
                if(!matches(id)) return;
                MAIN.removeCallbacks(timeout);
                position++;
                if(position<pieces.size()) speakNext();
                else {String completed=utterance;clearPlayback();emit("ready",completed,"");}
            });}
            public void failed(String id,String error) {onMain(()->{if(matches(id))fail(error);});}
        };
        try {driver=factory.create(context,voice,callbacks);}
        catch(RuntimeException failure) {fail("朗读引擎启动失败，请重新开启播放");disposeDriver();}
    }
    void speak(String id,String item,String text) {
        speak(id,item,text,false);
    }
    void speak(String id,String item,String text,boolean replace) {
        if(closed||session==null||!session.equals(id)) return;
        if(!ready||driver==null) {fail("朗读引擎尚未就绪，请重新开启");return;}
        if(utterance!=null&&utterance.equals(item))return;
        if(utterance!=null&&!replace) {fail("上一段朗读尚未结束，已暂停自动播放");return;}
        if(text==null||text.trim().isEmpty()||text.length()>16000) {fail("回复内容为空或过长，无法朗读");return;}
        // Keep the audio route and capture state stable while the newest reply replaces old PCM.
        if(utterance!=null)clearPlayback(false);
        int limit="system-default".equals(driverVoice)?Math.min(1800,driver.limit()):driver.limit();
        pieces=splitText(text.trim(),limit); position=0; utterance=item; generation++;
        playbackToken=PLAYBACK_SEQUENCE.incrementAndGet();
        if(gated) {speakNext();return;}
        boolean acquired;
        try {acquired=gate.begin();}
        catch(RuntimeException failure) {try{gate.end();}catch(RuntimeException ignored){}fail("无法开始音频播放，请重新开启");return;}
        if(!acquired) {fail("无法取得音频播放权限，请结束其他音频后重试");return;}
        gated=true;
        speakNext();
    }
    private void speakNext() {
        pieceId=generation+":"+position+":"+utterance;
        MAIN.removeCallbacks(timeout); MAIN.postDelayed(timeout,"system-default".equals(driverVoice)?240000:60000);
        emit("speaking",utterance,"");
        try {if(!driver.speak(pieces.get(position),pieceId))fail("系统朗读失败，请检查中文音色是否可用");}
        catch(RuntimeException failure) {fail("系统朗读中断，请重新开启播放");}
    }
    void stop() {String old=utterance;clearPlayback();if(session!=null)emit(ready?"ready":"preparing",old,"");}
    void disable(String message) {
        String old=session; session=null; clearPlayback();
        if(!ready)disposeDriver(); // Cancelling preparation stops pending model downloads as well.
        if(old!=null) listener.state(old,"off",null,message==null?"":message);
    }
    private boolean matches(String id) {return !closed&&session!=null&&pieceId!=null&&pieceId.equals(id);}
    private void clearPlayback() {
        clearPlayback(true);
    }
    private void clearPlayback(boolean releaseGate) {
        playbackToken=0;
        generation++;utterance=null;pieceId=null;pieces=Collections.emptyList();position=0;
        MAIN.removeCallbacks(timeout);
        if(driver!=null) {try{driver.stop();}catch(RuntimeException ignored){}}
        if(releaseGate&&gated) {gated=false;try{gate.end();}catch(RuntimeException ignored){}}
    }
    private void fail(String message) {
        String old=session, item=utterance; session=null;clearPlayback();
        if(!ready)disposeDriver();
        if(old!=null)listener.state(old,"error",item,message);
    }
    private void emit(String status,String item,String message) {if(session!=null)listener.state(session,status,item,message);}
    private void disposeDriver() {
        driverGeneration++;ready=false;MAIN.removeCallbacks(initTimeout);
        Driver old=driver;driver=null;if(old!=null){try{old.close();}catch(RuntimeException ignored){}}
    }
    static List<String> splitText(String text,int limit) {
        if(limit<32)throw new IllegalArgumentException("Invalid TTS limit");
        ArrayList<String> result=new ArrayList<>();
        for(int start=0;start<text.length();) {
            int end=Math.min(text.length(),start+limit);
            if(end<text.length()&&Character.isHighSurrogate(text.charAt(end-1)))end--;
            if(limit<=180) {
                // Offline synthesis completes a chunk before playing it: prefer a short
                // first sentence so a long reply does not delay the first audible words.
                for(int at=start+7;at<end;at++) if("。！？；\n.!?;".indexOf(text.charAt(at))>=0){end=at+1;break;}
            }
            else if(end<text.length()) for(int at=end-1;at>start+limit/2;at--) if("。！？；\n.!?;".indexOf(text.charAt(at))>=0){end=at+1;break;}
            result.add(text.substring(start,end));start=end;
        }
        return result;
    }
    private static void onMain(Runnable work) {if(Looper.myLooper()==Looper.getMainLooper())work.run();else MAIN.post(work);}
    @Override public void close() {if(closed)return;disable("");closed=true;disposeDriver();INSTANCES.remove(this);}

    private static final class SystemDriver implements Driver {
        private TextToSpeech tts;
        SystemDriver(Context context,Callbacks callback) {
            tts=new TextToSpeech(context,status -> MAIN.post(()->{
                if(tts==null)return;
                if(status!=TextToSpeech.SUCCESS){callback.ready("手机没有可用的系统朗读引擎，请在系统文字转语音设置中安装中文音色");return;}
                try {
                    Set<Voice> voices=tts.getVoices(); Voice selected=null;
                    if(voices!=null)selected=voices.stream().filter(v->"zh".equals(v.getLocale().getLanguage())&&!v.isNetworkConnectionRequired()
                            &&(v.getFeatures()==null||!v.getFeatures().contains(TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED)))
                            .sorted(Comparator.comparing((Voice v)->!"CN".equals(v.getLocale().getCountry())).thenComparing(Voice::getName)).findFirst().orElse(null);
                    if(selected==null||tts.setVoice(selected)!=TextToSpeech.SUCCESS){callback.ready("手机尚未安装可用的离线中文音色，请在系统文字转语音设置中安装");return;}
                    tts.setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
                    tts.setOnUtteranceProgressListener(new UtteranceProgressListener(){
                        @Override public void onStart(String id){callback.started(id);}
                        @Override public void onDone(String id){callback.done(id);}
                        @Override public void onError(String id){callback.failed(id,"系统朗读失败，请重新开启播放");}
                        @Override public void onError(String id,int code){onError(id);}
                    });
                    callback.ready(null);
                } catch(RuntimeException failure){callback.ready("无法初始化中文朗读，请检查系统文字转语音设置");}
            }));
        }
        public int limit(){return TextToSpeech.getMaxSpeechInputLength();}
        public boolean speak(String text,String id){return tts!=null&&tts.speak(text,TextToSpeech.QUEUE_FLUSH,null,id)==TextToSpeech.SUCCESS;}
        public void stop(){if(tts!=null)tts.stop();}
        public void close(){if(tts!=null){tts.stop();tts.shutdown();tts=null;}}
    }
    private static final class SystemGate implements PlaybackGate {
        private final Context context;
        private final AudioManager audio;
        private final Runnable lost;
        private AudioManager.OnAudioFocusChangeListener focus;
        private AudioFocusRequest request;
        private long focusEpoch;
        private boolean held;
        SystemGate(Context context,Runnable lost) {
            this.context=context;
            audio=context.getSystemService(AudioManager.class);
            this.lost=lost;
        }
        @Override public boolean begin() {
            if(audio==null)return false;
            long expected=++focusEpoch;
            focus=change->{if(change==AudioManager.AUDIOFOCUS_LOSS||change==AudioManager.AUDIOFOCUS_LOSS_TRANSIENT)
                onMain(()->{if(held&&expected==focusEpoch)lost.run();});};
            int result;
            if(Build.VERSION.SDK_INT>=26) {
                request=new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                        .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                        .setOnAudioFocusChangeListener(focus,MAIN).build();
                result=audio.requestAudioFocus(request);
            } else result=audio.requestAudioFocus(focus,AudioManager.STREAM_VOICE_CALL,AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK);
            if(result!=AudioManager.AUDIOFOCUS_REQUEST_GRANTED){focusEpoch++;request=null;focus=null;return false;}
            held=true;
            if(!CommunicationAudioSession.acquire(context,this)){end();return false;}
            VoiceCaptureService.setPlaybackActive(true);return true;
        }
        @Override public void end() {
            focusEpoch++;held=false;
            VoiceCaptureService.setPlaybackActive(false);
            CommunicationAudioSession.release(this);
            if(audio==null)return;
            if(Build.VERSION.SDK_INT>=26&&request!=null)audio.abandonAudioFocusRequest(request);else if(focus!=null)audio.abandonAudioFocus(focus);
            request=null;focus=null;
        }
    }
}
