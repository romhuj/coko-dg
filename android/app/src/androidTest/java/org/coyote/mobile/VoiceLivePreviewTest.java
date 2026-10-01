package org.coyote.mobile;

import android.content.Context;
import android.os.SystemClock;
import android.test.InstrumentationTestCase;
import android.test.InstrumentationTestRunner;
import com.k2fsa.sherpa.onnx.OfflineRecognizer;
import com.k2fsa.sherpa.onnx.OfflineStream;
import com.k2fsa.sherpa.onnx.Vad;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import org.json.JSONObject;

/** Meter/window checks plus explicitly enabled offline preview inference. Never opens a mic. */
@SuppressWarnings("deprecation")
public final class VoiceLivePreviewTest extends InstrumentationTestCase {
    public void testMeterIsBoundedSmoothedAndThrottled() {
        ContinuousVoiceRecognizer.LevelMeter meter=new ContinuousVoiceRecognizer.LevelMeter();
        float[] speech=new float[512];Arrays.fill(speech,.125f);
        float attack=meter.offer(speech,0);
        assertTrue(attack>0 && attack<1);
        assertEquals(-1f,meter.offer(speech,32));
        float peak=meter.offer(speech,128);assertTrue(peak>=attack && peak<=1);
        float decay=meter.offer(new float[512],256);assertTrue(decay<peak && decay>0);
        meter.reset();assertEquals(0f,meter.offer(new float[512],300));
    }

    public void testPreviewWindowsAreBoundedAndResetAtSentenceBoundaries() {
        ContinuousVoiceRecognizer.PreviewBuffer buffer=new ContinuousVoiceRecognizer.PreviewBuffer();
        float[] samples=new float[512];
        long previousSnapshot=-1000, firstSegment=-1;
        int snapshots=0;
        for(int i=0;i<400;i++) {
            ContinuousVoiceRecognizer.PreviewBuffer.Frame frame=buffer.offer(samples,true,i*32L);
            if(firstSegment<0) firstSegment=frame.segment;
            assertEquals(firstSegment,frame.segment);
            if(frame.snapshot!=null) {
                assertTrue(frame.snapshot.length>=12800 && frame.snapshot.length<=64000);
                assertTrue(i*32L-previousSnapshot>=1000);previousSnapshot=i*32L;snapshots++;
            }
        }
        assertTrue(snapshots>4);
        ContinuousVoiceRecognizer.PreviewBuffer.Frame end=buffer.offer(samples,false,13000);
        assertTrue(end.changed);assertFalse(end.speaking);assertNull(end.snapshot);
        ContinuousVoiceRecognizer.PreviewBuffer.Frame next=buffer.offer(samples,true,13032);
        assertTrue(next.segment>firstSegment);assertNull(next.snapshot);
        buffer.reset();
        assertTrue(buffer.offer(samples,true,14000).segment>next.segment);
    }

