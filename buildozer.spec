[app]
title = Youtube Music Player
package.name = youtubemusicplayer
package.domain = com.youtubemusicplayer
version = 1.6.0
android.numeric_version = 10600
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json

source.include_patterns = ./service/main.py, playlist_manager.py, musicapp.kv, library_tab.kv, utils.py

# Your main script
entrypoint = main.py

# Kivy stack + your Python deps
# Note: git URLs generally work with p4a/pip. If it errors, we can pin with PEP 508 "name @ git+..." syntax.
requirements = python3,kivy,kivymd==1.2.0,pyjnius,cython,requests==2.32.5,httpx==0.17.1,httpcore==0.12.3,h11==0.12.0,rfc3986==1.5.0,sniffio==1.3.0,idna==3.4,git+https://github.com/Arctic4161/youtube-search-python.git,yt-dlp,oscpy,androidstorage4kivy,Pillow,mutagen

# Android SDK targets (adjust if Gradle/p4a suggests otherwise)
android.api = 33
android.minapi = 28
android.ndk_api = 28
android.archs = arm64-v8a,armeabi-v7a

android.permissions = INTERNET, FOREGROUND_SERVICE, WAKE_LOCK, READ_MEDIA_AUDIO, READ_EXTERNAL_STORAGE, POST_NOTIFICATIONS, FOREGROUND_SERVICE_MEDIA_PLAYBACK

#Will have to manually set this in biuldozer templates. It does not set the foregroundService type and android will kill it.
services = musicservice:service/main.py:foreground
android.foreground_service_types = mediaPlayback
android.add_manifest_xml = """
<manifest xmlns:tools="http://schemas.android.com/tools">
  <application>
    <service
        android:name="com.youtubemusicplayer.youtubemusicplayer.ServiceMusicservice"
        android:enabled="true"
        android:exported="false"
        android:stopWithTask="true"
        tools:replace="android:foregroundServiceType,android:exported,android:stopWithTask"
        android:foregroundServiceType="mediaPlayback" />
  </application>
</manifest>
"""

# Icon / Presplash (optional)
icon.filename = music.png
# presplash.filename = music.png

# Orientation (optional)
orientation = portrait

# Use the modern toolkit
android.enable_androidx = True

# 2) Add dependencies to both the app and the service
android.gradle_dependencies = androidx.core:core:1.9.0
android.service.gradle_dependencies = androidx.core:core:1.9.0

p4a.branch = develop

[buildozer]
log_level = 2
warn_on_root = 1
# Build dir cache lives in ~/.buildozer (default, recommended)