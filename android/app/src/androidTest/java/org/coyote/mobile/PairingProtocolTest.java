package org.coyote.mobile;

import android.content.ComponentName;
import android.content.Intent;
import android.net.Uri;
import junit.framework.TestCase;
import org.json.JSONObject;

/** No clipboard, application launch, network or device connection. */
public final class PairingProtocolTest extends TestCase {
    static String pair(String socket) {return "https://dungeon-lab.cn/s/?v=1&action=socket&url="+Uri.encode(socket);}
    static final String PUBLIC=pair("wss://trex.dungeon-lab.cn/v4?tid=controller_123");

    public void testOfficialAndConfiguredV4WebsocketPayloads() {
        assertTrue(PairingBridge.validPairUrl(PUBLIC));
        assertTrue(PairingBridge.validPairUrl(pair("ws://192.168.1.50:9999/v4?tid=12345678-abcd")));
        assertTrue(PairingBridge.validPairUrl(pair("wss://relay.example:443/custom/v4?access=encoded%20value&tid=controller_1")));
        assertTrue(PairingBridge.validPairUrl(pair("ws://[2001:db8::1]:9999/v4?tid=test")));
    }

    public void testRejectOuterSpoofDuplicatesAndNonPairingLinks() {
        String[] bad={"javascript:alert(1)","intent://scan#Intent;end",PUBLIC.replace("dungeon-lab.cn","dungeon-lab.cn.evil"),
                PUBLIC.replace("https://","http://"),PUBLIC.replace("https://","https://user@"),PUBLIC+"#fragment",
                PUBLIC+"&action=socket",PUBLIC+"&extra=1",PUBLIC.replace("v=1","v=4"),PUBLIC.replace("/s/","/"),
                PUBLIC.replace("/s/","/s%2f"),PUBLIC.replace("dungeon-lab.cn/","dungeon-lab.cn:444/"),
                "https://dungeon-lab.cn/s/?v=1&action=socket&url=%XX"};
        for(String value:bad) assertFalse(value,PairingBridge.validPairUrl(value));
    }

    public void testRejectMalformedSocketOrAmbiguousTarget() {
        String[] bad={"https://trex.dungeon-lab.cn/v4?tid=a","wss://user:password@host/v4?tid=a",
                "wss://host/v4", "wss://host/v4?tid=", "wss://host/v4?tid=a&tid=b", "wss://host/v4?tid=a&targetId=b",
                "wss://host/v4?tid=a#data", "wss://host:0/v4?tid=a", "wss://host:65536/v4?tid=a",
                "wss://host/v4?tid=hello%0Aworld", "file:///private?tid=a"};
        for(String value:bad) assertFalse(value,PairingBridge.validPairUrl(pair(value)));
    }

    public void testStrictCommandTypesAndFixedLauncher() throws Exception {
        JSONObject command=new JSONObject().put("type","copyAndOpen").put("requestId","pair_test_001").put("pairUrl",PUBLIC);
        assertTrue(PairingBridge.validCommand(command));
        assertTrue(PairingBridge.validCommand(new JSONObject(command.toString()).put("type","copy")));
        assertFalse(PairingBridge.validCommand(new JSONObject(command.toString()).put("package","another.application")));
        assertFalse(PairingBridge.validCommand(new JSONObject(command.toString()).put("type","open")));
        assertFalse(PairingBridge.validCommand(new JSONObject(command.toString()).put("pairUrl",true)));
        assertFalse(PairingBridge.validCommand(new JSONObject(command.toString()).put("requestId","bad")));
        ComponentName target=new ComponentName(PairingBridge.PACKAGE,"com.bjsm.dungeonlabs4.container.JsActivity");
        Intent launch=PairingBridge.launchIntent(target);
        assertEquals(PairingBridge.PACKAGE,launch.getPackage());assertEquals(target,launch.getComponent());
        assertEquals(Intent.ACTION_MAIN,launch.getAction());assertTrue(launch.hasCategory(Intent.CATEGORY_LAUNCHER));
        assertNull(launch.getData());assertNull(launch.getSelector());
        try {PairingBridge.launchIntent(new ComponentName("another.app","another.app.Main"));fail("Foreign package accepted");}
        catch(IllegalArgumentException expected) { }
    }
}
