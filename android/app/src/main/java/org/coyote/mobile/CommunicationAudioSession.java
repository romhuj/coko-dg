package org.coyote.mobile;

import android.content.Context;
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import java.util.ArrayList;
import java.util.IdentityHashMap;
import java.util.List;

/** Shared, main-thread-owned communication route. ASR and playback own independent leases. */
final class CommunicationAudioSession {
    private static final Handler MAIN=new Handler(Looper.getMainLooper());
    private static final IdentityHashMap<Object,Boolean> OWNERS=new IdentityHashMap<>();
    private static final IdentityHashMap<Object,Runnable> LISTENERS=new IdentityHashMap<>();
    private static final IdentityHashMap<Object,AudioDeviceInfo> OUTPUTS=new IdentityHashMap<>();
    private static AudioManager audio;
    private static int previousMode;
    private static boolean routeRequested, previousSpeaker;
    private static volatile int outputType=AudioDeviceInfo.TYPE_UNKNOWN;
    private static AudioManager.OnCommunicationDeviceChangedListener communicationListener;
    private static final AudioDeviceCallback DEVICES=new AudioDeviceCallback() {
        @Override public void onAudioDevicesAdded(AudioDeviceInfo[] devices) { refreshRoute(true); }
        @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] devices) { refreshRoute(true); }
    };

    private static void mainThread() {
        if(Looper.myLooper()!=Looper.getMainLooper()) throw new IllegalStateException("Audio route changes require the main thread");
    }

    static boolean acquire(Context context,Object owner) {
        mainThread();
        if(owner==null) throw new IllegalArgumentException("Missing audio owner");
        if(OWNERS.containsKey(owner)) return audio!=null && audio.getMode()==AudioManager.MODE_IN_COMMUNICATION;
        if(OWNERS.isEmpty()) {
            AudioManager candidate=context.getApplicationContext().getSystemService(AudioManager.class);
            if(candidate==null) return false;
            try {
                // Never replace a telephony call's route/mode.
                previousMode=candidate.getMode();
                if(previousMode==AudioManager.MODE_IN_CALL) return false;
                previousSpeaker=candidate.isSpeakerphoneOn();
                audio=candidate;
                candidate.setMode(AudioManager.MODE_IN_COMMUNICATION);
                if(candidate.getMode()!=AudioManager.MODE_IN_COMMUNICATION) {cleanup();return false;}
                candidate.registerAudioDeviceCallback(DEVICES,MAIN);
                if(Build.VERSION.SDK_INT>=31) {
                    communicationListener=device->refreshRoute(false);
                    candidate.addOnCommunicationDeviceChangedListener(MAIN::post,communicationListener);
                } else if(!candidate.isWiredHeadsetOn() && !candidate.isBluetoothScoOn()) {
                    candidate.setSpeakerphoneOn(true);
                    routeRequested=true;
                }
            } catch(RuntimeException failure) {
                cleanup();
                return false;
            }
        }
        OWNERS.put(owner,Boolean.TRUE);
        refreshRoute(true);
        return true;
    }

    static void release(Object owner) {
        mainThread();
        if(OWNERS.remove(owner)==null) return;
        if(OWNERS.isEmpty()) cleanup();
    }

    static void addListener(Object owner,Runnable listener) { mainThread(); LISTENERS.put(owner,listener); }
    static void removeListener(Object owner) { mainThread(); LISTENERS.remove(owner); }
    static int outputDeviceType() { return outputType; }

    /** AudioTrack reports its actual active output; a preferred/connected device is not evidence. */
    static void reportOutputDevice(Object owner,AudioDeviceInfo device) {
        mainThread();
        if(device==null || OWNERS.isEmpty()) OUTPUTS.remove(owner); else OUTPUTS.put(owner,device);
        refreshRoute(false);
    }

    static boolean isHeadset(int type) {
        return type==AudioDeviceInfo.TYPE_WIRED_HEADSET || type==AudioDeviceInfo.TYPE_WIRED_HEADPHONES
                || type==AudioDeviceInfo.TYPE_USB_HEADSET || type==AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                || type==AudioDeviceInfo.TYPE_BLE_HEADSET;
    }

    private static void refreshRoute(boolean choose) {
        mainThread();
        AudioManager manager=audio;
        int selected=AudioDeviceInfo.TYPE_UNKNOWN;
        if(manager!=null && !OWNERS.isEmpty()) {
            try {
                if(Build.VERSION.SDK_INT>=31) {
                    if(choose) {
                        List<AudioDeviceInfo> devices=manager.getAvailableCommunicationDevices();
                        AudioDeviceInfo current=manager.getCommunicationDevice(), best=null;
                        if(current!=null && isHeadset(current.getType())) best=current;
                        if(best==null) for(AudioDeviceInfo device:devices) if(isHeadset(device.getType())) {best=device;break;}
                        if(best==null) for(AudioDeviceInfo device:devices)
                            if(device.getType()==AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) {best=device;break;}
                        if(best!=null && (current==null || current.getId()!=best.getId()))
                            routeRequested=manager.setCommunicationDevice(best) || routeRequested;
                    }
                    AudioDeviceInfo device=manager.getCommunicationDevice();
                    if(device!=null) selected=device.getType();
                }
                // If more than one active renderer disagrees, be conservative about isolation.
                if(!OUTPUTS.isEmpty()) {
                    selected=AudioDeviceInfo.TYPE_UNKNOWN;
                    AudioDeviceInfo[] attached=manager.getDevices(AudioManager.GET_DEVICES_OUTPUTS);
                    for(AudioDeviceInfo device:OUTPUTS.values()) {
                        boolean present=false;
                        for(AudioDeviceInfo known:attached) if(known.getId()==device.getId()) {present=true;break;}
                        // A removed headset can be reported by AudioTrack until its route callback arrives.
                        if(!present) {selected=AudioDeviceInfo.TYPE_UNKNOWN;break;}
                        if(!isHeadset(device.getType())) {selected=device.getType();break;}
                        selected=device.getType();
                    }
                }
            } catch(RuntimeException ignored) {selected=AudioDeviceInfo.TYPE_UNKNOWN;}
        }
        publish(selected);
    }

    private static void publish(int next) {
        if(outputType==next) return;
        outputType=next;
        for(Runnable listener:new ArrayList<>(LISTENERS.values())) {
            try {listener.run();} catch(RuntimeException ignored) { }
        }
    }

    private static void cleanup() {
        AudioManager manager=audio;
        audio=null;
        if(manager!=null) {
            try {manager.unregisterAudioDeviceCallback(DEVICES);} catch(RuntimeException ignored) { }
            if(Build.VERSION.SDK_INT>=31 && communicationListener!=null) {
                try {manager.removeOnCommunicationDeviceChangedListener(communicationListener);} catch(RuntimeException ignored) { }
            }
            try {
                // A newer system call may now own the mode. Do not alter that call's routing.
                if(manager.getMode()==AudioManager.MODE_IN_COMMUNICATION) {
                    if(routeRequested) {
                        if(Build.VERSION.SDK_INT>=31) manager.clearCommunicationDevice();
                        else manager.setSpeakerphoneOn(previousSpeaker);
                    }
                    manager.setMode(previousMode);
                }
            } catch(RuntimeException ignored) { }
        }
        communicationListener=null;routeRequested=false;OUTPUTS.clear();publish(AudioDeviceInfo.TYPE_UNKNOWN);
    }
}
