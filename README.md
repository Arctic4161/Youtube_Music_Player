## About Youtube Music Player

**Youtube Music Player** Is a program built in python 3.10 that searches Youtube, downloads the audio, and plays the audio as a player.
<hr>

## Installation

The installation is pretty easy.
1. Head over to the [Releases](https://github.com/Arctic4161/Youtube_Music_Player/releases) section and download the latest release. Run through the installer.
<hr>

## Dependencies
**Youtube Music Player** has some internal dependencies and external dependencies.
**Internal Dependency** represents *python libraries/modules* the application relies on.
**External Dependency** represents *external programs* the application relies on.<br>

|  Internal Dependency  |  Function  |
|:--:|:--:|
|  kivy  | Core of the GUI |
|  kivymd  |  Material design of the GUI  |
|  pytube  |  Safe file names for files from youtube  |
|  yt-dlp  |  youtube downloader  |
|  kivy-deps.gstreamer  |  Audio Streamer  |
|  youtube-search-python  |  Youtube search function  |

|  External Dependency  |  Function  |
|:--:|:--:|
|  ffmpeg  | requirement of yt-dlp |
|  ffprobe  |  requirement of yt-dlp  |
|  Gstreamer  |  Plays the audio  |

Clone the repository, `cd` into the respective directory and run the below command in terminal
```console
pip install -r requirements.txt
```
to install all the internal dependencies that doesn't come inbuilt with python.
External dependencies will need to be searched for on the web downloaded and installed from their respective sites.
You can download my release to install all together.
<hr>

## A Bit About How It Works
This program can be used to search youtube. Press the next and previous buttons to scroll through the searches. Press the play button to play the audio. The program will proceed to download the program and add it to your play list and then play the audio. After the play button has been pressed after a search, the next and previous buttons will fuction as next and previous for your playlist.
<hr>

**Not to be used with copyrighted material.**
