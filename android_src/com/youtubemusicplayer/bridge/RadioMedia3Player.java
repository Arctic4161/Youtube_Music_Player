package com.youtubemusicplayer.bridge;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import androidx.media3.common.MediaItem;
import androidx.media3.common.C;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.datasource.okhttp.OkHttpDataSource;
import androidx.media3.datasource.HttpDataSource;
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
            player.addListener(new Player.Listener() {
                @Override
                public void onEvents(Player source, Player.Events events) {
                    if (events.contains(Player.EVENT_PLAYBACK_STATE_CHANGED)
                            || events.contains(Player.EVENT_IS_PLAYING_CHANGED)
                            || events.contains(Player.EVENT_PLAYER_ERROR)) {
                        Log.i("YMPRadio", diagnosticInternal());
                    }
                }
            });
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

    public String playbackState() {
        String[] result = {"stop"};
        onMainSync(() -> {
            if (player != null) {
                PlaybackException error = player.getPlayerError();
                if (error != null) {
                    // Error-code names are stable diagnostic information and do
                    // not expose the signed, transient YouTube stream URL.
                    throw new IllegalStateException(
                            "Media3 " + PlaybackException.getErrorCodeName(error.errorCode));
                }
                int state = player.getPlaybackState();
                if (player.isPlaying()) {
                    result[0] = "play";
                } else if (player.getPlayWhenReady()
                        && (state == Player.STATE_READY || state == Player.STATE_BUFFERING)) {
                    // Buffering or suppressed playback must not look like either
                    // audible playback or EOF to the Python service.
                    result[0] = "loading";
                }
            }
        });
        return result[0];
    }

    public String playbackStatus() {
        String[] result = {"idle"};
        onMainSync(() -> {
            if (player == null) return;
            if (player.getPlayerError() != null) {
                throw new IllegalStateException(diagnosticInternal());
            }
            if (player.getPlaybackState() == Player.STATE_ENDED) result[0] = "ended";
            else if (!player.getPlayWhenReady()) result[0] = "paused";
            else result[0] = player.isPlaying() ? "playing" : "buffering";
        });
        return result[0];
    }

    public String diagnostic() {
        String[] result = {"released"};
        onMainSync(() -> result[0] = diagnosticInternal());
        return result[0];
    }

    private String diagnosticInternal() {
        if (player == null) return "released";
        String result = "state=" + player.getPlaybackState()
                + "; playing=" + player.isPlaying()
                + "; playWhenReady=" + player.getPlayWhenReady()
                + "; suppression=" + player.getPlaybackSuppressionReason()
                + "; positionMs=" + player.getCurrentPosition()
                + "; bufferedMs=" + player.getTotalBufferedDuration();
        PlaybackException error = player.getPlayerError();
        if (error != null) {
            result += "; error=" + PlaybackException.getErrorCodeName(error.errorCode);
            Throwable cause = error.getCause();
            for (int depth = 0; cause != null && depth < 5; depth++) {
                // Never include exception messages, headers or signed stream URLs.
                result += "; cause=" + cause.getClass().getSimpleName();
                if (cause instanceof HttpDataSource.InvalidResponseCodeException) {
                    result += "; http="
                            + ((HttpDataSource.InvalidResponseCodeException) cause).responseCode;
                }
                cause = cause.getCause();
            }
        }
        return result;
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
