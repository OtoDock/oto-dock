package com.otodock;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import com.capacitorjs.plugins.pushnotifications.PushNotificationsPlugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import java.lang.reflect.Method;
import java.util.Arrays;
import java.util.Set;
import java.util.TreeSet;
import org.junit.Test;

/**
 * A build without a Firebase project runs with a push plugin whose
 * register() refuses, under the plugin's own name, so the dashboard's push
 * setup never reaches FirebaseMessaging there.
 */
public class PushUnconfiguredPluginTest {

    @Test
    public void itStandsInUnderThePluginsOwnNameAndPermission() {
        CapacitorPlugin real = PushNotificationsPlugin.class.getAnnotation(CapacitorPlugin.class);
        CapacitorPlugin stand = PushUnconfiguredPlugin.class.getAnnotation(CapacitorPlugin.class);
        assertNotNull(stand);
        assertEquals(real.name(), stand.name());
        Permission realPermission = real.permissions()[0];
        Permission standPermission = stand.permissions()[0];
        assertEquals(realPermission.alias(), standPermission.alias());
        assertArrayEquals(realPermission.strings(), standPermission.strings());
    }

    @Test
    public void registerAndUnregisterAreItsOwnPluginMethods() throws Exception {
        for (String name : new String[]{"register", "unregister"}) {
            assertNotNull(name, PushUnconfiguredPlugin.class.getDeclaredMethod(name, PluginCall.class)
                    .getAnnotation(PluginMethod.class));
        }
        String source = SourceText.javaSource("PushUnconfiguredPlugin");
        assertTrue(SourceText.body(source, "public void register(").contains("call.reject("));
    }

    /** The plugin's methods the stand-in inherits, none of which reaches
     *  Firebase. A plugin upgrade that adds one fails here, so whoever bumps
     *  it checks the new method and overrides it when it needs Firebase. */
    @Test
    public void everyInheritedPluginMethodStaysAwayFromFirebase() {
        Set<String> inherited = new TreeSet<>();
        for (Method m : PushNotificationsPlugin.class.getDeclaredMethods()) {
            if (m.getAnnotation(PluginMethod.class) != null) inherited.add(m.getName());
        }
        inherited.remove("register");
        inherited.remove("unregister");
        assertEquals(new TreeSet<>(Arrays.asList(
                "checkPermissions", "requestPermissions", "getDeliveredNotifications",
                "removeDeliveredNotifications", "removeAllDeliveredNotifications",
                "createChannel", "deleteChannel", "listChannels")), inherited);
    }

    @Test
    public void theAppRegistersItOnlyWithoutAFirebaseAppAndBeforeTheBridgeIsBuilt() throws Exception {
        String main = SourceText.javaSource("MainActivity");
        String load = SourceText.body(main, "protected void load()");
        int reg = load.indexOf("if (!firebaseConfigured()) registerPlugin(PushUnconfiguredPlugin.class);");
        int sup = load.indexOf("super.load()");
        assertTrue(reg >= 0 && sup > reg);
        assertTrue(SourceText.body(main, "private static boolean firebaseConfigured(")
                .contains("FirebaseApp.getInstance()"));
    }
}
