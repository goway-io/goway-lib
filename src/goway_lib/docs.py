from fastapi import FastAPI
from fastapi.responses import HTMLResponse

_PAGE = """<!doctype html>
<html>
<head>
  <title>{title}</title>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
</head>
<body>
  <script id="api-reference" data-url="{openapi_url}"></script>
  <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body>
</html>"""


def install_scalar_docs(app: FastAPI) -> None:
    page = _PAGE.format(title=app.title, openapi_url=app.openapi_url)

    @app.get("/docs", include_in_schema=False)
    async def scalar_docs() -> HTMLResponse:
        return HTMLResponse(page)
