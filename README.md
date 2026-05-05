# VRChat Media Pipeline

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-00a393)](https://fastapi.tiangolo.com)
[![License MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> A local-to-cloud media preprocessing and HLS distribution pipeline designed for VRChat video players.
>
> **No Affiliation:** This project is not affiliated with, endorsed by, or connected to any content platform. All trademarks and platform names are the property of their respective owners.

---

## ⚠️ Important Legal Notice

> **Read this carefully before using this software.**

This project is provided for **educational and research purposes only**. It is a general-purpose media processing and distribution tool. **It does not:**

- Host, store, or distribute any copyrighted content
- Provide access to any third-party platform or service
- Circumvent any DRM, encryption, or access control mechanisms
- Include, embed, or reference any proprietary API keys, tokens, or credentials

**Your Responsibility as a User:**

1. **Compliance with Laws**: You are solely responsible for ensuring your use of this software complies with all applicable local, national, and international laws and regulations, including but not limited to copyright, intellectual property, and telecommunications laws.

2. **Third-Party Terms**: If you configure this software to interact with any third-party platform or service, you MUST review and comply with that platform's Terms of Service, API usage policies, and content usage agreements. The developers do not endorse or encourage any use that violates such terms.

3. **Content Rights**: You must have the legal right to access and process any media content through this software. Processing copyrighted content without authorization may constitute copyright infringement.

4. **No Warranty**: This software is provided "AS IS", without warranty of any kind, express or implied. See the [MIT License](LICENSE) for details.

5. **No Liability**: The authors and contributors assume **NO LIABILITY** for any misuse, damages, legal claims, or losses arising from the use of this software. You use it entirely at your own risk.

**If you are unsure whether your intended use is legal or compliant, consult a qualified legal professional before proceeding.**

---

## Which Project Should You Use?

This project is the successor to [VRChat BPlayer Proxy Ultra](https://github.com/IoriMaboroshi/vrchat-bplayer-proxy-ultra). Both are MIT-licensed tools for serving media to VRChat players, but they are designed for different scenarios.

### VRChat Media Pipeline (this project)

**Best for: High-quality, reliable playback with strong local hardware**

| ✅ Best When | ❌ Not Ideal When |
|---|---|
| You have a powerful local GPU (AMD/NVIDIA/Intel) | You don't have a local machine to run the software |
| You need VOD with seek support and accurate duration | You need instant streaming with minimal setup |
| Your internet upload to remote server is fast or you accept preprocessing wait time | You prefer real-time proxy without preprocessing |
| You want persistent task tracking (close browser, reopen, see progress) | You only need live streaming (no preloading) |
| You need to serve multiple viewers watching the same video in sync | Your remote server runs Docker and can host the full service |

### [VRChat BPlayer Proxy Ultra](https://github.com/IoriMaboroshi/vrchat-bplayer-proxy-ultra)

**Best for: Real-time streaming proxy with minimal local processing**

| ✅ Best When | ❌ Not Ideal When |
|---|---|
| You want instant streaming (no preprocessing wait) | You need reliable VOD with accurate seek |
| Your remote server can run Python + FFmpeg | Your connection to remote is slow (real-time proxy needs good bandwidth) |
| You prefer frp/tunnel-based deployment | You have local GPU you want to utilize fully |
| You only need one viewer at a time | You want persistent task history and management |

### Quick Comparison

| Feature | Media Pipeline | BPlayer Ultra |
|---|---|---|
| **Processing model** | Preprocess → Push → Static Serve | Real-time proxy |
| **GPU utilization** | Full local GPU acceleration (download + transcode) | Limited by tunnel bandwidth |
| **Playback** | VOD HLS with seek and duration | EVENT/LIVE HLS (no seek on long videos) |
| **Multi-viewer sync** | ✅ All viewers share same static files | ❌ Per-request transcode |
| **Dashboard** | Full pipeline management, task history, remote content viewer | Basic stats, URL generator, preload manager |
| **Remote server** | nginx only (static files) | Python + FFmpeg required |
| **Task persistence** | ✅ Survives browser close | ❌ In-memory only |
| **Remote content listing** | ✅ Public landing page with video cards | ❌ Not available |

---

## Architecture

```
                          Local Machine (GPU)
                    +-----------------------------+
                    |  Web Dashboard (:8080)      |
                    |  - Query video metadata     |
                    |  - Preload & cache          |
                    |  - One-click push           |
                    |  - Task management          |
                    +-------------+---------------+
                                  |
                    +-------------v---------------+
                    |  Pipeline Engine            |
                    |  1. aria2c multi-thread DL  |
                    |  2. FFmpeg GPU transcode    |
                    |  3. rclone SFTP upload      |
                    +-------------+---------------+
                                  |
                          Internet (rclone SFTP)
                                  |
                    +-------------v---------------+
                    |  Remote Server (nginx)      |
                    |  - Static HLS (:14515)      |
                    |  - Landing page             |
                    |  - Token auth               |
                    |  - 24h auto-cleanup         |
                    +-----------------------------+
```

---

## Features

### Core Pipeline
- **Query → Preload → Transcode → Push**: full automated pipeline
- **GPU-accelerated transcoding**: AMD AMF, NVIDIA NVENC, Intel QSV, VA-API; software fallback
- **AVC direct copy**: no re-encoding for H.264 sources (zero quality loss)
- **Multi-thread download**: aria2c with configurable parallel connections
- **Smart caching**: 12-hour TTL; push directly from cache (skip download)
- **Real-time progress**: per-stage percentage with speed/ETA info
- **Persistent tasks**: close browser, reopen, progress still visible

### Web Dashboard
- **Multi-format query**: accepts BVID / EP / SS identifiers
- **Quality selector**: shows only available qualities with estimated file size
- **One-click push**: preload → GPU transcode → upload to remote
- **Cache-to-push**: skip download, push directly from local cache
- **Task list**: real-time progress bars, cancel running tasks, delete history
- **Remote viewer**: see server content, copy play links, delete files
- **Settings**: token, ports, quality limits, threads

### Public Landing Page (Remote Server)
- Token-gated access
- Video cards: cover, title, duration, quality
- 24h countdown timer per video
- One-click copy play link
- Disk usage display

---

## Requirements

- **OS**: Windows 10/11 (primary), Linux (supported)
- **Python**: 3.9+
- **FFmpeg**: 5.x+ (with hardware encoder support recommended)
- **aria2c**: for multi-threaded download
- **rclone**: for remote upload (SFTP backend)
- **GPU**: AMD RX 500+ / NVIDIA GTX 900+ / Intel HD Graphics+ (optional)

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/IoriMaboroshi/vrchat-media-pipeline.git
cd vrchat-media-pipeline
```

### 2. Install Dependencies

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Install external tools (via [Scoop](https://scoop.sh) on Windows):

```bash
scoop install ffmpeg aria2 rclone
```

### 3. Configure

```bash
# Required: set your API token
set API_TOKEN=your_secure_token_here
```

Edit `config.py` or use environment variables:
- `API_TOKEN` — authentication token for API access
- `API_PORT` — API server port (default: 14515)
- `WEB_PORT` — Web dashboard port (default: 8080)
- `FFMPEG_PATH` — path to ffmpeg binary (default: `ffmpeg` in PATH)
- `ARIA2_CONNECTIONS` — parallel download threads (default: 32)

### 4. Configure Platform Authentication

This software requires valid platform credentials to access media metadata. Open the web dashboard at `http://localhost:8080`, navigate to the login page, and follow the authentication flow for your content platform.

### 5. Configure Remote Server

```bash
# Set up rclone SFTP remote
rclone config create myserver sftp host YOUR_SERVER_IP port 22 user root key_file C:\Users\You\.ssh\id_rsa

# Update pipeline config in config.py or via Web dashboard:
# - public_base_url = "http://YOUR_SERVER_IP:14515"
```

On the remote server, install nginx and deploy the included `nginx-remote.conf` reference configuration. Set up a cron job for 24-hour file cleanup:

```bash
0 * * * * find /var/www/hls -type f -mmin +1440 -delete
```

### 6. Run

```bash
# Windows (background, no console window)
start "" /B .venv\Scripts\pythonw.exe main.py

# Or with console output visible
.venv\Scripts\python.exe main.py
```

Access:
- **Dashboard**: `http://localhost:8080` (default: `admin` / `password`)
- **API**: `http://localhost:14515`
- **Health**: `http://localhost:14515/health`

---

## Usage Workflow

1. Open dashboard → enter a video identifier (BVID / EP / SS)
2. Click **Query** → see cover, title, duration, available qualities with file size estimates
3. Select quality → click **Push to Server** or **Preload Only**
4. Watch real-time progress: Download → Transcode → Upload
5. Copy the play link → paste into VRChat video player
6. All viewers see the same stream with full VOD support (seek, duration)

---

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/play` | GET | Token | Stream HLS video |
| `/health` | GET | None | Health + system metrics |
| `/api/video-info` | GET | Token | Query video metadata with quality options |
| `/api/preload` | POST | Token | Start background download |
| `/api/push-to-aliwh1` | POST | Token | Full pipeline: download → transcode → upload |
| `/api/push-from-cache` | POST | Token | Pipeline from cached files (skip download) |
| `/api/push-status/{id}` | GET | Token | Pipeline task progress |
| `/api/tasks` | GET | Token | All tasks (persistent across browser restarts) |
| `/api/tasks/{id}/cancel` | POST | Token | Cancel a running task |
| `/api/aliwh1-list` | GET | Token | List videos on remote server |
| `/api/aliwh1-delete/{bvid}` | DELETE | Token | Delete video from remote server |
| `/api/cache-stats` | GET | Token | Local cache statistics |

---

## Hardware Encoder Support

| Encoder | GPU | Drivers |
|---------|-----|---------|
| **AMD AMF** | RX 500+, Vega, RDNA 1/2/3 | AMD Adrenalin |
| **NVIDIA NVENC** | GTX 900+, RTX all | NVIDIA Driver + CUDA |
| **Intel QSV** | HD Graphics 4000+, UHD, Iris Xe, Arc | Intel Media SDK |
| **VA-API** | Any VA-API GPU (Linux) | Mesa / Intel VA-API |
| **libx264 (software)** | None (CPU) | None |

The pipeline auto-detects available encoders on startup and selects the best one.

---

## Project Structure

```
vrchat-media-pipeline/
├── main.py              # FastAPI entry (dual-port: API + Web)
├── config.py            # Configuration (env vars & dynamic settings)
├── api/
│   └── routes.py        # All API endpoints + pipeline orchestration
├── bilibili/            # Content platform API integration
│   ├── video.py         # Video metadata + stream URL resolution
│   ├── auth.py          # QR login + session management
│   └── wbi.py           # API request signing
├── utils/
│   ├── pipeline.py      # Pipeline runner (download→transcode→upload)
│   ├── downloader.py    # Multi-threaded download + smart cache
│   ├── codec_adapter.py # FFmpeg encoder detection & validation
│   └── geo.py           # IP geolocation
├── web/
│   └── routes.py        # Dashboard page routes
├── templates/           # Jinja2 HTML dashboard templates
├── db/                  # SQLite database layer
├── data/                # Runtime data (auto-created)
└── deploy/              # Remote server deployment files
    ├── nginx-remote.conf
    ├── setup-remote.sh
    └── index.html
```

---

## License

MIT License — see [LICENSE](LICENSE) file.

---

## Disclaimer

> ⚠️ **EDUCATIONAL PURPOSE ONLY**
>
> This software is a general-purpose media processing and distribution tool provided for educational and research purposes. It does not host, store, distribute, or provide access to any copyrighted content.
>
> Users are solely responsible for:
> - Complying with all applicable laws and regulations
> - Respecting third-party terms of service and content usage rights
> - Ensuring they have legal authorization to process any media content
>
> **The authors and contributors assume no liability for any misuse, damages, legal claims, or losses arising from the use of this software. Use at your own risk. If in doubt, consult a qualified legal professional.**
