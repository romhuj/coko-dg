package org.coyote.mobile;

import android.content.Context;
import android.os.SystemClock;
import android.test.InstrumentationTestCase;
import android.test.InstrumentationTestRunner;
import com.k2fsa.sherpa.onnx.OfflineRecognizer;
import com.k2fsa.sherpa.onnx.OfflineStream;
import com.k2fsa.sherpa.onnx.Vad;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;

/** Real offline inference on a public Chinese fixture; no microphone, network, or model API. */
public final class OfflineVoiceModelTest extends InstrumentationTestCase {
    public void testChineseSpeechAndSilenceSegmentation() throws Exception {
        if (!"true".equals(((InstrumentationTestRunner)getInstrumentation()).getArguments()
                .getString("allow_voice_model_test","false"))) return;
        assertTrue("Use the dedicated emulator",android.os.Build.MODEL.contains("sdk_gphone")
                || android.os.Build.HARDWARE.contains("ranchu"));
        Context context=getInstrumentation().getTargetContext();
        VoiceModel models=new VoiceModel(context);
        VoiceModel.CancelToken token=new VoiceModel.CancelToken();
        File[] files={models.model(),models.tokens(),models.vad()};
        for(int i=0;i<files.length;i++) assertTrue("Stage verified public model files before running the test",
                VoiceModel.verified(files[i],VoiceModel.FILES[i],token));
        float[] speech=readWave(new File(context.getFilesDir(),"voice-fixture-zh.wav"));
        ContinuousVoiceRecognizer engine=new ContinuousVoiceRecognizer(context,new ContinuousVoiceRecognizer.Listener(){
            public void onState(String id,String status,int progress,String message){ }
            public void onSentence(String id,String utterance,String text){ fail("This test never starts capture"); }
        });
        OfflineRecognizer recognizer=null; Vad vad=null;
        long started=SystemClock.elapsedRealtime();
        try {
            recognizer=engine.createRecognizer(); vad=engine.createVad();
            float[] silence=new float[512];
            for(int i=0;i<100;i++) vad.acceptWaveform(silence);
            assertTrue("Silence must not produce a sentence",vad.empty());
            ArrayList<float[]> segments=new ArrayList<>();
            for(int repeat=0;repeat<2;repeat++) {
                float[] utterance=new float[speech.length+24000];
                System.arraycopy(speech,0,utterance,0,speech.length);
                for(int offset=0;offset<utterance.length;offset+=512) {
                    float[] window=Arrays.copyOfRange(utterance,offset,offset+512);
                    vad.acceptWaveform(window);
                    while(!vad.empty()) {segments.add(vad.front().getSamples());vad.pop();}
                }
            }
            assertTrue("Two spoken phrases separated by silence must produce separate sentences",segments.size()>=2);
            int validChinese=0;
            for(float[] segment:segments) {
                assertTrue(segment.length<=ContinuousVoiceRecognizer.MAX_SENTENCE_SAMPLES);
                assertTrue(ContinuousVoiceRecognizer.hasEnergy(segment));
                OfflineStream stream=recognizer.createStream();
                try {
                    stream.acceptWaveform(segment,16000);recognizer.decode(stream);
                    String text=ContinuousVoiceRecognizer.cleanText(recognizer.getResult(stream).getText());
                    if(text.codePoints().filter(c->Character.UnicodeScript.of(c)==Character.UnicodeScript.HAN).count()>=4) validChinese++;
                } finally {stream.release();}
            }
            assertTrue("Both phrases must be recognized as Chinese speech",validChinese>=2);
            assertFalse(ContinuousVoiceRecognizer.hasEnergy(new float[16000]));
            assertFalse(ContinuousVoiceRecognizer.hasWords(ContinuousVoiceRecognizer.cleanText("<|zh|><|NEUTRAL|><|Speech|>...")));
            JSONObject report=new JSONObject();
            report.put("status","passed");report.put("segments",segments.size());report.put("chinese_sentences",validChinese);
            report.put("elapsed_ms",SystemClock.elapsedRealtime()-started);report.put("microphone_used",false);
            try(FileOutputStream output=new FileOutputStream(new File(context.getExternalFilesDir(null),"offline-voice-model-test.json"))) {
                output.write(report.toString(2).getBytes(StandardCharsets.UTF_8));
            }
        } finally {if(vad!=null)vad.release();if(recognizer!=null)recognizer.release();engine.close();}
    }
    private static float[] readWave(File file) throws Exception {
        assertTrue("Stage the public Chinese WAV fixture first",file.isFile()&&file.length()<2_000_000);
        byte[] bytes=new byte[(int)file.length()];
        try(FileInputStream input=new FileInputStream(file)){int offset=0,count;while(offset<bytes.length&&(count=input.read(bytes,offset,bytes.length-offset))>0)offset+=count;}
        ByteBuffer buffer=ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        assertEquals("RIFF",new String(bytes,0,4,StandardCharsets.US_ASCII));
        assertEquals("WAVE",new String(bytes,8,4,StandardCharsets.US_ASCII));
        int at=12,format=0,channels=0,rate=0,bits=0;
        while(at+8<=bytes.length) {
            String kind=new String(bytes,at,4,StandardCharsets.US_ASCII);int size=buffer.getInt(at+4);at+=8;
            assertTrue(size>=0&&at+size<=bytes.length);
            if("fmt ".equals(kind)){format=buffer.getShort(at);channels=buffer.getShort(at+2);rate=buffer.getInt(at+4);bits=buffer.getShort(at+14);}
            if("data".equals(kind)){
                assertEquals(1,format);assertEquals(1,channels);assertEquals(16000,rate);assertEquals(16,bits);
                float[] samples=new float[size/2];for(int i=0;i<samples.length;i++)samples[i]=buffer.getShort(at+i*2)/32768f;return samples;
            }
            at+=size+(size%2);
        }
        throw new AssertionError("No PCM samples in WAV fixture");
    }
}
