import os
import glob
import json
import time
import re
import subprocess
import base64
import shutil
import tempfile
import urllib.request
import urllib.error
import urllib.parse

import yt_dlp
from Crypto.Cipher import AES
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

API_TOKEN = os.environ.get("API_TOKEN", "").strip()
DEFAULT_COOKIES_B64 = os.environ.get("COOKIES_B64", "").strip()
UPSTREAM_PROXY = os.environ.get("UPSTREAM_PROXY", "").strip()
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY", "").strip()
IMPERSONATE = os.environ.get("IMPERSONATE", "").strip()

app = FastAPI(title="ytdlp-backend", version="1.0.0")


def check_auth(request: Request) -> None:
    if API_TOKEN and request.headers.get("x-auth") != API_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


def cookie_file(request: Request) -> str | None:
    b64 = request.headers.get("x-cookies-b64") or DEFAULT_COOKIES_B64
    if not b64:
        return None
    try:
        data = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=400, detail="bad x-cookies-b64")
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    f.write(data)
    f.close()
    return f.name


def proxy_for(url: str) -> str:
    u = (url or "").lower()
    if ("youtube.com" in u or "youtu.be" in u or "youtube-nocookie.com" in u) and YOUTUBE_PROXY:
        return YOUTUBE_PROXY
    return UPSTREAM_PROXY


def base_opts(request: Request, url: str = "") -> dict:
    cp = cookie_file(request)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
    }
    if cp:
        opts["cookiefile"] = cp
    px = proxy_for(url)
    if px:
        opts["proxy"] = px
    if IMPERSONATE:
        opts["impersonate"] = IMPERSONATE
    return opts


@app.get("/")
def root():
    return {"ok": True, "service": "ytdlp-backend", "auth": bool(API_TOKEN)}


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


@app.get("/api/fetch")
def fetch(
    request: Request,
    url: str = Query(...),
    method: str = Query("GET"),
    data: str = Query(""),
    headers: str = Query(""),
):
    """Token-gated remote fetch, executed from this server's egress (for recon)."""
    check_auth(request)
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad url"})
    hdrs = {}
    if headers:
        try:
            hdrs = json.loads(headers)
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "bad headers json"})
    body = data.encode() if data else None
    req = urllib.request.Request(url, data=body, method=method.upper())
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    for k, v in hdrs.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(300000).decode("utf-8", "replace")
            return {"ok": True, "status": r.status, "final_url": r.geturl(),
                    "headers": {k: v for k, v in r.headers.items()}, "body": raw}
    except urllib.error.HTTPError as e:
        raw = e.read(300000).decode("utf-8", "replace")
        return {"ok": False, "status": e.code, "headers": dict(e.headers), "body": raw}
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})


VIDSAVE_URL = "https://api.vidssave.com/api/contentsite_api/media/parse"
VIDSAVE_KEYS = ["4c9b7d2e" * 3 + "4c9b7d21", "rz18efAXUbdiaO7k"]
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_youtube(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in YOUTUBE_HOSTS)


def _vidssave_decrypt(blob: str):
    for k in VIDSAVE_KEYS:
        kb = k.encode()
        try:
            pt = AES.new(kb, AES.MODE_CBC, kb[:16]).decrypt(base64.b64decode(blob)).rstrip(b"\x00")
            return pt.decode("utf-8")
        except Exception:
            continue
    return None


_RID_CACHE: dict = {}


