package org.coyote.mobile;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.provider.MediaStore;
import java.io.OutputStream;
import junit.framework.Assert;

/** Creates only synthetic images owned by the tested app on a dedicated emulator. */
final class PhotoPickerFixture implements AutoCloseable {
    final ContentResolver resolver;
    final Uri uri;
    PhotoPickerFixture(Context context,boolean valid) throws Exception {
        Assert.assertTrue("Photo fixtures are emulator-only",Build.MODEL.contains("sdk_gphone") || Build.HARDWARE.contains("ranchu"));
        Assert.assertTrue("Photo fixture requires API 29+",Build.VERSION.SDK_INT>=29);
        resolver=context.getContentResolver();
        ContentValues values=new ContentValues();
        values.put(MediaStore.Images.Media.DISPLAY_NAME,"coko-picker-test-"+System.nanoTime()+".png");
        values.put(MediaStore.Images.Media.MIME_TYPE,"image/png");
        values.put(MediaStore.Images.Media.RELATIVE_PATH,"Pictures/CokoPickerTest");
        values.put(MediaStore.Images.Media.IS_PENDING,1);
        uri=resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI,values);
        Assert.assertNotNull(uri);
        try {
            try(OutputStream output=resolver.openOutputStream(uri,"w")) {
                Assert.assertNotNull(output);
                if(valid) {
                    Bitmap image=Bitmap.createBitmap(8,6,Bitmap.Config.ARGB_8888);
                    try {image.eraseColor(0xff886622);Assert.assertTrue(image.compress(Bitmap.CompressFormat.PNG,100,output));}
                    finally {image.recycle();}
                } else output.write("This is not an image".getBytes(java.nio.charset.StandardCharsets.UTF_8));
            }
            values.clear();values.put(MediaStore.Images.Media.IS_PENDING,0);
            resolver.update(uri,values,null,null);
        } catch(Throwable failure) {resolver.delete(uri,null,null);throw failure;}
    }
    @Override public void close() {resolver.delete(uri,null,null);}
}
