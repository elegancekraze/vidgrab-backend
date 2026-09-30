import os
import glob
import json
import base64
import shutil
import tempfile
import urllib.request
import urllib.error
import urllib.parse

import yt_dlp
from Crypto.Cipher import AES
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
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


def vidssave_parse(url: str, origin: str = "cache") -> dict:
    body = urllib.parse.urlencode({
        "hostname": "vidssave.com",
        "auth": "4c9b7d21",
        "domain": "api-ak.vidssave.com",
        "origin": origin,
        "link": url,
    }).encode()
    req = urllib.request.Request(
        VIDSAVE_URL, data=body, method="POST",
        headers={
            "User-Agent": UA,
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://vidssave.com",
            "referer": "https://vidssave.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        j = json.load(r)
    if not j.get("data"):
        raise RuntimeError(j.get("msg") or "no data from provider")
    obj = _vidssave_decrypt(j["data"])
    if not obj:
        raise RuntimeError("decrypt failed")
    return json.loads(obj)


@app.get("/api/youtube")
def youtube(request: Request, url: str = Query(...)):
    check_auth(request)
    o = None
    err = None
    for origin in ("cache", "source"):
        try:
            cand = vidssave_parse(url, origin)
            if any(x.get("download_url") for x in cand.get("resources", [])):
                o = cand
                break
            o = o or cand
        except Exception as e:
            err = e
    if o is None:
        return JSONResponse(status_code=502, content={"ok": False, "error": str(err)})
    fmts = []
    for x in o.get("resources", []):
        if not x.get("download_url"):
            continue
        fmts.append({
            "quality": x.get("quality"),
            "format": x.get("format"),
            "type": x.get("type"),
            "size": x.get("size"),
            "url": x.get("download_url"),
        })
    fmts.sort(key=lambda f: (0 if f.get("type") == "video" else 1, -(f.get("size") or 0)))
    return {"ok": True, "title": o.get("title"), "duration": o.get("duration"),
            "thumbnail": o.get("thumbnail"), "formats": fmts}


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
