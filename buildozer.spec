[app]
title = Youtube Music Player
package.name = youtubemusicplayer
package.domain = com.youtubemusicplayer
version = 2.0.5
android.numeric_version = 20005
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

source.include_patterns = ./service/main.py, ./service/download_service.py, playlist_manager.py, musicapp.kv, library_tab.kv, utils.py, download_config.py, download_state.py, playback_logic.py, radio_logic.py, radio_catalog.py, radio_player.py, radio_proxy.py, service_lifecycle.py, timer_lifecycle.py, media_identity.py, search_logic.py, youtube_search_compat.py, ui_scaling.py

# Your main script
entrypoint = main.py

# Kivy stack + your Python deps
# Note: git URLs generally work with p4a/pip. If it errors, we can pin with PEP 508 "name @ git+..." syntax.
requirements = python3==3.13.7,hostpython3==3.13.7,kivy==2.3.1,kivymd==1.2.0,pyjnius,requests==2.32.5,httpx==0.28.1,httpcore==1.0.9,h11==0.16.0,anyio==4.10.0,certifi==2025.8.3,charset-normalizer==2.1.1,sniffio==1.3.1,idna==3.10,urllib3==2.8.0,git+https://github.com/Arctic4161/youtube-search-python.git@73e7c725a1c3fd5204cd52afedbdbaf89bf2bc35,yt-dlp==2026.8.30.232658.dev0,oscpy==0.6.0,androidstorage4kivy==0.1.1,Pillow==11.3.0,mutagen==1.48.1

# Android SDK targets (adjust if Gradle/p4a suggests otherwise)
android.api = 36
android.minapi = 28
android.ndk = 28c
android.ndk_api = 28
android.archs = arm64-v8a,armeabi-v7a
android.release_artifact = apk

android.permissions = INTERNET, FOREGROUND_SERVICE, WAKE_LOCK, POST_NOTIFICATIONS, FOREGROUND_SERVICE_MEDIA_PLAYBACK, FOREGROUND_SERVICE_DATA_SYNC

# SDL/Kivy dialogs currently receive Back through legacy key events. Preserve
# that route on Android 16 until the bootstrap supports predictive Back.
android.extra_manifest_application_arguments = android_src/application_attributes.xml

# python-for-android generates ServiceMusicservice and writes this foreground
# service type into its manifest declaration (required for media playback on
# current Android versions).
services = musicservice:service/main.py:foreground:foregroundServiceType=mediaPlayback,downloadservice:service/download_service.py:foreground:sticky:foregroundServiceType=dataSync

# Icon / Presplash (optional)
icon.filename = music.png
# presplash.filename = music.png

# Orientation (optional)
orientation = portrait

# Use the modern toolkit
android.enable_androidx = True

# Keep the existing AndroidX dependency available to the packaged application.
android.gradle_dependencies = androidx.core:core:1.9.0,androidx.media3:media3-exoplayer:1.5.1,androidx.media3:media3-datasource-okhttp:1.5.1
android.add_src = android_src

p4a.branch = v2026.05.09
p4a.commit = 8aba7685beea080d0e34375e6c0e2067a2dcad0a

[buildozer]
log_level = 2
warn_on_root = 1
# Build dir cache lives in ~/.buildozer (default, recommended)
