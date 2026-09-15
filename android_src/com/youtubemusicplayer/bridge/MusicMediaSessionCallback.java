package com.youtubemusicplayer.bridge;

import android.media.session.MediaSession;

import java.io.ByteArrayOutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Routes Android media-session controls into the Python service's existing
 * localhost OSC command server.
 */
public final class MusicMediaSessionCallback extends MediaSession.Callback {
    private static final ExecutorService SENDER =
            Executors.newSingleThreadExecutor();

    private final int port;

    public MusicMediaSessionCallback(int port) {
        this.port = port;
    }

    @Override
    public void onPlay() {
        sendCommand(port, "/play", "media_session");
    }

    @Override
    public void onPause() {
        sendCommand(port, "/pause", "media_session");
    }

    @Override
    public void onStop() {
        sendCommand(port, "/stop", "media_session");
    }

    @Override
    public void onSkipToNext() {
        sendCommand(port, "/next", "media_session");
    }

    @Override
    public void onSkipToPrevious() {
        sendCommand(port, "/previous", "media_session");
    }

    @Override
    public void onSeekTo(long positionMs) {
        sendCommand(
                port,
                "/seek_seconds",
                Double.toString(positionMs / 1000.0)
        );
    }

    public static void sendCommand(
            final int port,
            final String route,
            final String value
    ) {
        sendCommand(port, route, value, null);
    }

    public static void sendCommand(
            final int port,
            final String route,
            final String value,
            final Runnable completion
    ) {
        try {
            SENDER.execute(new Runnable() {
                @Override
                public void run() {
                    try (DatagramSocket socket = new DatagramSocket()) {
                        byte[] payload = oscStringPacket(route, value);
                        DatagramPacket packet = new DatagramPacket(
                                payload,
                                payload.length,
                                InetAddress.getByName("127.0.0.1"),
                                port
                        );
                        socket.send(packet);
                    } catch (Exception ignored) {
                        // The Python service may be stopping; media controls must
                        // never crash Android's media-session callback thread.
                    } finally {
                        if (completion != null) {
                            completion.run();
                        }
                    }
                }
            });
        } catch (RuntimeException ignored) {
            if (completion != null) {
                completion.run();
            }
        }
    }

    private static byte[] oscStringPacket(String route, String value)
            throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        writePaddedString(out, route);
        writePaddedString(out, ",s");
        writePaddedString(out, value);
        return out.toByteArray();
    }

    private static void writePaddedString(
            ByteArrayOutputStream out,
            String value
    ) throws Exception {
        byte[] encoded = value.getBytes(StandardCharsets.UTF_8);
        out.write(encoded);
        out.write(0);
        while ((out.size() & 3) != 0) {
            out.write(0);
        }
    }
}
