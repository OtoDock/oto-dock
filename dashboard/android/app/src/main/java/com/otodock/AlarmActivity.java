package com.otodock;

import android.app.Activity;
import android.app.KeyguardManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowManager;
import android.widget.TextView;

/**
 * Lightweight full-screen alarm UI shown OVER the lock screen for danger alarms.
 * Launched by AlarmService's full-screen intent. Deliberately a plain Activity
 * (no WebView / dashboard) so nothing sensitive is exposed on the keyguard — the
 * dashboard only opens after the user unlocks. Closes itself when the alarm stops
 * (button, notification action, app open, or timeout) via ACTION_ALARM_STOPPED.
 */
public class AlarmActivity extends Activity {

    /** Set by NotificationService's lock-screen alarm: the alarm's service
     *  could not start from the background, so this activity starts it once
     *  it is in the foreground. */
    static final String EXTRA_START_ALARM = "start_alarm";

    private boolean startAlarmOnResume = false;

    private final BroadcastReceiver stopReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            finish();
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Show over the keyguard + turn the screen on (so the alarm is visible
        // while locked) — but the dashboard itself never shows on the lock screen.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        } else {
            getWindow().addFlags(
                    WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                            | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                            | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }

        setContentView(R.layout.activity_alarm);
        getWindow().setStatusBarColor(0xFFDA3536);

        Intent intent = getIntent();
        startAlarmOnResume = savedInstanceState == null && intent != null
                && intent.getBooleanExtra(EXTRA_START_ALARM, false);
        String title = intent != null ? intent.getStringExtra("title") : null;
        String body = intent != null ? intent.getStringExtra("body") : null;
        final String clickUrl = intent != null ? intent.getStringExtra("click_url") : null;
        final String messageId = intent != null ? intent.getStringExtra("google.message_id") : null;
        final String installId = intent != null ? intent.getStringExtra("install_id") : null;

        ((TextView) findViewById(R.id.alarm_title)).setText(title != null ? title : "Critical Alert");
        ((TextView) findViewById(R.id.alarm_body)).setText(body != null ? body : "");

        // Multi-installation: show which server raised the alarm (when known).
        String installLabel = intent != null ? intent.getStringExtra("install_label") : null;
        TextView installView = (TextView) findViewById(R.id.alarm_install);
        if (installLabel != null && !installLabel.isEmpty()) {
            installView.setText(installLabel);
            installView.setVisibility(android.view.View.VISIBLE);
        }

        findViewById(R.id.alarm_dismiss).setOnClickListener(v -> {
            stopAlarm();
            finish();
        });
        findViewById(R.id.alarm_open).setOnClickListener(v -> {
            stopAlarm();
            openApp(clickUrl, messageId, installId);
            finish();
        });

        IntentFilter filter = new IntentFilter(AlarmService.ACTION_ALARM_STOPPED);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(stopReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(stopReceiver, filter);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (intent.getBooleanExtra(EXTRA_START_ALARM, false)) startAlarmOnResume = true;
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!startAlarmOnResume) return;
        startAlarmOnResume = false;
        Intent start = new Intent(this, AlarmService.class);
        start.putExtras(getIntent());
        start.removeExtra(EXTRA_START_ALARM);
        try {
            startForegroundService(start);
        } catch (Exception ignored) {
            // The notification stays; the person still sees the alert.
        }
    }

    private void stopAlarm() {
        Intent stop = new Intent(this, AlarmService.class);
        stop.setAction(AlarmService.ACTION_DISMISS);
        startService(stop);
    }

    private void openApp(String clickUrl, String messageId, String installId) {
        // Ask the keyguard to show its unlock prompt, then open the dashboard —
        // which is NOT shown over the lock screen, so it appears after unlock.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            KeyguardManager km = (KeyguardManager) getSystemService(Context.KEYGUARD_SERVICE);
            if (km != null) km.requestDismissKeyguard(this, null);
        }
        Intent open = getPackageManager().getLaunchIntentForPackage(getPackageName());
        if (open == null) return;
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        open.putExtra("dismiss_alarm", true);
        if (clickUrl != null) open.putExtra("click_url", clickUrl);
        if (messageId != null) open.putExtra("google.message_id", messageId);
        if (installId != null) open.putExtra("install_id", installId);
        startActivity(open);
    }

    @Override
    protected void onDestroy() {
        try {
            unregisterReceiver(stopReceiver);
        } catch (Exception ignored) {}
        super.onDestroy();
    }
}
