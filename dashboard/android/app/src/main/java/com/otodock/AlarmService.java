package com.otodock;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Log;

import androidx.core.app.NotificationCompat;

import java.util.Locale;

/**
 * Foreground service for danger-level alarm notifications.
 * Loops: alarm sound -> TTS -> alarm sound -> TTS until dismissed.
 * Auto-stops after 5 minutes.
 */
public class AlarmService extends Service {
    private static final String TAG = "OtoDockAlarm";
    // Shared with NotificationService's lock-screen alarm, which this service's
    // notification replaces once the service runs.
    static final int NOTIFICATION_ID = 99999;
    static final long TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
    private static final long PAUSE_BETWEEN_MS = 2000; // 2s pause between alarm and TTS

    public static final String ACTION_DISMISS = "com.otodock.DISMISS_ALARM";
    public static final String ACTION_ALARM_STOPPED = "com.otodock.ALARM_STOPPED";

    private MediaPlayer mediaPlayer;
    private TextToSpeech tts;
    private Handler handler;
    private PowerManager.WakeLock wakeLock;
    private boolean running = false;
    private boolean ttsReady = false;
    private String alarmTitle = "";
    private String alarmBody = "";
    private String clickUrl = "/";
    private String installId = "";
    private String installLabel = null;
    private String googleMessageId = null;

    @Override
    public void onCreate() {
        super.onCreate();
        handler = new Handler(Looper.getMainLooper());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }

        // Check for dismiss action
        if (ACTION_DISMISS.equals(intent.getAction())) {
            Log.d(TAG, "Alarm dismissed by user");
            stopAlarm();
            return START_NOT_STICKY;
        }

        alarmTitle = intent.getStringExtra("title");
        alarmBody = intent.getStringExtra("body");
        clickUrl = intent.getStringExtra("click_url");
        installId = intent.getStringExtra("install_id");
        installLabel = intent.getStringExtra("install_label");
        googleMessageId = intent.getStringExtra("google.message_id");
        if (alarmTitle == null) alarmTitle = "Critical Alert";
        if (alarmBody == null) alarmBody = "";
        if (clickUrl == null) clickUrl = "/";
        if (installId == null) installId = "";

        // Hold a partial wake lock so the alarm + TTS loop keeps running while
        // the screen is off / the device is dozing. Without it the CPU suspends
        // between loop steps and the Handler callbacks + TTS stall until the user
        // manually wakes the phone (alarm plays once, then no loop / no TTS).
        acquireWakeLock();

        // Show foreground notification (Dismiss action + full-screen intent that
        // turns the screen on when locked).
        startForeground(NOTIFICATION_ID, buildNotification());

        running = true;