def _vs_post_text(path: str, fields: dict) -> str:
    fields = {"hostname": "vidssave.com", "auth": "4c9b7d21", "domain": "api-ak.vidssave.com", **fields}
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        "https://api.vidssave.com/api/contentsite_api/" + path,
        data=body, method="POST",
        headers={
            "User-Agent": UA,
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://vidssave.com",
            "referer": "https://vidssave.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def vidssave_parse(url: str, origin: str = "source") -> dict:
    j = json.loads(_vs_post_text("media/parse", {"origin": origin, "link": url}))
    if not j.get("data"):
        raise RuntimeError(j.get("msg") or "no data from provider")
    obj = _vidssave_decrypt(j["data"])
    if not obj:
        raise RuntimeError("decrypt failed")
    return json.loads(obj)


def vidssave_link(content: str) -> str:
    j = json.loads(_vs_post_text("media/download", {"request": content, "no_encrypt": 1}))
    if not j.get("data"):
        raise RuntimeError(j.get("msg") or "download init failed")
    tid = json.loads(_vidssave_decrypt(j["data"]))["task_id"]
    for _ in range(8):
        raw = _vs_post_text("media/download_query", {"task_id": tid, "download_domain": "vidssave.com", "origin": "content_site"})
        data = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except Exception:
                    data = None
        if data and data.get("download_link"):
            return data["download_link"]
        time.sleep(1.2)
    raise RuntimeError("no download link")


@app.get("/api/youtube")
def youtube(request: Request, url: str = Query(...)):
    check_auth(request)
    try:
        o = vidssave_parse(url, "source")
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})
    fmts = []
    for x in o.get("resources", []):
        rid = x.get("resource_id")
        if not rid:
            continue
        _RID_CACHE[rid] = x.get("resource_content") or ""
        f = {"quality": x.get("quality"), "format": x.get("format"), "type": x.get("type"),
             "size": x.get("size"), "rid": rid}
        if x.get("download_url"):
            f["url"] = x["download_url"]
        fmts.append(f)
    fmts.sort(key=lambda f: (0 if f.get("type") == "video" else 1, -(f.get("size") or 0)))
    return {"ok": True, "title": o.get("title"), "duration": o.get("duration"),
            "thumbnail": o.get("thumbnail"), "formats": fmts}


@app.get("/api/youtube/url")
def youtube_url(request: Request, url: str = Query(...), rid: str = Query(...)):
    check_auth(request)
    content = _RID_CACHE.get(rid)
    if not content:
        try:
            o = vidssave_parse(url, "source")
        except Exception as e:
            return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})
        for x in o.get("resources", []):
            if x.get("resource_id"):
                _RID_CACHE[x["resource_id"]] = x.get("resource_content") or ""
        content = _RID_CACHE.get(rid)
    if not content:
        return JSONResponse(status_code=404, content={"ok": False, "error": "format not found"})
    try:
        return {"ok": True, "url": vidssave_link(content)}
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})


TT_UA = ("com.zhiliaoapp.musically/2023501030 (Linux; U; Android 13; en_US; "
         "Pixel 7; Build/TD.A220804.031; Cronet/58.0.2991.0)")
TT_HOSTS = ["api16-normal-c-useast1a.tiktokv.com", "api22-normal-c-useast2a.tiktokv.com",
            "api19-normal-c-useast1a.tiktokv.com"]


