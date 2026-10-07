# FileToLink

> A Telegram-powered file streaming and web delivery system with direct
> playback, HTTP range streaming, HLS transcoding, and multi-audio
> support.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![AIOHTTP](https://img.shields.io/badge/AIOHTTP-Web%20Server-2C5F2D)](https://docs.aiohttp.org/)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-Telegram%20Client-2CA5E0)](https://docs.pyrogram.org/)
[![HLS](https://img.shields.io/badge/Streaming-HLS-111827)](https://developer.apple.com/streaming/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

FileToLink is a lightweight Python application that connects Telegram
media handling with a browser-based streaming layer. It can serve
supported media through a web player, handle HTTP byte ranges for
seeking, and generate HLS playlists for advanced playback scenarios such
as multiple audio tracks.

------------------------------------------------------------------------

## ✨ Highlights

-   **Telegram-powered media handling**
-   **Browser-based video and audio playback**
-   **HTTP Range support** for smooth seeking and resumable delivery
-   **HLS playback** with FFmpeg
-   **Multiple audio-track detection and switching**
-   **Language-aware audio labels** with sensible fallbacks
-   **Responsive web player**
-   **Multi-client Telegram support**
-   **Hash-protected media URLs**
-   **Protected HLS file paths**
-   **HLS process and cache management**
-   **Automatic cleanup of temporary HLS jobs**
-   **MongoDB-backed user/chat data**
-   **Cloud/background-worker friendly**
-   **Docker and Procfile support**

------------------------------------------------------------------------

## 🧱 Architecture

``` text
                         ┌─────────────────────┐
                         │       Telegram      │
                         │   Bot / Media File  │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │     Pyrogram        │
                         │  Media / File Info  │
                         └──────────┬──────────┘
                                    │
                                    ▼
┌──────────────┐         ┌─────────────────────┐
│   Browser    │ ──────► │     AIOHTTP         │
│  Web Player  │         │  Streaming Routes   │
└──────────────┘         └──────────┬──────────┘
                                    │
                         ┌──────────┴──────────┐
                         │                     │
                         ▼                     ▼
                ┌────────────────┐    ┌────────────────┐
                │ Range Streaming│    │  HLS Manager   │
                │  ByteStreamer  │    │    FFmpeg      │
                └────────────────┘    └───────┬────────┘
                                              │
                                              ▼
                                      ┌────────────────┐
                                      │ HLS Playlists  │
                                      │ Video + Audio  │
                                      └────────────────┘
```

------------------------------------------------------------------------

## 📂 Project Structure

``` text
FileToLink/
├── TechVJ/
│   ├── bot/
│   │   ├── __init__.py
│   │   └── clients.py
│   ├── server/
│   │   └── exceptions.py
│   ├── template/
│   │   ├── req.html
│   │   └── dl.html
│   └── util/
│       ├── custom_dl.py
│       ├── file_properties.py
│       ├── hls_manager.py
│       ├── media_probe.py
│       ├── render_template.py
│       └── ...
│
├── database/
│   └── users_chats_db.py
│
├── plugins/
│   ├── __init__.py
│   ├── route.py
│   ├── start.py
│   └── broadcast.py
│
├── tests/
│   └── test_audio_tracks.py
│
├── bot.py
├── info.py
├── Script.py
├── requirements.txt
├── Dockerfile
├── Procfile
├── runtime.txt
├── logging.conf
└── README.md
```

------------------------------------------------------------------------

## 🚀 Core Features

### 🎬 Web Playback

The web player provides browser-based playback for supported media and
includes common playback controls such as:

-   Play / pause
-   Seeking
-   Volume control
-   Fullscreen
-   Picture-in-Picture where supported
-   Download
-   Audio selection
-   HLS playback

The player is designed to work across modern desktop and mobile
browsers.

### ⚡ HTTP Range Streaming

FileToLink supports byte-range requests so the browser can request only
the portion of a file it needs.

This enables:

-   Fast seeking
-   Partial media delivery
-   Resuming playback
-   Better browser compatibility
-   Reduced unnecessary data transfer

### 🎧 Multi-Audio HLS

For media containing multiple audio streams, the application can:

1.  Detect available audio tracks.
2.  Read language and codec metadata.
3.  Generate separate HLS audio renditions.
4.  Build a master HLS playlist.
5.  Expose those renditions to the web player.
6.  Switch between available audio tracks without unnecessarily
    restarting playback.

When language metadata is unavailable, the player can fall back to
labels such as `Track 1`, `Track 2`, etc.

### 🔄 Audio State Synchronization

Audio selection is synchronized with the actual active HLS track rather
than relying only on UI state.

The player handles:

-   HLS audio-track indexes
-   Track switching events
-   Default audio selection
-   Rapid track changes
-   Switching timeouts
-   Failed-switch recovery
-   HLS listener cleanup
-   Player/HLS instance cleanup

------------------------------------------------------------------------

## 🔐 Security

The project includes protections around media delivery and HLS files.

Examples include:

-   Hash validation for media requests
-   Protected streaming endpoints
-   HLS path traversal checks
-   Safe file-path resolution
-   Sanitized filenames
-   No client-side exposure of private credentials

### Important

Never commit secrets to GitHub.

Keep credentials such as:

-   Telegram API ID
-   Telegram API hash
-   Bot token
-   MongoDB URI
-   Session strings
-   Private keys

outside the repository and provide them through environment variables or
the hosting platform's secret manager.

------------------------------------------------------------------------

## 🛠️ Requirements

Recommended environment:

-   Python **3.10+**
-   Telegram Bot Token
-   Telegram API ID
-   Telegram API Hash
-   MongoDB
-   FFmpeg

The project includes `imageio-ffmpeg`, allowing deployments to use a
bundled FFmpeg binary where supported.

------------------------------------------------------------------------

## 📦 Installation

Clone the repository:

``` bash
git clone https://github.com/konexdevelopers/FileToLink.git
cd FileToLink
```

Create a virtual environment:

``` bash
python -m venv venv
```

Activate it.

### Windows

``` bash
venv\Scripts\activate
```

### Linux / macOS

``` bash
source venv/bin/activate
```

Install dependencies:

``` bash
pip install -r requirements.txt
```

------------------------------------------------------------------------

## ⚙️ Configuration

Configure the project's required environment variables before starting
the application.

A typical setup contains values similar to:

``` env
API_ID=your_api_id
API_HASH=your_api_hash
BOT_TOKEN=your_bot_token

DATABASE_URI=your_mongodb_uri

LOG_CHANNEL=-100xxxxxxxxxx

URL=https://your-domain.example/
```

Use the variable names expected by the current `info.py` configuration.

**Do not copy real credentials into this README or commit them to the
repository.**

------------------------------------------------------------------------

## ▶️ Running Locally

Start the application with:

``` bash
python bot.py
```

The application initializes the Telegram client and starts the AIOHTTP
web server.

For development, monitor the console for:

-   Telegram client initialization
-   Plugin loading
-   HLS manager initialization
-   Web server startup
-   FFmpeg/HLS errors

------------------------------------------------------------------------

## 🧪 Testing

The repository includes audio-track tests under:

``` text
tests/test_audio_tracks.py
```

Run the test suite with:

``` bash
pytest
```

Before production deployment, verify:

``` text
✓ Application starts successfully
✓ Telegram client connects
✓ Database connection works
✓ Watch page loads
✓ Video playback works
✓ Seeking works
✓ Range requests work
✓ HLS playlist generation works
✓ Multiple audio tracks are detected
✓ Audio switching works
✓ Single-audio media works
✓ HLS cleanup works
✓ Invalid hashes are rejected
✓ Invalid HLS paths are rejected
```

------------------------------------------------------------------------

## ☁️ Deployment

FileToLink can be deployed to a compatible Python hosting service or
background-worker platform.

### Build command

``` bash
pip install -r requirements.txt
```

### Start command

``` bash
python bot.py
```

The repository also contains:

-   `Dockerfile`
-   `Procfile`
-   `runtime.txt`

for environments that support those deployment formats.

### Git-based deployment

For automatic deployment, configure your hosting provider to track:

``` text
Repository: konexdevelopers/FileToLink
Branch: main
```

After pushing a new commit to the configured branch, the hosting
platform can rebuild and restart the application according to its
deployment settings.

------------------------------------------------------------------------

## 🧹 HLS Resource Management

HLS generation can require significant CPU, memory, and temporary
storage.

The HLS manager is responsible for:

-   Creating HLS jobs
-   Preventing duplicate jobs
-   Managing FFmpeg processes
-   Generating video playlists
-   Generating audio playlists
-   Serving cached HLS files
-   Cleaning unused resources

Production deployments should monitor:

-   CPU usage
-   Memory usage
-   Temporary disk usage
-   Number of concurrent FFmpeg processes

------------------------------------------------------------------------

## 🌐 Supported Media Delivery

Depending on the source media and browser capabilities, FileToLink can
work with:

``` text
Direct / Range Streaming
        │
        ├── Video
        ├── Audio
        └── Browser seeking

HLS
        │
        ├── Video playlist
        ├── Multiple audio playlists
        └── Master playlist
```

Actual codec support depends on the browser and the source media.

------------------------------------------------------------------------

## 📱 Browser Compatibility

The player is designed for modern browsers.

  Browser             Playback
  ------------------- ----------
  Chrome / Chromium   ✅
  Edge                ✅
  Firefox             ✅
  Safari              ✅
  Mobile browsers     ✅\*

\* Feature availability such as AirPlay, Picture-in-Picture, native HLS
behavior, and codec support depends on the browser and device.

------------------------------------------------------------------------

## 🤝 Contributing

Contributions and improvements are welcome.

Before submitting a change:

1.  Keep changes focused.
2.  Test the affected functionality.
3.  Run the existing test suite.
4.  Never commit secrets.
5.  Avoid breaking existing streaming routes.
6.  Document significant architectural changes.

------------------------------------------------------------------------

## 📜 License

This project is licensed under the MIT License.

See [`LICENSE`](LICENSE) for the full license text.

------------------------------------------------------------------------

## ⚠️ Responsible Use

FileToLink is a software project for file delivery and media streaming.

Only process, stream, or distribute content that you are authorized to
use and distribute. Users are responsible for complying with applicable
laws, platform rules, copyright requirements, and the terms of the
services they use.

------------------------------------------------------------------------

## ⭐ Project

**FileToLink**

Built with Python, Telegram APIs, AIOHTTP, FFmpeg, and HLS.
