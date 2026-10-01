package org.coyote.mobile;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.net.Uri;
import android.os.CancellationSignal;
import android.provider.MediaStore;
import android.test.InstrumentationTestCase;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient.FileChooserParams;
import java.util.ArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

public final class WebFilePickerTest extends InstrumentationTestCase {
    static FileChooserParams params(String... accepts) {
        return new FileChooserParams() {
            public int getMode() {return MODE_OPEN;}
            public String[] getAcceptTypes() {return accepts;}
            public boolean isCaptureEnabled() {return false;}
            public CharSequence getTitle() {return null;}
            public String getFilenameHint() {return null;}
            public Intent createIntent() {return new Intent(Intent.ACTION_GET_CONTENT);}
        };
    }
    static final class Events implements WebFilePicker.Events {
        boolean pending;
        int failures;
        public void pendingChanged(boolean value) {pending=value;}
        public void failed(String ignored) {failures++;}
    }
    static final class Result implements ValueCallback<Uri[]> {
        final CountDownLatch arrived=new CountDownLatch(1);
        int calls;
        Uri[] value;
        public void onReceiveValue(Uri[] value) {this.value=value;calls++;arrived.countDown();}
    }
    private void ui(Runnable action) {getInstrumentation().runOnMainSync(action);}

    public void testPhotoIntentAndDocumentFallbackPolicy() {
        assertTrue(WebFilePicker.imageInput(new String[]{"image/*"}));
        assertTrue(WebFilePicker.imageInput(new String[]{".JPEG,.png", "image/webp"}));
        assertFalse(WebFilePicker.imageInput(new String[]{"image/*", "application/zip"}));
        assertFalse(WebFilePicker.imageInput(new String[]{""}));
        Intent photo=WebFilePicker.intent(true,false,33);
        assertEquals(MediaStore.ACTION_PICK_IMAGES,photo.getAction());assertEquals("image/*",photo.getType());
        assertFalse(photo.hasExtra(MediaStore.EXTRA_PICK_IMAGES_MAX));
        assertNull(photo.getData());assertNull(photo.getClipData());
        Intent fallback=WebFilePicker.intent(true,false,32);
        assertEquals(Intent.ACTION_OPEN_DOCUMENT,fallback.getAction());assertEquals("image/*",fallback.getType());
        assertTrue(fallback.hasCategory(Intent.CATEGORY_OPENABLE));assertFalse(fallback.getBooleanExtra(Intent.EXTRA_ALLOW_MULTIPLE,true));
        Intent imported=WebFilePicker.intent(false,true,35);
        assertEquals(Intent.ACTION_OPEN_DOCUMENT,imported.getAction());assertEquals("*/*",imported.getType());
        assertTrue(imported.getBooleanExtra(Intent.EXTRA_ALLOW_MULTIPLE,false));
    }
    public void testOriginsAndReturnedUriContract() {
        assertTrue(WebFilePicker.localPage(Uri.parse("http://127.0.0.1:4567/roles"),"http://127.0.0.1:4567"));
        for(String address:new String[]{"http://127.0.0.1:4568/", "http://attacker.invalid:4567/", "http://user@127.0.0.1:4567/", "https://127.0.0.1:4567/", "file:///data/secret"})
            assertFalse(WebFilePicker.localPage(Uri.parse(address),"http://127.0.0.1:4567"));
        for(String address:new String[]{"https://example.invalid/photo.png","file:///data/secret","content:///missing-authority","content://user@provider/photo"})
            assertNull(WebFilePicker.contentUris(new Intent().setData(Uri.parse(address)),false));
        Uri first=Uri.parse("content://media/test/1"),second=Uri.parse("content://media/test/2");
        assertEquals(first,WebFilePicker.contentUris(new Intent().setData(first),false)[0]);
        ClipData clip=ClipData.newRawUri("fixture",first);clip.addItem(new ClipData.Item(second));
        Intent clipped=new Intent();clipped.setClipData(clip);
        assertNull(WebFilePicker.contentUris(clipped,false));
        assertEquals(2,WebFilePicker.contentUris(clipped,true).length);
        Intent mismatched=new Intent().setData(second);mismatched.setClipData(ClipData.newRawUri("fixture",first));
        assertNull(WebFilePicker.contentUris(mismatched,false));
    }
    public void testCancelReplacementLateResultAndUntrustedRequest() {
        Events events=new Events();ArrayList<Integer> codes=new ArrayList<>();
        WebFilePicker picker=new WebFilePicker(getInstrumentation().getTargetContext().getContentResolver(),(i,c)->codes.add(c),events,35);
        Result first=new Result(),second=new Result(),unsafe=new Result(),closed=new Result();
        ui(()->{
            picker.open(first,params("image/*"),true,1);
            picker.open(second,params("image/*"),true,1);
            assertEquals(1,first.calls);assertNull(first.value);assertTrue(events.pending);
            picker.result(codes.get(0),Activity.RESULT_OK,new Intent().setData(Uri.parse("content://media/test/1")),1,()->true);
            assertEquals(0,second.calls);
            picker.result(codes.get(1),Activity.RESULT_CANCELED,null,1,()->true);
            assertEquals(1,second.calls);assertNull(second.value);assertFalse(events.pending);
            picker.result(codes.get(1),Activity.RESULT_CANCELED,null,1,()->true);assertEquals(1,second.calls);
            picker.open(unsafe,params("image/*"),false,1);assertEquals(1,unsafe.calls);assertEquals(2,codes.size());
            picker.open(closed,params("image/*"),true,1);picker.close();assertEquals(1,closed.calls);assertNull(closed.value);
        });
    }
    public void testUnavailablePhotoPickerFallsBackToImagesOnlyAndDenialClearsCallback() {
        Events events=new Events();ArrayList<Intent> launches=new ArrayList<>();
        WebFilePicker picker=new WebFilePicker(getInstrumentation().getTargetContext().getContentResolver(),(i,c)->{
            launches.add(i);if(MediaStore.ACTION_PICK_IMAGES.equals(i.getAction()))throw new ActivityNotFoundException();
        },events,35);
        Result accepted=new Result();
        ui(()->{
            picker.open(accepted,params(".png,.jpg"),true,3);
            assertEquals(2,launches.size());assertEquals(Intent.ACTION_OPEN_DOCUMENT,launches.get(1).getAction());
            assertEquals("image/*",launches.get(1).getType());assertTrue(events.pending);picker.close();
        });
        assertEquals(1,accepted.calls);assertNull(accepted.value);
        WebFilePicker denied=new WebFilePicker(getInstrumentation().getTargetContext().getContentResolver(),(i,c)->{throw new SecurityException();},events,35);
        Result rejected=new Result();
        ui(()->{denied.open(rejected,params("image/*"),true,4);assertFalse(events.pending);denied.close();});
        assertEquals(1,rejected.calls);assertNull(rejected.value);assertEquals(1,events.failures);
    }
    public void testPageChangeAndLockDiscardSelection() {
        for(boolean samePage:new boolean[]{true,false}) {
            AtomicInteger code=new AtomicInteger();Result result=new Result();
            WebFilePicker picker=new WebFilePicker(getInstrumentation().getTargetContext().getContentResolver(),(i,c)->code.set(c),new Events(),35);
            ui(()->{
                picker.open(result,params("image/*"),true,1);
                picker.result(code.get(),Activity.RESULT_OK,new Intent().setData(Uri.parse("content://media/test/1")),samePage?1:2,()->!samePage);
                assertEquals(1,result.calls);assertNull(result.value);picker.close();
            });
        }
    }
    public void testActualImageValidationAndCancelledWorker() throws Exception {
        try(PhotoPickerFixture valid=new PhotoPickerFixture(getInstrumentation().getTargetContext(),true);
            PhotoPickerFixture corrupt=new PhotoPickerFixture(getInstrumentation().getTargetContext(),false)) {
            assertTrue(WebFilePicker.validImage(valid.resolver,valid.uri,new CancellationSignal()));
            assertFalse(WebFilePicker.validImage(corrupt.resolver,corrupt.uri,new CancellationSignal()));
            CancellationSignal cancelled=new CancellationSignal();cancelled.cancel();
            assertFalse(WebFilePicker.validImage(valid.resolver,valid.uri,cancelled));
            AtomicInteger code=new AtomicInteger();Events events=new Events();Result result=new Result();
            WebFilePicker picker=new WebFilePicker(valid.resolver,(i,c)->code.set(c),events,35);
            ui(()->{picker.open(result,params("image/*"),true,1);picker.result(code.get(),Activity.RESULT_OK,new Intent().setData(valid.uri),1,()->true);});
            assertTrue(result.arrived.await(5,TimeUnit.SECONDS));assertEquals(1,result.calls);assertEquals(valid.uri,result.value[0]);
            Result obsolete=new Result();
            ui(()->{
                picker.open(obsolete,params("image/*"),true,1);
                picker.result(code.get(),Activity.RESULT_OK,new Intent().setData(valid.uri),1,()->true);
                picker.cancel();picker.close();assertEquals(1,obsolete.calls);assertNull(obsolete.value);
            });
            getInstrumentation().waitForIdleSync();assertEquals(1,obsolete.calls);assertFalse(events.pending);
        }
    }
}
