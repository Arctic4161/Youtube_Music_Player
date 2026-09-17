package com.youtubemusicplayer.bridge;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;

import androidx.media3.common.MediaItem;
import androidx.media3.common.C;
import androidx.media3.common.Player;
import androidx.media3.datasource.okhttp.OkHttpDataSource;
import androidx.media3.exoplayer.ExoPlayer;

import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.net.InetSocketAddress;
import java.net.Proxy;
import okhttp3.OkHttpClient;

/**
 * A synchronous-to-Python facade over an ExoPlayer that is always owned by the
 * Android main looper.  It deliberately accepts only transient stream data;
 * no URL or audio is persisted here.
 */
public final class RadioMedia3Player {
    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private ExoPlayer player;
    private OkHttpClient httpClient;

    public RadioMedia3Player(Context context) {
        this.context = context.getApplicationContext();
    }

    private void onMainSync(Runnable work) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            work.run();
            return;
        }
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<RuntimeException> failure = new AtomicReference<>();
        Runnable task = () -> {
            try {
                work.run();
            } catch (RuntimeException error) {
                failure.set(error);
            } finally {
                done.countDown();
            }
        };
        if (!mainHandler.post(task)) {
            throw new IllegalStateException("Media3 main looper is shutting down.");
        }
        try {
            if (!done.await(5, TimeUnit.SECONDS)) {
                mainHandler.removeCallbacks(task);
                throw new IllegalStateException("Timed out waiting for Media3.");
            }
        } catch (InterruptedException error) {
            mainHandler.removeCallbacks(task);
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Interrupted while waiting for Media3.", error);
        }
        if (failure.get() != null) {
            throw failure.get();
        }
    }

    public void load(String url, String userAgent, String referer, String origin, int proxyPort) {
        onMainSync(() -> {
            releaseInternal();
            httpClient = new OkHttpClient.Builder()
                    .proxy(new Proxy(Proxy.Type.HTTP, new InetSocketAddress("127.0.0.1", proxyPort)))
                    .connectTimeout(20, TimeUnit.SECONDS)
                    .readTimeout(20, TimeUnit.SECONDS)
                    .build();
            OkHttpDataSource.Factory http = new OkHttpDataSource.Factory(httpClient);
            if (userAgent != null && !userAgent.isEmpty()) {
                http.setUserAgent(userAgent);
            }
            Map<String, String> headers = new HashMap<>();
            if (referer != null && !referer.isEmpty()) {
                headers.put("Referer", referer);
            }
            if (origin != null && !origin.isEmpty()) {
                headers.put("Origin", origin);
            }
            if (!headers.isEmpty()) {
                http.setDefaultRequestProperties(headers);
            }
            player = new ExoPlayer.Builder(context)
                    .setMediaSourceFactory(
                            new androidx.media3.exoplayer.source.DefaultMediaSourceFactory(http)
                    )
                    .build();
            player.setMediaItem(MediaItem.fromUri(url));
            player.prepare();
        });
    }

    public void play() {
        onMainSync(() -> {
            if (player != null) {
                player.play();
            }
        });
    }

    public void pause() {
        onMainSync(() -> {
            if (player != null) {
                player.pause();
            }
        });
    }

    public void seekSeconds(double seconds) {
        onMainSync(() -> {
            if (player != null) {
                player.seekTo(Math.max(0L, (long) (seconds * 1000.0)));
            }
        });
    }

    public double positionSeconds() {
        double[] result = {0.0};
        onMainSync(() -> {
            if (player != null) {
                result[0] = Math.max(0L, player.getCurrentPosition()) / 1000.0;
            }
        });
        return result[0];
    }

    public double durationSeconds() {
        double[] result = {0.0};
        onMainSync(() -> {
            if (player != null) {
                long duration = player.getDuration();
                if (duration != C.TIME_UNSET) {
                    result[0] = Math.max(0L, duration) / 1000.0;
                }
            }
        });
        return result[0];
    }

    public boolean isPlaybackActive() {
        boolean[] result = {false};
        onMainSync(() -> {
            if (player != null) {
                if (player.getPlayerError() != null) {
                    throw new IllegalStateException("Radio playback failed.");
                }
                int state = player.getPlaybackState();
                result[0] = player.getPlayWhenReady()
                        && (state == Player.STATE_READY || state == Player.STATE_BUFFERING);
            }
        });
        return result[0];
    }

    public void release() {
        onMainSync(this::releaseInternal);
    }

    public float getVolume() {
        float[] result = {1.0f};
        onMainSync(() -> {
            if (player != null) {
                result[0] = player.getVolume();
            }
        });
        return result[0];
    }

    public void setVolume(float volume) {
        onMainSync(() -> {
            if (player != null) {
                player.setVolume(volume);
            }
        });
    }

    private void releaseInternal() {
        if (player != null) {
            player.release();
            player = null;
        }
        if (httpClient != null) {
            httpClient.dispatcher().cancelAll();
            httpClient.connectionPool().evictAll();
            httpClient.dispatcher().executorService().shutdown();
            httpClient = null;
        }
    }
}
