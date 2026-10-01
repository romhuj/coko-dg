package org.coyote.mobile;

import android.test.InstrumentationTestCase;
import java.util.ArrayList;
import java.util.List;

/** Playback lifecycle without microphones, installed TTS engines, network or device commands. */
@SuppressWarnings("deprecation")
public final class ReplySpeechTest extends InstrumentationTestCase {
    private ReplySpeech speech;
    private final List<String> states=new ArrayList<>();
    private final List<FakeDriver> drivers=new ArrayList<>();
    private final FakeGate gate=new FakeGate();
    private static final class FakeGate implements ReplySpeech.PlaybackGate {
        int begins, ends;boolean allowed=true, active;
        public boolean begin(){begins++;active=allowed;return allowed;}
        public void end(){ends++;active=false;}
    }
    private static final class FakeDriver implements ReplySpeech.Driver {
        final ReplySpeech.Callbacks callbacks;
        final List<String> texts=new ArrayList<>(), ids=new ArrayList<>();
        boolean closed, accepted=true;
        int limit=40, stops;
        FakeDriver(ReplySpeech.Callbacks callbacks){this.callbacks=callbacks;}
        public int limit(){return limit;}
        public boolean speak(String text,String id){texts.add(text);ids.add(id);return accepted;}
        public void stop(){stops++;}
        public void close(){closed=true;}
    }
    @Override protected void setUp() throws Exception {
        super.setUp();
        main(()->speech=new ReplySpeech(getInstrumentation().getTargetContext(),
                (session,status,item,message)->states.add(session+"/"+status+"/"+item),
                (context,voice,callbacks)->{FakeDriver driver=new FakeDriver(callbacks);drivers.add(driver);return driver;},gate));
    }
    @Override protected void tearDown() throws Exception {try{main(()->speech.close());}finally{super.tearDown();}}
    private void main(Runnable action){getInstrumentation().runOnMainSync(action);getInstrumentation().waitForIdleSync();}
    private FakeDriver ready(String session){main(()->speech.enable(session));FakeDriver driver=drivers.get(drivers.size()-1);main(()->driver.callbacks.ready(null));return driver;}

