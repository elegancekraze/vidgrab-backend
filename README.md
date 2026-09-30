---
title: ytdlp backend
emoji: 🎬
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# ytdlp-backend

A tiny yt-dlp + ffmpeg HTTP API. Fronted by the `vidgrab` Cloudflare Worker.

## Endpoints

- `GET /` — health.
- `GET /api/info?url=<url>` — metadata + available formats (JSON).
- `GET /api/download?url=<url>&fmt=bestvideo*+bestaudio/best&merge=mp4` — downloads and streams the merged file.

## Auth / cookies

- Set env `API_TOKEN` → every request must send header `x-auth: <token>`.
- For **private / members-only / age-gated** videos, send header `x-cookies-b64` =
  base64 of a Netscape `cookies.txt` for that platform (exported from a logged-in
  browser). Or set env `COOKIES_B64` to use one account's cookies for all requests.

## Deploy

Works on any Docker host: Hugging Face Spaces (Docker SDK), Render, Fly.io, Railway.
Container listens on `PORT` (default 7860, required by HF Spaces).
