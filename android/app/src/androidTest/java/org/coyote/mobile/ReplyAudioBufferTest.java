package org.coyote.mobile;

import junit.framework.TestCase;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/** Pure Java: run either on the JVM or in the instrumentation suite, without models/audio. */
public final class ReplyAudioBufferTest extends TestCase {
    public void testPlaybackCanConsumeBeforeProducerFinishesAndPreservesEveryFrame() throws Exception {
        ReplyAudioBuffer pipe=new ReplyAudioBuffer(4,2);
        CountDownLatch initial=new CountDownLatch(1),complete=new CountDownLatch(1);
        AtomicReference<Throwable> failure=new AtomicReference<>();
        Thread producer=new Thread(()->{
            try {pipe.put(new float[]{1,2,3,4});initial.countDown();pipe.put(new float[]{5,6});pipe.finish();}
            catch(Throwable error){failure.set(error);}
            finally {complete.countDown();}
        });
        producer.start();
        try {
            assertTrue(initial.await(2,TimeUnit.SECONDS));assertEquals(4,pipe.bufferedFrames());
            assertFalse("Producer ignored bounded capacity",complete.await(50,TimeUnit.MILLISECONDS));
            List<Float> heard=new ArrayList<>();
            for(int i=0;i<3;i++) {float[] block=pipe.take(2000);assertNotNull(block);for(float value:block)heard.add(value);}
            assertTrue(complete.await(2,TimeUnit.SECONDS));assertNull(failure.get());
            assertEquals(Arrays.asList(1f,2f,3f,4f,5f,6f),heard);assertTrue(pipe.drained());
            assertNull(pipe.take(0));assertFalse(pipe.put(new float[]{7}));
        } finally {pipe.cancel();producer.join(2000);}
    }

    public void testCancellationWakesBlockedProducerAndDiscardsPendingPcm() throws Exception {
        ReplyAudioBuffer pipe=new ReplyAudioBuffer(2,2);assertTrue(pipe.put(new float[]{1,2}));
        CountDownLatch entered=new CountDownLatch(1),complete=new CountDownLatch(1);
        AtomicBoolean accepted=new AtomicBoolean(true);
        Thread producer=new Thread(()->{
            try {entered.countDown();accepted.set(pipe.put(new float[]{3,4}));}
            catch(InterruptedException error){Thread.currentThread().interrupt();}
            finally {complete.countDown();}
        });
        producer.start();
        assertTrue(entered.await(2,TimeUnit.SECONDS));pipe.cancel();
        assertTrue(complete.await(2,TimeUnit.SECONDS));assertFalse(accepted.get());
        assertEquals(0,pipe.bufferedFrames());assertNull(pipe.take(0));assertFalse(pipe.accepting());
        producer.join(2000);
    }

    public void testOneNativeCallbackLargerThanCapacityStreamsWithoutDeadlock() throws Exception {
        ReplyAudioBuffer pipe=new ReplyAudioBuffer(4,2);float[] large=new float[400];
        for(int i=0;i<large.length;i++)large[i]=i;
        AtomicReference<Throwable> failure=new AtomicReference<>();CountDownLatch done=new CountDownLatch(1);
        Thread producer=new Thread(()->{
            try {assertTrue(pipe.put(large));pipe.finish();}
            catch(Throwable error){failure.set(error);}
            finally {done.countDown();}
        });
        producer.start();
        try {
            int heard=0;
            while(heard<large.length) {
                float[] block=pipe.take(2000);assertNotNull("Large callback stopped making progress",block);
                assertTrue(pipe.bufferedFrames()<=4);
                for(float value:block)assertEquals((float)heard++,value,0f);
            }
            assertTrue(done.await(2,TimeUnit.SECONDS));assertNull(failure.get());assertTrue(pipe.drained());
        } finally {pipe.cancel();producer.join(2000);}
    }

    public void testCancellationWakesConsumerAndReplacementQueueContainsOnlyNewAudio() throws Exception {
        ReplyAudioBuffer old=new ReplyAudioBuffer(4,2);CountDownLatch complete=new CountDownLatch(1);
        AtomicReference<float[]> heard=new AtomicReference<>(new float[]{99});
        Thread consumer=new Thread(()->{
            try {heard.set(old.take(20000));}
            catch(InterruptedException error){Thread.currentThread().interrupt();}
            finally {complete.countDown();}
        });
        consumer.start();old.cancel();assertTrue(complete.await(2,TimeUnit.SECONDS));assertNull(heard.get());
        assertFalse(old.put(new float[]{1,2}));
        ReplyAudioBuffer replacement=new ReplyAudioBuffer(4,2);replacement.put(new float[]{7,8});replacement.finish();
        assertTrue(Arrays.equals(new float[]{7,8},replacement.take(0)));assertTrue(replacement.drained());
        consumer.join(2000);
    }

    public void testProducerFailureDoesNotPlayQueuedRemainder() throws Exception {
        ReplyAudioBuffer pipe=new ReplyAudioBuffer(4,2);pipe.put(new float[]{1,2,3,4});pipe.fail("failed");
        assertEquals("failed",pipe.error());assertNull(pipe.take(0));assertTrue(pipe.drained());
        assertFalse(pipe.put(new float[]{5}));
    }

    public void testSemanticChunksPreserveLongReplyAndSurrogatePairs() {
        StringBuilder source=new StringBuilder("第一句需要完整朗读。第二句也应该继续播放，不能只读第一句。数值是35.25，不应丢失。");
        for(int i=0;i<30;i++)source.append("这里包含表情🙂，还包含 English words and punctuation. ");
        String text=source.toString();
        List<String> pieces=ReplyAudioBuffer.split(text);assertTrue(pieces.size()>3);
        assertEquals(text,String.join("",pieces));
        for(int i=0;i<pieces.size();i++) {
            String piece=pieces.get(i);assertFalse(piece.isEmpty());assertTrue(piece.length()<=(i==0?24:48));
            assertFalse(Character.isHighSurrogate(piece.charAt(piece.length()-1)));
            assertFalse(Character.isLowSurrogate(piece.charAt(0)));
        }
    }

    public void testOnlyExcessEdgeSilenceIsTrimmedWithShortNaturalPauseRetained() {
        float[] samples=new float[1000];Arrays.fill(samples,300,400,.2f);samples[350]=0;
        float[] trimmed=ReplyAudioBuffer.trimEdges(samples,1000);
        assertEquals(220,trimmed.length);assertEquals(.2f,trimmed[20],0f);assertEquals(0f,trimmed[70],0f);
        assertEquals(.2f,trimmed[119],0f);assertEquals(0f,trimmed[219],0f);
        assertEquals(0,ReplyAudioBuffer.trimEdges(new float[100],1000).length);
    }
}
