# CamStack 2.0.1

CamStack is a self-contained, offline, single-camera display appliance for x86 thin clients / mini PCs.

## What it does

- Fullscreen live camera playback (mpv).
- On-screen overlay with:
  - CamStack version
  - Device IP
  - Admin URL
- Auto-fallback to a nature livestream if the chosen RTSP feed is down.
- Built-in HTTPS admin UI (FastAPI + HTMX) on port 443:
  - Scan LAN for ONVIF cameras
  - Show snapshot thumbnails
  - Guess vendor and RTSP paths
  - Let you pick "Use this camera"
  - Let you paste a manual RTSP URL
  - Run "Identify" deep probe with a live progress bar
- Port 80 just redirects straight to HTTPS.
- Systemd services so it runs headless as an appliance.
- Offline TLS:
  - selfsigned (default)
  - private_ca_local (generates a local LAN CA you can trust)
  - csr_only (you sign the cert yourself)

## Install

1. Copy `CamStack_2.0.1.zip` to your Ubuntu box.
2. Run:

  unzip CamStack_2.0.1.zip
  cd CamStack_2.0.1
   sudo bash install_me.sh

The installer:
- Copies everything to `/opt/camstack`
- Installs system deps (`mpv`, `ffmpeg`, `yt-dlp`, etc.)
- Sets up Python deps with `uv`
- Generates HTTPS certs
- Installs and starts:
  - `camredirect.service` (HTTP→HTTPS redirect on :80)
  - `camstack.service`    (FastAPI admin on :443 with TLS)
  - `camplayer.service`   (fullscreen RTSP player w/ overlay + fallback)

When it finishes it'll print something like:

    HTTPS Admin UI: https://192.168.86.86/

Open that URL from another device on the same LAN.  
You'll see a browser warning for the self-signed cert. Continue past it and you're in.

## After install

- The TV/monitor plugged into the box will show either:
  - Your chosen camera fullscreen, or
  - The fallback nature stream
- The overlay in the video includes the box's IP and the admin URL, so you (or someone in the field) can just type that into a phone and switch cameras.

## Version / Changelog

### 2.0.1
- First public cut.
- Appliance-style boot.
- HTTPS by default (self-signed).
- Camera discovery + Identify + thumbnail grab.
- Fallback nature cam behavior.
