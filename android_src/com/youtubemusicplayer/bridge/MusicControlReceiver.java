package com.youtubemusicplayer.bridge;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Handles only explicit notification actions sent by this application. */
public final class MusicControlReceiver extends BroadcastReceiver {
    public static final String ACTION_PREVIOUS =
            "com.youtubemusicplayer.action.PREVIOUS";
    public static final String ACTION_TOGGLE =
            "com.youtubemusicplayer.action.TOGGLE";
    public static final String ACTION_NEXT =
            "com.youtubemusicplayer.action.NEXT";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) {
            return;
        }
        String route;
        String action = intent.getAction();
        if (ACTION_PREVIOUS.equals(action)) {
            route = "/previous";
        } else if (ACTION_TOGGLE.equals(action)) {
            route = "/toggle";
        } else if (ACTION_NEXT.equals(action)) {
            route = "/next";
        } else {
            return;
        }
        final PendingResult pendingResult = goAsync();
        MusicMediaSessionCallback.sendCommand(
                3000,
                route,
                "notification",
                new Runnable() {
                    @Override
                    public void run() {
                        pendingResult.finish();
                    }
                }
        );
    }
}
