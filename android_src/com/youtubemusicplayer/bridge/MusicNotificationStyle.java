package com.youtubemusicplayer.bridge;

import android.app.Notification;
import android.media.session.MediaSession;

/**
 * Applies the media notification style in Java so Pyjnius does not need to
 * resolve framework inheritance or the int-array vararg at runtime.
 */
public final class MusicNotificationStyle {
    private MusicNotificationStyle() {
    }

    public static void applyMediaStyle(
            Notification.Builder builder,
            MediaSession session
    ) {
        if (builder == null || session == null) {
            return;
        }
        Notification.MediaStyle style = new Notification.MediaStyle()
                .setMediaSession(session.getSessionToken())
                .setShowActionsInCompactView(0, 1, 2);
        builder.setStyle(style);
    }
}