def is_tiktok(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()


def tiktok_id(url: str):
    m = re.search(r"/video/(\d+)", url or "")
    return m.group(1) if m else None


def _tt_query() -> dict:
    import uuid
    import random
    t = int(time.time())
    return {
        "device_platform": "android", "os": "android", "ssmix": "a",
        "_rticket": int(time.time() * 1000), "cdid": str(uuid.uuid4()),
        "channel": "googleplay", "aid": "1988", "app_name": "musical_ly",
        "version_code": "350103", "version_name": "35.1.3",
        "manifest_version_code": "2023501030", "update_version_code": "2023501030",
        "ab_version": "35.1.3", "resolution": "1080*2400", "dpi": "420",
        "device_type": "Pixel 7", "device_brand": "Google", "language": "en",
        "os_api": "29", "os_version": "13", "ac": "wifi", "is_pad": "0",
        "current_region": "US", "app_type": "normal", "sys_region": "US",
        "last_install_time": t - 86400, "timezone_name": "America/New_York",
        "residence": "US", "app_language": "en", "timezone_offset": "-14400",
        "host_abi": "armeabi-v7a", "locale": "en", "ac2": "wifi5g", "uoo": "1",
        "carrier_region": "US", "op_region": "US", "region": "US", "ts": t,
        "device_id": str(random.randint(7250000000000000000, 7325099899999994577)),
        "openudid": "".join(random.choices("0123456789abcdef", k=16)),
    }


def tiktok_app(url: str):
    """TikTok app API: returns the aweme detail (video.play_addr = clean, download_addr = watermarked)."""
    import random
    aid = tiktok_id(url)
    if not aid:
        return None
    body = f"aweme_ids=[{aid}]&request_source=0".encode()
    for host in TT_HOSTS:
        u = f"https://{host}/aweme/v1/multi/aweme/detail/?" + urllib.parse.urlencode(_tt_query())
        req = urllib.request.Request(u, data=body, method="POST", headers={
            "User-Agent": TT_UA, "Accept": "application/json", "X-Argus": "",
            "Cookie": "odin_tt=" + "".join(random.choices("0123456789abcdef", k=160)),
        })
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.load(r)
        except Exception:
            continue
        det = (j.get("aweme_details") or [None])[0]
        if det:
            return det
    return None


@app.get("/api/tiktok")
def tiktok(request: Request, url: str = Query(...)):
    check_auth(request)
    det = tiktok_app(url)
    if not det:
        return JSONResponse(status_code=502, content={"ok": False, "error": "tiktok resolve failed"})
    v = det.get("video") or {}
    play = ((v.get("play_addr") or {}).get("url_list") or [None])[0]
    dl = ((v.get("download_addr") or {}).get("url_list") or [None])[0]
    music = (((det.get("music") or {}).get("play_url") or {}).get("url_list") or [None])[0]
    hw = v.get("has_watermark")
    fmts = []
    seen = {}
    for br in (v.get("bit_rate") or []):
        pa = ((br.get("play_addr") or {}).get("url_list") or [None])[0]
        if not pa:
            continue
        name = br.get("gear_name") or ""
        m = re.search(r"(\d{3,4})", name)
        label = f"{m.group(1)}p" if m else (name or f"{int((br.get('bit_rate') or 0)) // 1000}kbps")
        size = br.get("data_size") or 0
        if label not in seen or size > (seen[label][0] or 0):
            seen[label] = (size, pa)
    fmts = [{"quality": k, "format": "MP4", "type": "video", "size": seen[k][0] or None, "sel": seen[k][1]}
            for k in seen]
    fmts.sort(key=lambda f: -int(re.sub(r"\D", "", f["quality"]) or 0))
    if not fmts and play:
        fmts.append({"quality": "Best", "format": "MP4", "type": "video", "size": None, "sel": play})
    nowm = ((v.get("download_no_watermark_addr") or {}).get("url_list") or [None])[0]
    if nowm and nowm != play:
        fmts.insert(0, {"quality": "Origin · no watermark", "format": "MP4", "type": "video",
                        "size": None, "sel": nowm})
    if dl:
        fmts.append({"quality": "With watermark", "format": "MP4", "type": "video", "size": None, "sel": dl})
    if music:
        fmts.append({"quality": "Audio", "format": "MP3", "type": "audio", "size": None, "sel": music})
    if hw and play:
        fmts.append({"quality": "Clean · crop watermark", "format": "MP4", "type": "video",
                     "size": None, "sel": "crop:" + play})
        fmts.append({"quality": "Clean · blur watermark", "format": "MP4", "type": "video",
                     "size": None, "sel": "blur:" + play})
    return {"ok": True, "title": det.get("desc"), "duration": det.get("duration") or v.get("duration"),
            "thumbnail": (((v.get("origin_cover") or {}).get("url_list") or [None])[0]),
            "has_watermark": hw, "formats": fmts}


@app.get("/api/stream")
def stream(request: Request, url: str = Query(...), name: str = Query("video.mp4")):
    """Token-gated proxy: fetch a (IP/geo-gated) media URL from this server and stream it out."""
    check_auth(request)
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad url"})
    hdrs = {"User-Agent": UA, "accept": "*/*", "referer": "https://www.tiktok.com/"}
    rng = request.headers.get("range")
    if rng:
        hdrs["range"] = rng
    try:
        up = urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=60)
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})

    def gen():
        try:
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            up.close()

    out = {"content-type": up.headers.get("Content-Type", "video/mp4"),
           "content-disposition": f'attachment; filename="{name}"'}
    if up.headers.get("Content-Length"):
        out["content-length"] = up.headers["Content-Length"]
    return StreamingResponse(gen(), headers=out)