    public void testRealChinesePreviewPrecedesFinalBoundary() throws Exception {
        if(!"true".equals(((InstrumentationTestRunner)getInstrumentation()).getArguments()
                .getString("allow_voice_model_test","false"))) return;
        Context context=getInstrumentation().getTargetContext();
        VoiceModel model=new VoiceModel(context);
        File[] files={model.model(),model.tokens(),model.vad()};
        for(File file:files) assertTrue("Stage the fixed public ASR model first; no downloads in this test",file.isFile());
        model.preverifyCached(new VoiceModel.CancelToken());
        for(int i=0;i<files.length;i++) assertTrue(model.verifiedCached(files[i],VoiceModel.FILES[i],new VoiceModel.CancelToken()));
        float[] wav=readWave(new File(context.getFilesDir(),"voice-fixture-zh.wav"));
        ContinuousVoiceRecognizer engine=new ContinuousVoiceRecognizer(context,new ContinuousVoiceRecognizer.Listener(){
            public void onState(String id,String status,int progress,String message) { fail("No capture started"); }
            public void onSentence(String id,String utterance,String text) { fail("Previews must never submit chat sentences"); }
        });
        OfflineRecognizer recognizer=null;Vad vad=null;
        int previews=0, chinesePreviews=0, finalBoundaries=0;
        long firstChineseAt=-1, firstFinalAt=-1, maxPreviewMs=0;
        try {
            recognizer=engine.createRecognizer();vad=engine.createVad();
            ContinuousVoiceRecognizer.PreviewBuffer buffer=new ContinuousVoiceRecognizer.PreviewBuffer();
            float[] input=Arrays.copyOf(wav,wav.length+24000);
            for(int offset=0;offset<input.length;offset+=512) {
                float[] chunk=Arrays.copyOfRange(input,offset,offset+512);
                vad.acceptWaveform(chunk);
                ContinuousVoiceRecognizer.PreviewBuffer.Frame frame=buffer.offer(chunk,vad.isSpeechDetected(),offset*1000L/16000);
                if(frame.snapshot!=null && vad.empty()) {
                    long start=SystemClock.elapsedRealtime();
                    OfflineStream stream=recognizer.createStream();
                    try {
                        stream.acceptWaveform(frame.snapshot,16000);recognizer.decode(stream);
                        String text=ContinuousVoiceRecognizer.cleanText(recognizer.getResult(stream).getText());
                        if(text.codePoints().anyMatch(c->Character.UnicodeScript.of(c)==Character.UnicodeScript.HAN)) {
                            chinesePreviews++;if(firstChineseAt<0)firstChineseAt=offset*1000L/16000;
                        }
                    } finally {stream.release();}
                    maxPreviewMs=Math.max(maxPreviewMs,SystemClock.elapsedRealtime()-start);previews++;
                }
                while(!vad.empty()) {finalBoundaries++;if(firstFinalAt<0)firstFinalAt=offset*1000L/16000;vad.pop();}
            }
            assertTrue("Speech must produce genuine partial Chinese text before its final boundary",chinesePreviews>0);
            assertTrue(firstChineseAt>=0 && firstFinalAt>firstChineseAt);
            JSONObject report=new JSONObject();report.put("status","passed");report.put("microphone_used",false);
            report.put("preview_requests",previews);report.put("chinese_previews",chinesePreviews);
            report.put("first_preview_audio_ms",firstChineseAt);report.put("first_final_audio_ms",firstFinalAt);
            report.put("final_boundaries",finalBoundaries);report.put("max_preview_decode_ms",maxPreviewMs);
            try(FileOutputStream output=new FileOutputStream(new File(context.getExternalFilesDir(null),"voice-live-preview-test.json"))) {
                output.write(report.toString(2).getBytes(StandardCharsets.UTF_8));
            }
        } finally {if(vad!=null)vad.release();if(recognizer!=null)recognizer.release();engine.close();}
    }

    private static float[] readWave(File file) throws Exception {
        assertTrue(file.isFile() && file.length()<2_000_000);
        byte[] bytes=new byte[(int)file.length()];
        try(FileInputStream input=new FileInputStream(file)) {
            int at=0,count;while(at<bytes.length && (count=input.read(bytes,at,bytes.length-at))>0)at+=count;
            assertEquals(bytes.length,at);
        }
        ByteBuffer data=ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
        assertEquals("RIFF",new String(bytes,0,4,StandardCharsets.US_ASCII));
        assertEquals("WAVE",new String(bytes,8,4,StandardCharsets.US_ASCII));
        int at=12,format=0,channels=0,rate=0,bits=0;
        while(at+8<=bytes.length) {
            String type=new String(bytes,at,4,StandardCharsets.US_ASCII);int size=data.getInt(at+4);at+=8;
            assertTrue(size>=0 && at+size<=bytes.length);
            if(type.equals("fmt ")) {format=data.getShort(at);channels=data.getShort(at+2);rate=data.getInt(at+4);bits=data.getShort(at+14);}
            if(type.equals("data")) {
                assertEquals(1,format);assertEquals(1,channels);assertEquals(16000,rate);assertEquals(16,bits);
                float[] samples=new float[size/2];for(int i=0;i<samples.length;i++)samples[i]=data.getShort(at+2*i)/32768f;return samples;
            }
            at+=size+(size%2);
        }
        throw new AssertionError("No PCM samples in fixture");
    }
}
