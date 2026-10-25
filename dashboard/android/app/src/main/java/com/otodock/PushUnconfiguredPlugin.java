package com.otodock;

import android.Manifest;

import com.capacitorjs.plugins.pushnotifications.PushNotificationsPlugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;

/**
 * The push plugin of a build made without a Firebase project (no
 * google-services.json, so no default Firebase app). The plugin's register()
 * reaches FirebaseMessaging, which throws when that app is missing, and
 * Capacitor rethrows a plugin exception on its plugin thread, which ends the
 * app. Here register() refuses and unregister() does nothing, and the
 * dashboard carries on without push; the plugin's other methods never reach
 * Firebase (PushUnconfiguredPluginTest lists them). MainActivity.load()
 * registers it after the plugins of capacitor.plugins.json, so it replaces
 * theirs under the same name; the name and the permission alias are the
 * plugin's own.
 */
@CapacitorPlugin(
    name = "PushNotifications",
    permissions = @Permission(strings = { Manifest.permission.POST_NOTIFICATIONS }, alias = "receive")
)
public class PushUnconfiguredPlugin extends PushNotificationsPlugin {

    static final String NOT_CONFIGURED = "Push is not configured in this build of the app";

    @Override
    @PluginMethod
    public void register(PluginCall call) {
        call.reject(NOT_CONFIGURED);
    }

    @Override
    @PluginMethod
    public void unregister(PluginCall call) {
        call.resolve();
    }
}
