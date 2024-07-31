## About Youtube Music Player

**Youtube Music Player** Is a program built in python 3.10 that searches Youtube, downloads the audio, and plays the audio as a player.
<hr>

## Installation

The installation is pretty easy.
1. Head over to the [Releases](https://github.com/Arctic4161/Youtube_Music_Player/releases) section and download the latest release and run the installer.
<hr>

## Dependencies
**Youtube Music Player** has some internal dependencies and external dependencies.
**Internal Dependency** represents *python libraries/modules* the application relies on.
<br>

|  Internal Dependency  |  Function  |
|:--:|:--:|
|  kivy  | Core of the GUI |
|  kivymd  |  Material design of the GUI  |
|  pytube  |  Safe file names for files from youtube  |
|  yt-dlp  |  youtube downloader  |
|  kivy-deps.gstreamer  |  Audio Streamer  |
|  youtube-search-python  |  Youtube search function  |
|  audio_extract  | Extract the audio from mp4 |

Clone the repository, `cd` into the respective directory and run the below command in terminal
```console
pip install -r requirements.txt
```
to install all the internal dependencies that doesn't come inbuilt with python.

This program requires a custom audio_extract library. If manually installing dependecies you can install the custom 
dependency using the command below.

```console
pip install git+https://github.com/Arctic4161/audio-extract.git
```

<hr>

## A Bit About How It Works
This program can be used to search youtube. Press the next and previous buttons to scroll through the searches. Press the play button to play the audio. The program will proceed to download the program and add it to your play list and then play the audio. After the play button has been pressed after a search, the next and previous buttons will fuction as next and previous for your playlist.
<hr>

## Screenshots

<div align="center">
  <img src="https://github.com/Arctic4161/Youtube_Music_Player/blob/master/Images/2024-07-19_14-07.png?raw=true"
  title="Youtube Music Player">
</div>
<div align="center">
  <img src="https://github.com/Arctic4161/Youtube_Music_Player/blob/master/Images/2024-07-19_14-42.png?raw=true"
  title="Youtube Music Player">
</div>
<div align="center">
  <img src="https://github.com/Arctic4161/Youtube_Music_Player/blob/master/Images/2024-07-19_14-44.png?raw=true"
  title="Youtube Music Player">
</div>
<div align="center">
  <img src="https://github.com/Arctic4161/Youtube_Music_Player/blob/master/Images/2024-07-19_14-44_1.png?raw=true"
  title="Youtube Music Player">
</div>
<hr>

**Not to be used with copyrighted material.**