@app.get("/api/clean")
def clean(request: Request, url: str = Query(...), name: str = Query("video.mp4"),
          mode: str = Query("crop"), bottom: float = 0.12):
    """Remove a baked-in bottom watermark: mode=crop (trim band) or mode=blur (delogo the band, keeps frame)."""
    check_auth(request)
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad url"})
    tmp = tempfile.mkdtemp(prefix="ttclean_")
    src = os.path.join(tmp, "in.mp4")
    dst = os.path.join(tmp, "out.mp4")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "referer": "https://www.tiktok.com/"}), timeout=120) as r, open(src, "wb") as f:
            shutil.copyfileobj(r, f)
        band = max(0.0, min(bottom, 0.4))
        if mode == "blur":
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                    "-show_entries", "stream=width,height", "-of", "csv=p=0", src],
                                   capture_output=True, timeout=60).stdout.decode().strip()
            w, h = (int(x) for x in probe.split(",")[:2])
            bh = max(8, int(h * band))
            y = h - bh - 1
            vf = f"delogo=x=1:y={y}:w={w - 2}:h={bh}"
        else:
            keep = max(0.5, 1.0 - band)
            vf = f"crop=iw:ih*{keep:.4f}:0:0"
        p = subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", vf, "-preset", "ultrafast",
                            "-crf", "23", "-c:a", "copy", "-movflags", "+faststart", dst],
                           capture_output=True, timeout=240)
        if p.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError(p.stderr.decode("utf-8", "replace")[-200:])
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})
    return FileResponse(dst, filename=name, media_type="video/mp4",
                        background=BackgroundTask(shutil.rmtree, tmp, True))


@app.get("/api/info")
def info(request: Request, url: str = Query(...)):
    check_auth(request)
    opts = base_opts(request, url)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as e:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})

    fmts = []
    for f in data.get("formats") or []:
        if f.get("vcodec") in (None, "none") and f.get("acodec") in (None, "none"):
            continue
        fmts.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "height": f.get("height"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "note": f.get("format_note"),
            }
        )
    fmts.sort(key=lambda x: (x.get("height") or 0), reverse=True)

    return {
        "ok": True,
        "title": data.get("title"),
        "uploader": data.get("uploader"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "extractor": data.get("extractor"),
        "is_live": data.get("is_live"),
        "formats": fmts[:60],
    }


@app.get("/api/download")
def download(
    request: Request,
    url: str = Query(...),
    fmt: str = Query("bestvideo*+bestaudio/best"),
    merge: str = Query("mp4"),
):
    check_auth(request)
    tmp = tempfile.mkdtemp(prefix="ydlp_")
    opts = base_opts(request, url)
    opts.update(
        {
            "format": fmt,
            "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
            "merge_output_format": merge,
        }
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return JSONResponse(status_code=502, content={"ok": False, "error": str(e)})

    files = [f for f in glob.glob(os.path.join(tmp, "*")) if os.path.isfile(f)]
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        return JSONResponse(status_code=502, content={"ok": False, "error": "no output file"})

    path = max(files, key=os.path.getsize)
    ext = os.path.splitext(path)[1].lstrip(".") or "bin"
    media = "video/mp4" if ext == "mp4" else f"video/{ext}"
    return FileResponse(
        path,
        filename=os.path.basename(path),
        media_type=media,
        background=BackgroundTask(shutil.rmtree, tmp, True),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
