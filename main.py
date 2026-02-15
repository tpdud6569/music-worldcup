from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

import os, random, re

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# =====================
# OAuth 설정
# =====================
from google_auth_oauthlib.flow import Flow
from fastapi import Request
import os

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/youtube.readonly",
]

def make_flow(request: Request) -> Flow:
    # FastAPI가 계산한 콜백 URL
    redirect_uri = str(request.url_for("auth"))

    # Render 같은 프록시 환경에서 scheme이 http로 잡힐 수 있어서 보정
    proto = request.headers.get("x-forwarded-proto")
    if proto == "https":
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    return Flow.from_client_config(
        {
            "web": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )

@app.get("/login")
def login(request: Request):
    flow = make_flow(request)
    url, _ = flow.authorization_url(prompt="consent")
    return RedirectResponse(url)

@app.get("/auth", name="auth")
def auth(request: Request):
    flow = make_flow(request)
    flow.fetch_token(authorization_response=str(request.url))
    store["creds"] = flow.credentials
    return RedirectResponse("/playlists")


# =====================
# 단일 사용자 저장소
# =====================
store = {}

# =====================
# 설정
# =====================
MIN_SECONDS = 90
CHOICE_LEVELS = [2, 4, 8, 16, 32, 64, 128, 256]

LIKED_MAX_PAGES = 6
LIKED_PER_PAGE = 50


# =====================
# 유틸
# =====================
def iso8601_to_seconds(d):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d)
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def must_login():
    return "creds" not in store


def shuffle_take(arr, n):
    tmp = arr[:]
    random.shuffle(tmp)
    return tmp[:n]


def available_levels(n):
    return [x for x in CHOICE_LEVELS if x <= n]


# =====================
# 라우트
# =====================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/login")
def login():
    url, _ = flow.authorization_url()
    return RedirectResponse(url)


@app.get("/auth")
def auth(request: Request):
    flow.fetch_token(authorization_response=str(request.url))
    store["creds"] = flow.credentials
    return RedirectResponse("/playlists")


# =====================
# 플레이리스트 선택 화면
# =====================
@app.get("/playlists", response_class=HTMLResponse)
def playlists(request: Request):
    if must_login():
        return RedirectResponse("/")

    yt = build("youtube", "v3", credentials=store["creds"])

    res = yt.playlists().list(
        part="snippet",
        mine=True,
        maxResults=50
    ).execute()

    result = []

    for p in res.get("items", []):
        pid = p["id"]

        # ▶ 서로 다른 4곡 썸네일
        pres = yt.playlistItems().list(
            part="snippet",
            playlistId=pid,
            maxResults=4
        ).execute()

        preview = []
        for it in pres.get("items", []):
            thumb = it["snippet"]["thumbnails"].get("medium")
            if thumb:
                preview.append(thumb["url"])

        result.append({
            "id": pid,
            "snippet": p["snippet"],
            "preview": preview
        })

    return templates.TemplateResponse(
        "playlists.html",
        {
            "request": request,
            "playlists": result
        }
    )


# =====================
# ❤️ 좋아요
# =====================
@app.get("/prepare/liked")
def prepare_liked():
    if must_login():
        return RedirectResponse("/")

    yt = build("youtube", "v3", credentials=store["creds"])

    pool = []
    page = None
    pages = 0

    while pages < LIKED_MAX_PAGES:
        res = yt.videos().list(
            part="snippet,contentDetails",
            myRating="like",
            maxResults=LIKED_PER_PAGE,
            pageToken=page
        ).execute()

        for it in res.get("items", []):
            if iso8601_to_seconds(it["contentDetails"]["duration"]) >= MIN_SECONDS:
                pool.append({
                    "id": it["id"],
                    "title": it["snippet"]["title"]
                })

        page = res.get("nextPageToken")
        pages += 1
        if not page:
            break

    store["pool"] = pool
    return RedirectResponse("/choose")


# =====================
# 🎵 플레이리스트 선택
# =====================
@app.get("/prepare/playlist/{playlist_id}")
def prepare_playlist(playlist_id: str):
    if must_login():
        return RedirectResponse("/")

    yt = build("youtube", "v3", credentials=store["creds"])

    res = yt.playlistItems().list(
        part="snippet",
        playlistId=playlist_id,
        maxResults=50
    ).execute()

    ids = []
    titles = {}

    for it in res.get("items", []):
        vid = it["snippet"]["resourceId"].get("videoId")
        if vid:
            ids.append(vid)
            titles[vid] = it["snippet"]["title"]

    pool = []
    if ids:
        vres = yt.videos().list(
            part="contentDetails",
            id=",".join(ids)
        ).execute()

        for v in vres.get("items", []):
            if iso8601_to_seconds(v["contentDetails"]["duration"]) >= MIN_SECONDS:
                pool.append({
                    "id": v["id"],
                    "title": titles.get(v["id"], v["id"])
                })

    store["pool"] = pool
    return RedirectResponse("/choose")


# =====================
# 강 선택
# =====================
@app.get("/choose", response_class=HTMLResponse)
def choose_get(request: Request):
    pool = store.get("pool", [])
    if len(pool) < 2:
        return RedirectResponse("/playlists")

    return templates.TemplateResponse(
        "choose.html",
        {
            "request": request,
            "count": len(pool),
            "options": available_levels(len(pool))
        }
    )


@app.post("/choose")
def choose_post(size: int = Form(...)):
    pool = store.get("pool", [])
    if not pool:
        return RedirectResponse("/playlists")

    if size not in CHOICE_LEVELS or size > len(pool):
        size = max(available_levels(len(pool)))

    store["worldcup"] = shuffle_take(pool, size)
    return RedirectResponse("/worldcup", status_code=303)


# =====================
# 월드컵
# =====================
@app.get("/worldcup", response_class=HTMLResponse)
def worldcup(request: Request):
    return templates.TemplateResponse(
        "worldcup.html",
        {
            "request": request,
            "videos": store.get("worldcup", [])
        }
    )