        // Initialize TTS asynchronously; the loop's TTS step checks ttsReady.
        tts = new TextToSpeech(this, status -> {
            if (status == TextToSpeech.SUCCESS) {
                tts.setLanguage(Locale.US);
                ttsReady = true;
                tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                    @Override
                    public void onStart(String utteranceId) {}

                    @Override
                    public void onDone(String utteranceId) {
                        // After TTS finishes, wait then play alarm again
                        if (running) {
                            handler.postDelayed(() -> playAlarmSound(), PAUSE_BETWEEN_MS);
                        }
                    }

                    @Override
                    public void onError(String utteranceId) {
                        if (running) {
                            handler.postDelayed(() -> playAlarmSound(), PAUSE_BETWEEN_MS);
                        }
                    }
                });
            }
        });

        // Kick the alarm loop off immediately — don't wait for TTS init (which is
        // slow / deferred with the screen off). The TTS step no-ops until ttsReady,
        // looping the siren in the meantime.
        playAlarmSound();

        // Safety timeout
        handler.postDelayed(this::stopAlarm, TIMEOUT_MS);

        return START_NOT_STICKY;
    }

    private Notification buildNotification() {
        // Dismiss action button
        Intent dismissIntent = new Intent(this, AlarmService.class);
        dismissIntent.setAction(ACTION_DISMISS);
        PendingIntent dismissPending = PendingIntent.getService(
                this, 0, dismissIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        // Open app intent — include deep link data for Capacitor navigation
        Intent openIntent = getPackageManager().getLaunchIntentForPackage(getPackageName());
        if (openIntent == null) openIntent = new Intent();
        openIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        openIntent.putExtra("dismiss_alarm", true);
        openIntent.putExtra("click_url", clickUrl);
        openIntent.putExtra("install_id", installId);
        if (googleMessageId != null) {
            openIntent.putExtra("google.message_id", googleMessageId);
        }
        PendingIntent openPending = PendingIntent.getActivity(
                this, 1, openIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);

        // Full-screen intent \u2014 when the device is locked / screen-off the system
        // launches this (turning the screen on) instead of posting a quiet
        // heads-up. Targets the lightweight AlarmActivity (shown OVER the lock
        // screen) \u2014 NOT the dashboard, which must stay behind the keyguard.
        Intent fsIntent = new Intent(this, AlarmActivity.class);
        fsIntent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        fsIntent.putExtra("title", alarmTitle);
        fsIntent.putExtra("body", alarmBody);
        fsIntent.putExtra("click_url", clickUrl);
        fsIntent.putExtra("install_id", installId);
        if (installLabel != null) fsIntent.putExtra("install_label", installLabel);
        if (googleMessageId != null) {
            fsIntent.putExtra("google.message_id", googleMessageId);
        }
        PendingIntent fsPending = PendingIntent.getActivity(
                this, 2, fsIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        // Multi-installation: prefix the title with the source server's name (the
        // lock-screen AlarmActivity also shows it in its own label line).
        String headerTitle = (installLabel != null ? installLabel + " \u00b7 " : "") + "\ud83d\udea8 " + alarmTitle;

        return new NotificationCompat.Builder(this, "notif_danger")
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle(headerTitle)
                .setContentText(alarmBody)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(alarmBody))
                .setColor(0xFFDA3536)
                .setColorized(true)
                .setOngoing(true)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                // The notification's own lock-screen version hides the text when
                // the phone hides sensitive content; AlarmActivity (the full-screen
                // alarm over the keyguard) shows and speaks it by design.
                .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
                .setPublicVersion(new NotificationCompat.Builder(this, "notif_danger")
                        .setSmallIcon(R.drawable.ic_stat)
                        .setContentTitle("OtoDock")
                        .setContentText("Alarm")
                        .build())
                .setContentIntent(openPending)
                .setFullScreenIntent(fsPending, true)
                .addAction(android.R.drawable.ic_delete, "Dismiss Alarm", dismissPending)
                .setDeleteIntent(dismissPending)
                .build();
    }

    private void playAlarmSound() {
        if (!running) return;

        try {
            if (mediaPlayer != null) {
                mediaPlayer.release();
            }

            mediaPlayer = new MediaPlayer();
            mediaPlayer.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build());
            mediaPlayer.setDataSource(this,
                    Uri.parse("android.resource://" + getPackageName() + "/raw/notif_danger"));
            mediaPlayer.prepare();
            mediaPlayer.setOnCompletionListener(mp -> {
                // After alarm sound finishes, speak TTS
                if (running) {
                    handler.postDelayed(this::speakTts, PAUSE_BETWEEN_MS);
                }
            });
            mediaPlayer.start();
        } catch (Exception e) {
            Log.e(TAG, "Error playing alarm: " + e.getMessage());
            // Try TTS anyway
            if (running) {
                handler.postDelayed(this::speakTts, PAUSE_BETWEEN_MS);
            }
        }
    }

    private void speakTts() {
        if (!running) return;

        if (ttsReady && tts != null) {
            String text = alarmTitle + ". " + alarmBody;
            tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, "alarm_tts");
            // onDone callback in UtteranceProgressListener will trigger next alarm
        } else {
            // TTS not ready, just loop alarm again
            if (running) {
                handler.postDelayed(this::playAlarmSound, PAUSE_BETWEEN_MS);
            }
        }
    }

    private void stopAlarm() {
        running = false;
        handler.removeCallbacksAndMessages(null);

        if (mediaPlayer != null) {
            try {
                mediaPlayer.stop();
                mediaPlayer.release();
            } catch (Exception ignored) {}
            mediaPlayer = null;
        }

        if (tts != null) {
            tts.stop();
            tts.shutdown();
            tts = null;
        }

        // Remove notification
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.cancel(NOTIFICATION_ID);
        }

        releaseWakeLock();
        // Close the lock-screen AlarmActivity (if showing).
        sendBroadcast(new Intent(ACTION_ALARM_STOPPED).setPackage(getPackageName()));
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
        Log.d(TAG, "Alarm stopped");
    }

    private void acquireWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) return;
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "OtoDock::Alarm");
                // Bounded safety net just past the 5-min auto-stop so a leaked
                // lock can never outlive the alarm.
                wakeLock.acquire(TIMEOUT_MS + 30_000L);
            }
        } catch (Exception e) {
            Log.w(TAG, "Wake lock acquire failed: " + e.getMessage());
        }
    }

    private void releaseWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Exception ignored) {}
        wakeLock = null;
    }

    @Override
    public void onDestroy() {
        stopAlarm();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
