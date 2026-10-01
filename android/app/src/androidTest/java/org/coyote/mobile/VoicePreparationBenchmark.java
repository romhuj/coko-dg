package org.coyote.mobile;

import android.content.Context;
import android.os.Build;
import android.os.SystemClock;
import android.test.InstrumentationTestCase;
import android.test.InstrumentationTestRunner;
import com.k2fsa.sherpa.onnx.OfflineRecognizer;
import com.k2fsa.sherpa.onnx.Vad;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;

/** Explicit opt-in benchmark on a phone or emulator. No audio capture, inference or network. */
@SuppressWarnings("deprecation")
public final class VoicePreparationBenchmark extends InstrumentationTestCase {
    public void testModelCheckAndJniLoadSeparately() throws Exception {
        if(!"true".equals(((InstrumentationTestRunner)getInstrumentation()).getArguments()
                .getString("allow_voice_benchmark","false"))) return;
        Context context=getInstrumentation().getTargetContext();
        VoiceModel model=new VoiceModel(context);
        File[] files={model.model(),model.tokens(),model.vad()};
        for(File file:files) assertTrue("Install the public offline model first; this benchmark never downloads",file.isFile());
        VoiceModel.CancelToken token=new VoiceModel.CancelToken();
        JSONObject report=new JSONObject();
        report.put("model_version",VoiceModel.MODEL_VERSION); report.put("sdk",Build.VERSION.SDK_INT);
        report.put("device",Build.MODEL); report.put("microphone_used",false); report.put("network_used",false);
        long start=SystemClock.elapsedRealtime();
        for(int i=0;i<files.length;i++) assertTrue("Pinned model SHA must match",VoiceModel.verified(files[i],VoiceModel.FILES[i],token));
        report.put("forced_full_hash_ms",SystemClock.elapsedRealtime()-start);
        report.put("initial_preflight",model.preverifyCached(token).json());
        VoiceModel.PreparationStats cached=new VoiceModel(context).preverifyCached(token);
        report.put("cached_preflight",cached.json());
        if(Build.VERSION.SDK_INT>=27) { assertEquals(3,cached.cacheHits); assertEquals(0L,cached.hashedBytes); }
        ContinuousVoiceRecognizer engine=new ContinuousVoiceRecognizer(context,new ContinuousVoiceRecognizer.Listener(){
            public void onState(String id,String state,int progress,String message) { fail("Benchmark never starts capture"); }
            public void onSentence(String id,String utterance,String text) { fail("Benchmark never recognizes speech"); }
        });
        OfflineRecognizer recognizer=null; Vad vad=null;
        try {
            start=SystemClock.elapsedRealtime(); recognizer=engine.createRecognizer();
            report.put("recognizer_init_ms",SystemClock.elapsedRealtime()-start);
            start=SystemClock.elapsedRealtime(); vad=engine.createVad();
            report.put("vad_init_ms",SystemClock.elapsedRealtime()-start);
        } finally {
            if(vad!=null) vad.release(); if(recognizer!=null) recognizer.release(); engine.close();
        }
        report.put("status","passed");
        try(FileOutputStream output=new FileOutputStream(new File(context.getExternalFilesDir(null),"voice-preparation-benchmark.json"))) {
            output.write(report.toString(2).getBytes(StandardCharsets.UTF_8));
        }
        android.util.Log.i("CoyoteVoiceBenchmark",report.toString());
    }
}
