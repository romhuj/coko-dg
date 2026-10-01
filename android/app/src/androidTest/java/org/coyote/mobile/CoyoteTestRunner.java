package org.coyote.mobile;

import android.content.Context;
import android.content.ContextWrapper;
import android.test.InstrumentationTestRunner;

/** Loads newly installed test classes when attaching to an existing app process. */
public final class CoyoteTestRunner extends InstrumentationTestRunner {
    private Context targetWithTestLoader;

    @Override
    public ClassLoader getLoader() {
        return getContext().getClassLoader();
    }

    @Override
    public Context getTargetContext() {
        Context target = super.getTargetContext();
        if (target == null) return null;
        if (targetWithTestLoader == null) {
            // The legacy runner ignores getLoader() when resolving an explicit
            // test class. Keep the target's data/resources, replacing only its
            // loader with the test APK loader (whose parent is the live app).
            targetWithTestLoader = new ContextWrapper(target) {
                @Override
                public ClassLoader getClassLoader() {
                    return CoyoteTestRunner.this.getLoader();
                }
            };
        }
        return targetWithTestLoader;
    }
}
