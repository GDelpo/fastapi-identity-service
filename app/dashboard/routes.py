"""Dashboard routes — serves the admin UI as HTML pages.

All pages require a valid token stored in localStorage.
The dashboard calls the existing JSON API under the hood.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _DIR / "static"
templates = Jinja2Templates(directory=str(_DIR / "templates"))

router = APIRouter(tags=["Dashboard"], include_in_schema=False)


# ============================================================================
# Static files (self-hosted Tailwind, Lucide, fonts)
# ============================================================================
_MIME = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}


@router.get("/static/{path:path}")
async def dashboard_static(path: str):
    """Serve dashboard static assets (JS, CSS, fonts)."""
    file = _STATIC_DIR / path
    if not file.is_file() or not file.resolve().is_relative_to(_STATIC_DIR.resolve()):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Not found"}, status_code=404)
    media_type = _MIME.get(file.suffix, "application/octet-stream")
    return FileResponse(file, media_type=media_type)


def _base(request: Request) -> str:
    """Get the external base path (e.g., '/identity') from root_path."""
    return (request.scope.get("root_path") or "").rstrip("/")


# ============================================================================
# Shared template context
# ============================================================================

_NAV_ITEMS = [
    {"href_suffix": "/dashboard/", "icon": "bar-chart-3", "label": "Resumen", "key": "overview"},
    {"href_suffix": "/dashboard/users", "icon": "users", "label": "Usuarios", "key": "users"},
]


def _ctx(request: Request, *, page_title: str, active_key: str, **extra) -> dict:
    """Build the common template context for authenticated pages."""
    base = _base(request)
    nav_items = [
        {**item, "href": base + item["href_suffix"], "active": item["key"] == active_key}
        for item in _NAV_ITEMS
    ]
    return {
        "request": request,
        "base": base,
        "service_name": "Identidad",
        "service_icon": "shield",
        "nav_items": nav_items,
        "page_title": page_title,
        **extra,
    }


# ============================================================================
# Pages
# ============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    """Overview — user stats + health."""
    return templates.TemplateResponse(
        "overview.html",
        _ctx(request, page_title="Resumen", active_key="overview"),
    )


@router.get("/users", response_class=HTMLResponse)
async def dashboard_users(request: Request):
    """Users list page."""
    return templates.TemplateResponse(
        "users.html",
        _ctx(request, page_title="Usuarios", active_key="users"),
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def dashboard_user_detail(request: Request, user_id: str):
    """User detail page."""
    return templates.TemplateResponse(
        "user_detail.html",
        _ctx(request, page_title="Detalle de Usuario", active_key="users", user_id=user_id),
    )


@router.get("/login", response_class=HTMLResponse)
async def dashboard_login(request: Request):
    """Login page."""
    base = _base(request)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "base": base,
        "service_name": "Identidad",
        "service_icon": "shield",
    })
