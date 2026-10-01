package org.coyote.mobile;

import android.media.AudioDeviceInfo;
import junit.framework.TestCase;

/** Capability/segmentation boundaries only: no microphone, speaker, JNI or model request. */
public final class VoiceDuplexTest extends TestCase {
    public void testAecRequiresEnabledAndControl() {
        int speaker=AudioDeviceInfo.TYPE_BUILTIN_SPEAKER;
        assertTrue(ContinuousVoiceRecognizer.supportsDuplex(true,true,speaker));
        assertFalse(ContinuousVoiceRecognizer.supportsDuplex(true,false,speaker));
        assertFalse(ContinuousVoiceRecognizer.supportsDuplex(false,true,speaker));
        assertFalse(ContinuousVoiceRecognizer.supportsDuplex(false,false,speaker));
    }

    public void testOnlyKnownIsolatedActualOutputCountsAsHeadset() {
        int[] isolated={AudioDeviceInfo.TYPE_WIRED_HEADSET,AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
                AudioDeviceInfo.TYPE_USB_HEADSET,AudioDeviceInfo.TYPE_BLUETOOTH_SCO,AudioDeviceInfo.TYPE_BLE_HEADSET};
        for(int type:isolated) assertTrue(ContinuousVoiceRecognizer.supportsDuplex(false,false,type));
        int[] ambiguous={AudioDeviceInfo.TYPE_UNKNOWN,AudioDeviceInfo.TYPE_USB_DEVICE,AudioDeviceInfo.TYPE_BLE_SPEAKER,
                AudioDeviceInfo.TYPE_BUILTIN_SPEAKER,AudioDeviceInfo.TYPE_BUILTIN_EARPIECE,AudioDeviceInfo.TYPE_BLUETOOTH_A2DP};
        for(int type:ambiguous) assertFalse(ContinuousVoiceRecognizer.supportsDuplex(false,false,type));
    }

    public void testFullDuplexPlaybackPreservesCurrentAudioGeneration() {
        ContinuousVoiceRecognizer.PlaybackGate gate=new ContinuousVoiceRecognizer.PlaybackGate();
        long ongoing=gate.generation();
        assertFalse(gate.playback(true,true,100));
        assertTrue(gate.accepts(ongoing,100));
        assertFalse(gate.playback(false,true,110));
        assertTrue(gate.accepts(ongoing,110));
        assertEquals(ongoing,gate.generation());
        // Losing echo protection during playback invalidates samples and pending results immediately.
        assertTrue(gate.playback(true,false,200));
        assertFalse(gate.accepts(ongoing,200));
        assertTrue(gate.playback(false,false,300));
        long fresh=gate.generation();
        assertFalse(gate.accepts(fresh,649));
        assertTrue(gate.accepts(fresh,650));
    }

    public void testBargeInRequiresContinuousQuarterSecondAndEmitsOnce() {
        ContinuousVoiceRecognizer.SpeechStartGate gate=new ContinuousVoiceRecognizer.SpeechStartGate();
        for(int i=0;i<7;i++) assertFalse(gate.offer(true,512));
        assertTrue(gate.offer(true,512));
        for(int i=0;i<20;i++) assertFalse(gate.offer(true,512));
        assertFalse(gate.offer(false,512));
        for(int i=0;i<7;i++) assertFalse(gate.offer(true,512));
        assertTrue(gate.offer(true,512));
        gate.reset();
        assertFalse(gate.offer(true,2000));
        assertFalse(gate.offer(false,512));
        assertFalse(gate.offer(true,2000));
    }
}
