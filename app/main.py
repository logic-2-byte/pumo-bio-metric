"""
The FastAPI application: the local dashboard and the ADMS/iClock listener.

The bridge that actually moves punches into the LMS is started by the lifespan
in `app.core.manager`; see `app.sync` for what it does.
"""

from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.core.logger import setup_logger
from app.core.manager import lifespan
from app.core.settings import Settings
from app.router.base import router as base_router
from app.router.iclock import get_local_ips
from app.router.iclock import router as iclock_router

_settings = Settings()

TEMPLATES = Path(__file__).parent / "templates"

app = FastAPI(lifespan=lifespan, debug=_settings.debug, docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

setup_logger(_settings.debug)


@app.middleware("http")
async def log_incoming_requests(request: Request, call_next):
    path = request.url.path
    # The SSE stream and the dashboard's own polling would otherwise fill the
    # terminal and bury the punch banners, which are the useful output.
    if not path.startswith(("/events", "/api/punches", "/favicon.ico")):
        client_ip = request.client.host if request.client else "unknown"
        query = f"?{request.url.query}" if request.url.query else ""
        print(f"\033[1;90m[HTTP {request.method}]\033[0m "
              f"\033[1;36m{client_ip}\033[0m -> \033[1;97m{path}{query}\033[0m")
    return await call_next(request)


app.include_router(base_router)
app.include_router(iclock_router)


@lru_cache(maxsize=1)
def _dashboard_template() -> str:
    """
    The dashboard markup, read once.

    It used to be ~340 lines of HTML, CSS and JavaScript inside an f-string in
    this file, which meant every CSS and JS brace had to be doubled to survive
    interpolation — unreadable, and unlintable either as Python or as HTML.
    """
    return (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
async def root_dashboard(request: Request) -> HTMLResponse:
    ips = get_local_ips()
    # Plain string substitution rather than str.format: the page is full of CSS
    # and JS braces, and format() would try to read every one of them as a
    # field. This is also why the placeholders are __NAME__ rather than {name}.
    html = (
        _dashboard_template()
        .replace("__SERVER_IP__", ips[0] if ips else "localhost")
        .replace("__SERVER_PORT__", str(request.url.port or 8000))
    )
    return HTMLResponse(content=html)


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "PUT", "OPTIONS"])
async def catch_all(path: str, request: Request) -> PlainTextResponse:
    """
    Answer OK to anything unrecognised.

    Reader firmware varies in which ADMS path it calls, and one that gets a 404
    may stop pushing altogether. Logging the path is how an unsupported variant
    gets noticed and added.
    """
    body = (await request.body()).decode(errors="ignore")
    client_ip = request.client.host if request.client else "unknown"
    print(f"\033[1;33m[Unmapped Route]\033[0m /{path} | {request.method} | {client_ip}")
    if body:
        print(f"   Payload: {body[:300]}")
    return PlainTextResponse("OK", media_type="text/plain")
