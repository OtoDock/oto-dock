package com.otodock;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.util.Log;

import androidx.core.app.NotificationCompat;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import java.util.Map;

/**
 * Custom FCM service that handles data-only messages.
 * - Shows notification with proper channel/sound
 * - Starts AlarmService for danger severity (loops alarm + TTS)
 */
public class NotificationService extends FirebaseMessagingService {
    private static final String TAG = "OtoDockNotif";

    /** Notification tag on every end-of-turn (ephemeral) entry, so the
     *  foreground sweep in MainActivity clears exactly these and nothing else
     *  (severity notifications and the alarm are posted untagged). */
    public static final String TURN_COMPLETE_TAG = "otodock_turn_complete";
    @Override
    public void onMessageReceived(RemoteMessage remoteMessage) {
        Map<String, String> data = remoteMessage.getData();
        if (data.isEmpty()) return;

        String title = data.getOrDefault("title", "OtoDock");
        String body = data.getOrDefault("body", "");
        String severity = data.getOrDefault("severity", "info");
        String deliveryId = data.getOrDefault("delivery_id", String.valueOf(System.currentTimeMillis()));
        boolean ephemeral = "true".equals(data.getOrDefault("ephemeral", "false"));

        Log.d(TAG, "Received: severity=" + severity + ", ephemeral=" + ephemeral + ", title=" + title);

        // When the app holds more than one installation, label the notification with
        // the source server's name (null when single-install or the source is unknown).
        String installLabel = resolveInstallLabel(data.getOrDefault("install_id", ""));

        if (ephemeral) {
            handleEphemeral(installLabel, title, body, data, remoteMessage.getMessageId());
            return;
        }

        if ("danger".equals(severity)) {
            // Start foreground alarm service (handles its own notification + TTS loop)
            Intent alarmIntent = new Intent(this, AlarmService.class);
            alarmIntent.putExtra("title", title);
            alarmIntent.putExtra("body", body);
            alarmIntent.putExtra("delivery_id", deliveryId);
            alarmIntent.putExtra("click_url", data.getOrDefault("click_url", "/"));
            alarmIntent.putExtra("install_id", data.getOrDefault("install_id", ""));
            if (installLabel != null) alarmIntent.putExtra("install_label", installLabel);
            String msgId = remoteMessage.getMessageId();
            if (msgId != null) alarmIntent.putExtra("google.message_id", msgId);
            // Only a high-priority message may start a foreground service from
            // the background; Android downgrades it for an app in the frequent
            // standby bucket or lower (an app opened rarely, not exempt from
            // battery optimisation). Then the lock-screen alarm starts the
            // service from its activity instead.
            if (remoteMessage.getPriority() == RemoteMessage.PRIORITY_HIGH) {
                try {
                    startForegroundService(alarmIntent);
                    return;
                } catch (IllegalStateException e) {
                    Log.w(TAG, "Alarm service not started from the background: " + e.getClass().getSimpleName());
                }
            } else {
                Log.w(TAG, "Danger message at priority " + remoteMessage.getPriority()
                        + " (sent at " + remoteMessage.getOriginalPriority() + ")");
            }
            showLockScreenAlarm(alarmIntent);
        } else {
            // Show regular notification (channel handles sound + vibration)
            showNotification(title, body, severity, deliveryId, data, remoteMessage.getMessageId(), installLabel);
        }
    }

    /** The source installation's display name, to show on a notification — only when
     *  the app holds more than one install and the source install_id is bound/known.
     *  Returns null otherwise (single-install, or an unidentified/old-proxy source). */
    private String resolveInstallLabel(String installId) {
        try {
            InstallStore store = new InstallStore(this);
            if (store.list().size() <= 1) return null;
            if (installId == null || installId.isEmpty()) return null;
            InstallStore.Install src = store.byInstallId(installId);
            return src != null ? src.label : null;
        } catch (Exception e) {
            return null;
        }
    }

    private void showNotification(String title, String body, String severity, String deliveryId) {
        showNotification(title, body, severity, deliveryId, null, null, null);
    }

    private void showNotification(String title, String body, String severity, String deliveryId, Map<String, String> fcmData, String messageId, String installLabel) {
        String channelId = "notif_" + severity;
        int notifId = deliveryId.hashCode();

        // Intent to open app when tapped — include FCM data so Capacitor's
        // pushNotificationActionPerformed can read click_url for deep linking
        Intent intent = getPackageManager().getLaunchIntentForPackage(getPackageName());
        if (intent == null) intent = new Intent();
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        // Include google.message_id so Capacitor's PushNotifications plugin
        // recognizes this as a push notification tap and fires
        // pushNotificationActionPerformed in JS with the data extras.
        if (messageId != null) {
            intent.putExtra("google.message_id", messageId);
        }
        if (fcmData != null) {
            for (Map.Entry<String, String> entry : fcmData.entrySet()) {
                intent.putExtra(entry.getKey(), entry.getValue());
            }
        }
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this, notifId, intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);

        // Severity-specific colors
        int color;
        switch (severity) {
            case "success": color = Color.parseColor("#0d9488"); break;  // teal
            case "warning": color = Color.parseColor("#f4b206"); break;  // amber
            case "danger":  color = Color.parseColor("#da3536"); break;  // red
            default:        color = Color.parseColor("#146bb5"); break;  // brand blue
        }

