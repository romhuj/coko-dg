package org.coyote.mobile;
import android.content.Context;
import android.widget.Toast;
final class ToastMessage {
    private ToastMessage() {}
    static void show(Context context, String message) { Toast.makeText(context, message, Toast.LENGTH_LONG).show(); }
}
