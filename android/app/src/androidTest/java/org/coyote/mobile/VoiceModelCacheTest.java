package org.coyote.mobile;

import android.os.Build;
import android.system.Os;
import android.test.AndroidTestCase;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InterruptedIOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** Tiny private files only: no download, JNI, microphone or model API. */
@SuppressWarnings("deprecation")
public final class VoiceModelCacheTest extends AndroidTestCase {
    private File directory,file;
    private VoiceModel model;
    private VoiceModel.Spec spec;
    private final byte[] bytes="固定的模型字节：quotes\"\n".getBytes(StandardCharsets.UTF_8);

    @Override protected void setUp() throws Exception {
        super.setUp();
        directory=File.createTempFile("voice-cache-test-","",getContext().getCacheDir());
        assertTrue(directory.delete()); assertTrue(directory.mkdir());
        file=new File(directory,"sample.onnx"); write(file,bytes);
        model=new VoiceModel(directory,getContext().getAssets());
        spec=new VoiceModel.Spec(file.getName(),"https://example.invalid/model",bytes.length,
                VoiceModel.hex(MessageDigest.getInstance("SHA-256").digest(bytes)));
    }
    @Override protected void tearDown() throws Exception {
        for(File child:directory.listFiles()) child.delete();
        directory.delete(); super.tearDown();
    }
    private static void write(File target,byte[] data) throws Exception {
        try(FileOutputStream output=new FileOutputStream(target)) { output.write(data); output.getFD().sync(); }
    }
    private boolean verify() throws Exception { return model.verifiedCached(file,spec,new VoiceModel.CancelToken()); }

    public void testReceiptSurvivesNewModelInstanceAndCancellationStillWins() throws Exception {
        assertFalse(model.cachedOnly(file,spec)); assertTrue(verify());
        assertEquals(Build.VERSION.SDK_INT>=27,new VoiceModel(directory,getContext().getAssets()).cachedOnly(file,spec));
        VoiceModel.CancelToken cancelled=new VoiceModel.CancelToken(); cancelled.cancel();
        try { model.verifiedCached(file,spec,cancelled); fail("Cancelled cached hit accepted"); }
        catch(InterruptedIOException expected) { }
    }
    public void testSameSizeMutationWithRestoredMtimeIsRejected() throws Exception {
        // Start with millisecond precision, so restoring mtime also restores its nanoseconds.
        assertTrue(file.setLastModified(1700000000000L));
        assertTrue(verify()); long modified=file.lastModified();
        Thread.sleep(20); byte[] corrupted=bytes.clone(); corrupted[0]^=1; write(file,corrupted);
        assertTrue(file.setLastModified(modified));
        assertFalse(model.cachedOnly(file,spec)); assertFalse(verify());
    }
    public void testAtomicReplacementInvalidatesInodeEvenWhenSizeAndMtimeMatch() throws Exception {
        assertTrue(verify()); long modified=file.lastModified();
        File replacement=new File(directory,"replacement"); byte[] corrupted=bytes.clone(); corrupted[0]^=1;
        write(replacement,corrupted); assertTrue(replacement.setLastModified(modified));
        Os.rename(replacement.getAbsolutePath(),file.getAbsolutePath());
        assertFalse(model.cachedOnly(file,spec)); assertFalse(verify());
    }
    public void testExpectedHashAndReceiptVersionAreBound() throws Exception {
        assertTrue(verify());
        VoiceModel.Spec changed=new VoiceModel.Spec(spec.filename,spec.url,spec.bytes,
                "0000000000000000000000000000000000000000000000000000000000000000");
        assertFalse(model.cachedOnly(file,changed));
        assertFalse(model.verifiedCached(file,changed,new VoiceModel.CancelToken()));
        assertTrue(verify());
        if(Build.VERSION.SDK_INT>=27) {
            File receipt=new File(directory,spec.filename+".verified.json");
            String json=new String(java.nio.file.Files.readAllBytes(receipt.toPath()),StandardCharsets.UTF_8);
            write(receipt,json.replace(VoiceModel.MODEL_VERSION,"old-model-version").getBytes(StandardCharsets.UTF_8));
            assertFalse(model.cachedOnly(file,spec)); assertTrue(verify());
        }
    }
    public void testMalformedReceiptRequiresFullHashAndCanBeRepaired() throws Exception {
        assertTrue(verify());
        write(new File(directory,spec.filename+".verified.json"),"{bad".getBytes(StandardCharsets.UTF_8));
        assertFalse(model.cachedOnly(file,spec)); assertTrue(verify());
        assertEquals(Build.VERSION.SDK_INT>=27,model.cachedOnly(file,spec));
    }
    public void testSymlinkAndTruncationCannotReuseReceipt() throws Exception {
        assertTrue(verify());
        File actual=new File(directory,"actual"); assertTrue(file.renameTo(actual));
        Os.symlink(actual.getAbsolutePath(),file.getAbsolutePath());
        assertFalse(model.cachedOnly(file,spec)); assertFalse(verify());
        assertTrue(file.delete()); write(file,new byte[]{1});
        assertFalse(model.cachedOnly(file,spec)); assertFalse(verify());
    }
    public void testPlaybackInvalidatesInflightAndKeepsTailWindowWithoutReloading() {
        ContinuousVoiceRecognizer.PlaybackGate gate=new ContinuousVoiceRecognizer.PlaybackGate();
        long initial=gate.generation(); assertTrue(gate.accepts(initial,100));
        assertTrue(gate.set(true,200)); assertFalse(gate.accepts(initial,201));
        long playing=gate.generation(); assertFalse(gate.accepts(playing,10000));
        assertFalse(gate.set(true,201)); assertEquals(playing,gate.generation());
        assertTrue(gate.set(false,1000)); long resumed=gate.generation();
        assertFalse(gate.accepts(initial,2000)); assertFalse(gate.accepts(playing,2000));
        assertFalse(gate.accepts(resumed,1349)); assertTrue(gate.accepts(resumed,1350));
        assertFalse(gate.set(false,1400)); assertTrue(gate.accepts(resumed,1400));
    }
}
