from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.requests import Request
from starlette.routing import Route

async def do_redirect(request: Request):
    host = request.headers.get("host", "").split(":")[0]
    path = request.url.path or "/"
    query = ("?" + request.url.query) if request.url.query else ""
    return RedirectResponse(url=f"https://{host}{path}{query}", status_code=308)

routes = [Route("/{path:path}", do_redirect)]
app = Starlette(routes=routes)
