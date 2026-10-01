package org.coyote.mobile;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.TimeUnit;

/** Bounded producer/consumer PCM handoff. Cancellation wakes both sides without JNI access. */
final class ReplyAudioBuffer {
    private final ArrayDeque<float[]> blocks=new ArrayDeque<>();
    private final int capacityFrames,blockFrames;
    private int bufferedFrames;
    private boolean finished,cancelled;
    private String error;

    ReplyAudioBuffer(int capacityFrames,int blockFrames) {
        if(blockFrames<=0||capacityFrames<blockFrames)throw new IllegalArgumentException("Invalid PCM capacity");
        this.capacityFrames=capacityFrames;this.blockFrames=blockFrames;
    }

    boolean put(float[] samples) throws InterruptedException {
        for(int offset=0;offset<samples.length;) {
            int count=Math.min(blockFrames,samples.length-offset);
            synchronized(this) {
                while(!cancelled&&!finished&&bufferedFrames+count>capacityFrames)wait();
                if(cancelled||finished)return false;
                blocks.addLast(Arrays.copyOfRange(samples,offset,offset+count));bufferedFrames+=count;
                offset+=count;notifyAll();
            }
        }
        return true;
    }

    synchronized float[] take(long timeoutMs) throws InterruptedException {
        long deadline=System.nanoTime()+TimeUnit.MILLISECONDS.toNanos(timeoutMs);
        while(blocks.isEmpty()&&!finished&&!cancelled) {
            long remaining=deadline-System.nanoTime();if(remaining<=0)return null;
            TimeUnit.NANOSECONDS.timedWait(this,remaining);
        }
        if(cancelled||blocks.isEmpty())return null;
        float[] result=blocks.removeFirst();bufferedFrames-=result.length;notifyAll();return result;
    }

    synchronized void finish() {finished=true;notifyAll();}
    synchronized void fail(String message) {error=message;finished=true;blocks.clear();bufferedFrames=0;notifyAll();}
    synchronized void cancel() {cancelled=true;blocks.clear();bufferedFrames=0;notifyAll();}
    synchronized boolean drained() {return (finished||cancelled)&&blocks.isEmpty();}
    synchronized boolean accepting() {return !finished&&!cancelled;}
    synchronized String error() {return error;}
    synchronized int bufferedFrames() {return bufferedFrames;}

    /** Keep natural short boundaries; don't compound model edge silence with synthesis waits. */
    static float[] trimEdges(float[] samples,int sampleRate) {
        int first=0,last=samples.length;
        while(first<last&&Math.abs(samples[first])<0.0005f)first++;
        while(last>first&&Math.abs(samples[last-1])<0.0005f)last--;
        if(first==last)return new float[0];
        first=Math.max(0,first-sampleRate/50); // retain 20ms before speech
        last=Math.min(samples.length,last+sampleRate/10); // retain 100ms sentence pause
        return first==0&&last==samples.length?samples:Arrays.copyOfRange(samples,first,last);
    }

    /** Preserve every character while bounding individual non-interruptible native batches. */
    static List<String> split(String text) {
        List<String> result=new ArrayList<>();
        for(int start=0;start<text.length();) {
            int limit=result.isEmpty()?24:48,end=Math.min(text.length(),start+limit);
            if(end<text.length()&&Character.isHighSurrogate(text.charAt(end-1)))end--;
            int boundary=-1;
            for(int at=start+7;at<end;at++) {
                char value=text.charAt(at);
                boolean decimal=value=='.'&&at>0&&at+1<text.length()
                        &&Character.isDigit(text.charAt(at-1))&&Character.isDigit(text.charAt(at+1));
                if(!decimal&&"。！？；\n.!?;".indexOf(value)>=0) {
                    boundary=at+1;if(result.isEmpty())break;
                }
            }
            if(boundary>start)end=boundary;
            else if(end<text.length()) {
                for(int at=end-1;at>start+limit/2;at--) {
                    if(Character.isWhitespace(text.charAt(at))||"，,、：:".indexOf(text.charAt(at))>=0) {end=at+1;break;}
                }
            }
            result.add(text.substring(start,end));start=end;
        }
        return result;
    }
}