    public void testRapidSessionChangeCancelsInitializingDriver() {
        main(()->{speech.enable("first-session");speech.disable("");speech.enable("second-session");});
        assertEquals(2,drivers.size());assertTrue(drivers.get(0).closed);
        main(()->{drivers.get(0).callbacks.ready(null);drivers.get(1).callbacks.ready(null);});
        assertEquals("second-session/ready/null",states.get(states.size()-1));
        assertFalse(states.contains("first-session/ready/null"));
        main(()->speech.close());assertTrue(drivers.get(1).closed);
    }
    public void testChunkedReplyCompletesOnceAndHoldsMicrophoneGate() {
        FakeDriver driver=ready("read-session");String text="你好，今天的消息将分段朗读，整个回复只返回一次完成状态。".repeat(4);
        main(()->speech.speak("read-session","reply-one",text));
        main(()->speech.speak("read-session","reply-one",text));
        assertEquals(1,driver.texts.size());assertTrue(gate.active);
        int next=0;
        while(next<driver.ids.size()) {String id=driver.ids.get(next++);main(()->driver.callbacks.done(id));}
        assertEquals(text,String.join("",driver.texts));assertEquals(1,gate.begins);assertEquals(1,gate.ends);
        assertEquals(1,states.stream().filter(s->s.equals("read-session/ready/reply-one")).count());
    }
    public void testStopInvalidatesLateCallbacksAndReleasesGate() {
        FakeDriver driver=ready("read-session");main(()->speech.speak("read-session","reply-one","第一条消息"));
        String previous=driver.ids.get(0);main(()->speech.disable(""));
        int count=states.size();main(()->{driver.callbacks.done(previous);driver.callbacks.failed(previous,"late");});
        assertEquals(count,states.size());assertFalse(gate.active);assertEquals(1,gate.ends);
        main(()->speech.enable("new-session"));main(()->speech.speak("read-session","old","不应播放"));
        assertEquals(1,driver.texts.size());
    }
    public void testInitializationFailureCanBeRetried() {
        main(()->speech.enable("read-session"));FakeDriver bad=drivers.get(0);
        main(()->bad.callbacks.ready("no engine"));assertTrue(bad.closed);
        assertEquals("read-session/error/null",states.get(states.size()-1));
        ready("retry-session");assertEquals(2,drivers.size());
        main(()->bad.callbacks.ready(null));assertEquals("retry-session/ready/null",states.get(states.size()-1));
    }
    public void testSpeakFailureReleasesPauseAndDisablesSession() {
        FakeDriver driver=ready("read-session");driver.accepted=false;
        main(()->speech.speak("read-session","reply-one","你好"));
        assertFalse(gate.active);assertEquals(1,gate.ends);
        assertEquals("read-session/error/reply-one",states.get(states.size()-1));
    }
    public void testEarlySpeakCancelsPreparationAndIgnoresLateReady() {
        main(()->speech.enable("read-session"));FakeDriver driver=drivers.get(0);
        main(()->speech.speak("read-session","reply-one","尚未准备好"));assertTrue(driver.closed);
        int count=states.size();main(()->{driver.callbacks.preparing("late");driver.callbacks.ready(null);});
        assertEquals(count,states.size());assertTrue(driver.texts.isEmpty());
    }
    public void testDeniedAudioFocusDoesNotStartEngine() {
        FakeDriver driver=ready("read-session");gate.allowed=false;
        main(()->speech.speak("read-session","reply-one","你好"));assertTrue(driver.texts.isEmpty());assertFalse(gate.active);
    }
    public void testSplitPreservesUnicodeAndAllText() {
        String text="语音😀消息。".repeat(99);List<String> chunks=ReplySpeech.splitText(text,32);
        assertEquals(text,String.join("",chunks));
        for(String chunk:chunks){assertTrue(chunk.length()<=32);assertFalse(Character.isHighSurrogate(chunk.charAt(chunk.length()-1)));assertFalse(Character.isLowSurrogate(chunk.charAt(0)));}
    }
    public void testReplacementInvalidatesOldCallbacksWithoutDroppingAudioRoute() {
        FakeDriver driver=ready("read-session");
        main(()->speech.speak("read-session","old-reply","旧回复需要播放很长时间。"));
        String oldPiece=driver.ids.get(0);long oldToken=ReplySpeech.currentPlaybackToken();int stopped=driver.stops;
        main(()->speech.speak("read-session","new-reply","现在应该读这一条。",true));
        assertEquals(stopped+1,driver.stops);assertEquals(1,gate.begins);assertEquals(0,gate.ends);assertTrue(gate.active);
        assertEquals(2,driver.texts.size());long current=ReplySpeech.currentPlaybackToken();assertTrue(current!=0&&current!=oldToken);
        int count=states.size();
        main(()->{driver.callbacks.started(oldPiece);driver.callbacks.progress(oldPiece);driver.callbacks.done(oldPiece);
            driver.callbacks.failed(oldPiece,"过期错误");ReplySpeech.interruptForUserSpeech(oldToken);});
        assertEquals(count,states.size());assertEquals(current,ReplySpeech.currentPlaybackToken());assertTrue(gate.active);
        main(()->driver.callbacks.done(driver.ids.get(1)));
        assertEquals("read-session/ready/new-reply",states.get(states.size()-1));assertEquals(1,gate.ends);
    }
    public void testBargeInStopsOnlyCurrentUtteranceAndKeepsPlaybackEnabled() {
        FakeDriver driver=ready("read-session");
        main(()->speech.speak("read-session","reply-one","可以开口打断这条回复。"));
        String old=driver.ids.get(0);long token=ReplySpeech.currentPlaybackToken();
        main(()->ReplySpeech.interruptForUserSpeech(token));
        assertEquals("read-session/interrupted/reply-one",states.get(states.size()-1));
        assertEquals(0,ReplySpeech.currentPlaybackToken());assertFalse(gate.active);
        int count=states.size();main(()->driver.callbacks.done(old));assertEquals(count,states.size());
        main(()->speech.speak("read-session","reply-two","收到下一条回复后继续朗读。",true));
        assertEquals(2,driver.texts.size());assertTrue(gate.active);
        assertEquals("read-session/speaking/reply-two",states.get(states.size()-1));
    }
    public void testOfflineDriverReceivesWholeReplyForConcurrentSynthesisAndPlayback() {
        main(()->speech.enable("offline-session","melo-zh"));FakeDriver driver=drivers.get(0);driver.limit=16000;
        main(()->driver.callbacks.ready(null));String text="这是需要连续朗读的多句消息。".repeat(250);
        main(()->speech.speak("offline-session","long-reply",text,true));
        assertEquals(1,driver.texts.size());assertEquals(text,driver.texts.get(0));
        main(()->driver.callbacks.done(driver.ids.get(0)));
        assertEquals("offline-session/ready/long-reply",states.get(states.size()-1));
    }
}