        // Severity label prefix for clarity
        String prefix;
        switch (severity) {
            case "warning": prefix = "\u26a0\ufe0f "; break;  // ⚠️
            case "danger":  prefix = "\ud83d\udea8 "; break;   // 🚨
            case "success": prefix = "\u2705 "; break;         // ✅
            default:        prefix = ""; break;
        }

        // Multi-installation: prefix the title with the source server's name (a
        // reliably-visible "header"; setSubText is hidden by some OEMs / heads-up).
        String headerTitle = (installLabel != null ? installLabel + " · " : "") + prefix + title;

        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, channelId)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle(headerTitle)
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setColor(color)
                .setColorized("danger".equals(severity))
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .setPublicVersion(lockScreenVersion(channelId))
                .setContentIntent(pendingIntent);

        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.notify(notifId, builder.build());
        }
    }

    /** The alarm when its service cannot start from the background: a
     *  full-screen notification on the danger channel (it bypasses Do Not
     *  Disturb and vibrates) that opens AlarmActivity over the lock screen, or
     *  as a heads-up on an unlocked phone. AlarmActivity, in the foreground,
     *  starts AlarmService, whose notification replaces this one (same id). */
    private void showLockScreenAlarm(Intent alarm) {
        Intent open = new Intent(this, AlarmActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        open.putExtras(alarm);
        open.putExtra(AlarmActivity.EXTRA_START_ALARM, true);
        PendingIntent pending = PendingIntent.getActivity(
                this, 3, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String label = alarm.getStringExtra("install_label");
        String title = alarm.getStringExtra("title");
        String body = alarm.getStringExtra("body");
        NotificationCompat.Builder builder = new NotificationCompat.Builder(this, "notif_danger")
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle((label != null ? label + " · " : "") + "\ud83d\udea8 " + title)
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setColor(Color.parseColor("#da3536"))
                .setColorized(true)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .setPublicVersion(lockScreenVersion("notif_danger"))
                .setContentIntent(pending)
                .setFullScreenIntent(pending, true)
                .setAutoCancel(true)
                .setTimeoutAfter(AlarmService.TIMEOUT_MS);
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(AlarmService.NOTIFICATION_ID, builder.build());
    }

    /** What a secure lock screen shows when the phone hides sensitive content:
     *  that something arrived, never its title or text (those show after unlock). */
    private android.app.Notification lockScreenVersion(String channelId) {
        return new NotificationCompat.Builder(this, channelId)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("OtoDock")
                .setContentText("New notification")
                .build();
    }

    /**
     * Handle the end-of-turn (ephemeral) turn-complete signal.
     *
     * Uses the notif_ephemeral channel (IMPORTANCE_DEFAULT) which plays sound + vibration
     * through the OS notification system — reliable even when the app is backgrounded
     * on aggressive OEMs (Honor/Huawei). IMPORTANCE_DEFAULT means NO heads-up banner.
     * Entries stay visible in the shade (each turn end posts its own, so they pile up
     * while the user is away) and are swept in one go when the app next comes to the
     * foreground — see MainActivity's TURN_COMPLETE_TAG sweep. Tapping one opens the
     * app, which triggers that same sweep.
     */
    private void handleEphemeral(String installLabel, String title, String body,
                                 Map<String, String> fcmData, String messageId) {
        int notifId = (int) (System.currentTimeMillis() % Integer.MAX_VALUE);

        try {
            Intent intent = getPackageManager().getLaunchIntentForPackage(getPackageName());
            if (intent == null) intent = new Intent();
            intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            // Mirror showNotification: google.message_id + the data extras make
            // Capacitor fire pushNotificationActionPerformed on tap, so the SPA
            // can deep-link to the finished chat (click_url). Without them the
            // tap only foregrounded the app on whatever page it already showed.
            if (messageId != null) {
                intent.putExtra("google.message_id", messageId);
            }
            if (fcmData != null) {
                for (Map.Entry<String, String> entry : fcmData.entrySet()) {
                    intent.putExtra(entry.getKey(), entry.getValue());
                }
            }
            PendingIntent pendingIntent = PendingIntent.getActivity(
                    this, notifId, intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);

            NotificationCompat.Builder builder = new NotificationCompat.Builder(this, "notif_ephemeral")
                    .setSmallIcon(R.drawable.ic_stat)
                    .setContentTitle((installLabel != null ? installLabel + " · " : "") + title)
                    .setContentText(body)
                    .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                    .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                    .setPublicVersion(lockScreenVersion("notif_ephemeral"))
                    .setContentIntent(pendingIntent)
                    .setAutoCancel(true);

            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.notify(TURN_COMPLETE_TAG, notifId, builder.build());
            }
        } catch (Exception e) {
            Log.w(TAG, "Ephemeral notification failed: " + e.getMessage());
        }
    }

    @Override
    public void onNewToken(String token) {
        Log.d(TAG, "FCM token refreshed: " + token.substring(0, 20) + "...");
        // Token will be re-registered on next app open via Capacitor PushNotifications plugin
    }
}
