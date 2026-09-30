import os
import glob
import json
import time
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


TIKWM = "https://www.tikwm.com/api/"


def is_tiktok(url: str) -> bool:
    return "tiktok.com" in (url or "").lower()


@app.get("/api/tiktok")
def tiktok(request: Request, url: str = Query(...)):
    check_auth(request)
    title = None
    duration = None
    thumb = None
    vids: dict = {}
    auds: dict = {}
    try:
        opts = base_opts(request, url)
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get("title")
        duration = info.get("duration")
        thumb = info.get("thumbnail")
        for f in info.get("formats") or []:
            fid = (f.get("format_id") or "").lower()
            u = f.get("url")
            if not u:
                continue
            if "download" in fid:  # watermarked variant
                continue
            size = f.get("filesize") or f.get("filesize_approx") or 0
            if f.get("vcodec") not in (None, "none"):
                h = f.get("height")
                if h and (h not in vids or size > (vids[h][0] or 0)):
                    vids[h] = (size, u, f.get("ext") or "mp4")
            elif f.get("acodec") not in (None, "none"):
                abr = f.get("abr") or 0
                k = int(abr) if abr else 0
                if k not in auds or size > (auds[k][0] or 0):
                    auds[k] = (size, u, f.get("ext") or "m4a")
    except Exception:
        pass
    fmts = []
    for h in sorted(vids, reverse=True):
        fmts.append({"quality": f"{h}p", "format": "MP4", "type": "video", "size": None, "sel": str(h)})
    if auds:
        fmts.append({"quality": "Audio", "format": "M4A", "type": "audio", "size": None, "sel": "audio"})
    # watermarked option (direct) + fallback via tikwm
    try:
        q = urllib.parse.urlencode({"url": url, "hd": "1"})
        req = urllib.request.Request(TIKWM + "?" + q, headers={"User-Agent": UA, "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=45) as r:
            j = json.load(r)
        d = j.get("data") or {}
        title = title or d.get("title")
        duration = duration or d.get("duration")
        thumb = thumb or d.get("cover")
        if d.get("wmplay"):
            fmts.append({"quality": "With watermark", "format": "MP4", "type": "video",
                         "size": d.get("size") or None, "url": d.get("wmplay")})
        if not vids and d.get("hdplay"):
            fmts.insert(0, {"quality": "HD", "format": "MP4", "type": "video",
                            "size": d.get("hd_size") or None, "url": d.get("hdplay")})
    except Exception:
        pass
    if not fmts:
        return JSONResponse(status_code=502, content={"ok": False, "error": "no formats"})
    return {"ok": True, "title": title, "duration": duration, "thumbnail": thumb, "formats": fmts}


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
