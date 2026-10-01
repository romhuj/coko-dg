package org.coyote.mobile;

import android.test.AndroidTestCase;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InterruptedIOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** Small deterministic tests: no microphone, network, model inference or device commands. */
@SuppressWarnings("deprecation")
public final class VoiceEngineUnitTest extends AndroidTestCase {
    public void testSilenceAndPunctuationAreNotSent() {
        assertFalse(ContinuousVoiceRecognizer.hasEnergy(new float[16000]));
        assertFalse(ContinuousVoiceRecognizer.hasWords("。！？ …"));
        assertFalse(ContinuousVoiceRecognizer.hasWords(""));
        assertTrue(ContinuousVoiceRecognizer.hasWords("停止"));
        assertTrue(ContinuousVoiceRecognizer.hasWords("123"));
        assertTrue(ContinuousVoiceRecognizer.hasEnergy(new float[]{.01f, -.01f, .01f}));
    }

    public void testSenseVoiceTagsAreRemovedWithoutChangingChineseWords() {
        assertEquals("你好，请保持。", ContinuousVoiceRecognizer.cleanText("<|zh|><|NEUTRAL|><|Speech|><|withitn|>你好，请保持。 "));
        assertEquals("", ContinuousVoiceRecognizer.cleanText(null));
        assertEquals("", ContinuousVoiceRecognizer.cleanText("<|zh|><|NEUTRAL|>"));
        assertEquals("再说一次，再说一次。", ContinuousVoiceRecognizer.cleanText("再说一次，再说一次。"));
    }

    public void testIncompleteAndCorruptModelFilesAreNeverReady() throws Exception {
        File file=File.createTempFile("voice-check-", ".tmp", getContext().getCacheDir());
        byte[] valid="known model bytes".getBytes(StandardCharsets.UTF_8);
        String expected=VoiceModel.hex(MessageDigest.getInstance("SHA-256").digest(valid));
        VoiceModel.Spec spec=new VoiceModel.Spec("model", "https://example.invalid/model", valid.length, expected);
        VoiceModel.CancelToken token=new VoiceModel.CancelToken();
        try {
            assertFalse(VoiceModel.verified(file, spec, token));
            try (FileOutputStream output=new FileOutputStream(file)) { output.write(valid); }
            assertTrue(VoiceModel.verified(file, spec, token));
            valid[0]^=1;
            try (FileOutputStream output=new FileOutputStream(file)) { output.write(valid); }
            assertFalse(VoiceModel.verified(file, spec, token));
        } finally { file.delete(); }
    }

    public void testCancelledPreparationDoesNotHashOrStartInference() throws Exception {
        VoiceModel.CancelToken token=new VoiceModel.CancelToken();
        token.cancel();
        assertTrue(token.isCancelled());
        try { token.check(); fail("Cancelled token accepted"); }
        catch (InterruptedIOException expected) { }
    }

    public void testPinnedDownloadsAreIndividualHttpsFilesWithFullHashes() {
        long bytes=0;
        for (VoiceModel.Spec spec : VoiceModel.FILES) {
            assertTrue(spec.url.startsWith("https://"));
            assertFalse(spec.filename.contains("/"));
            assertFalse(spec.filename.contains(".."));
            assertTrue(spec.sha256.matches("[a-f0-9]{64}"));
            assertTrue(spec.bytes > 0);
            bytes+=spec.bytes;
        }
        assertEquals(VoiceModel.TOTAL_BYTES, bytes);
    }

    public void testBundledVadIsVerifiedBeforeBecomingAvailable() throws Exception {
        VoiceModel model=new VoiceModel(getContext());
        VoiceModel.CancelToken token=new VoiceModel.CancelToken();
        File destination=File.createTempFile("voice-bundled-", ".onnx", getContext().getCacheDir());
        assertTrue(destination.delete());
        try {
            assertTrue(model.installBundled(VoiceModel.FILES[2], destination, token));
            assertTrue(VoiceModel.verified(destination, VoiceModel.FILES[2], token));
            assertTrue(destination.delete());
            VoiceModel.Spec invalid=new VoiceModel.Spec("silero_vad.onnx", VoiceModel.FILES[2].url,
                    VoiceModel.FILES[2].bytes, "0000000000000000000000000000000000000000000000000000000000000000");
            try { model.installBundled(invalid, destination, token); fail("Invalid asset hash accepted"); }
            catch (java.io.IOException expected) { }
            assertFalse(destination.exists());
        } finally { destination.delete(); }
    }
}
