import json
import urllib
from typing import Dict, List, Optional

import requests
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import RedirectResponse
from fastapi_csrf_protect import CsrfProtect
from karton.core.backend import KartonBackend, KartonBind
from karton.core.config import Config as KartonConfig
from karton.core.inspect import KartonState
from starlette.datastructures import Headers

from artemis import csrf
from artemis.config import Config
from artemis.db import DB, ColumnOrdering, TaskFilter
from artemis.json_utils import JSONEncoderAdditionalTypes
from artemis.karton_utils import restart_crashed_tasks
from artemis.producer import create_tasks
from artemis.templating import templates

router = APIRouter()
db = DB()

BINDS_THAT_CANNOT_BE_DISABLED = ["classifier", "http_service_to_url", "webapp_identifier", "IPLookup"]


def whitelist_proxy_request_headers(headers: Headers) -> Dict[str, str]:
    result = {}
    for header in headers:
        if header.lower() in ["referer", "referrer"]:
            result[header] = headers[header]
    return result


def whitelist_proxy_response_headers(headers: requests.structures.CaseInsensitiveDict[str]) -> Dict[str, str]:
    result = {}
    for header in headers:
        if header.lower() in [
            "content-type",
            "content-length",
            "last-modified",
            "cache-control",
            "etag",
            "content-encoding",
            "location",
        ]:
            result[header] = headers[header]
    return result


def get_binds_that_can_be_disabled() -> List[KartonBind]:
    backend = KartonBackend(config=KartonConfig())

    binds = []
    for bind in backend.get_binds():
        if bind.identity in BINDS_THAT_CANNOT_BE_DISABLED:
            # Not allowing to disable as it's a core module
            continue

        binds.append(bind)

    return binds


def error_content_not_found(request: Request, exc: HTTPException) -> Response:
    return templates.TemplateResponse("not_found.jinja2", {"request": request}, status_code=404)


@router.get("/", include_in_schema=False)
def get_root(request: Request) -> Response:
    karton_state = KartonState(backend=KartonBackend(config=KartonConfig()))
    has_analyses = len(list(db.get_paginated_analyses(0, 1, [ColumnOrdering("target", True)]).data)) > 0
    return templates.TemplateResponse(
        "index.jinja2",
        {
            "request": request,
            "has_analyses": has_analyses,
            "api_url": "/api/analyses-table",
            "num_active_tasks": sum([len(analysis.pending_tasks) for analysis in karton_state.analyses.values()]),
        },
    )


@router.get("/add", include_in_schema=False)
def get_add_form(request: Request, csrf_protect: CsrfProtect = Depends()) -> Response:
    binds = sorted(get_binds_that_can_be_disabled(), key=lambda bind: bind.identity.lower())

    return csrf.csrf_form_template_response(
        "add.jinja2",
        {
            "request": request,
            "binds": binds,
            "modules_disabled_by_default": Config.Miscellaneous.MODULES_DISABLED_BY_DEFAULT,
        },
        csrf_protect,
    )


@router.post("/add", include_in_schema=False)
@csrf.validate_csrf
async def post_add(
    request: Request,
    targets: Optional[str] = Form(None),
    file: Optional[bytes] = File(None),
    tag: Optional[str] = Form(None),
    choose_modules_to_enable: Optional[bool] = Form(None),
    redirect: bool = Form(True),
    csrf_protect: CsrfProtect = Depends(),
) -> Response:
    disabled_modules = []

    if choose_modules_to_enable:
        async with request.form() as form:
            for bind in get_binds_that_can_be_disabled():
                if f"module_enabled_{bind.identity}" not in form:
                    disabled_modules.append(bind.identity)

    total_list: List[str] = []
    if targets:
        total_list += (x.strip() for x in targets.split())
    if file:
        total_list += (x.strip() for x in file.decode().split())
    create_tasks(total_list, tag, disabled_modules)
    if redirect:
        return RedirectResponse("/", status_code=301)
    else:
        return Response(
            content="OK",
            status_code=200,
        )


@router.get("/analysis/remove-pending-tasks/{analysis_id}", include_in_schema=False)
def get_remove_pending_tasks(request: Request, analysis_id: str, csrf_protect: CsrfProtect = Depends()) -> Response:
    return csrf.csrf_form_template_response(
        "/remove_pending_tasks.jinja2",
        {
            "analysis_id": analysis_id,
            "analysed_object": db.get_analysis_by_id(analysis_id)["target"],  # type: ignore
            "request": request,
        },
        csrf_protect,
    )


@router.post("/analysis/remove-pending-tasks/{analysis_id}", include_in_schema=False)
@csrf.validate_csrf
async def post_remove_pending_tasks(
    request: Request, analysis_id: str, csrf_protect: CsrfProtect = Depends()
) -> Response:
    db.mark_analysis_as_stopped(analysis_id)

    backend = KartonBackend(config=KartonConfig())

    for task in backend.get_all_tasks():
        if task.root_uid == analysis_id:
            backend.delete_task(task)

    return RedirectResponse("/", status_code=301)


@router.get("/restart-crashed-tasks", include_in_schema=False)
def get_restart_crashed_tasks(request: Request, csrf_protect: CsrfProtect = Depends()) -> Response:
    return csrf.csrf_form_template_response(
        "/restart_crashed_tasks.jinja2",
        {
            "request": request,
        },
        csrf_protect,
    )


@router.post("/restart-crashed-tasks", include_in_schema=False)
@csrf.validate_csrf
async def post_restart_crashed_tasks(request: Request, csrf_protect: CsrfProtect = Depends()) -> Response:
    restart_crashed_tasks()
    return RedirectResponse("/", status_code=301)


@router.get("/queue", include_in_schema=False)
def get_queue(request: Request) -> Response:
    return templates.TemplateResponse(
        "queue.jinja2",
        {
            "request": request,
        },
    )


@router.api_route("/karton-dashboard/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def karton_dashboard(request: Request, path: str) -> Response:
    response = requests.request(
        url="http://karton-dashboard:5000/karton-dashboard/" + path,
        method=request.method,
        allow_redirects=False,
        headers={"connection": "close", **whitelist_proxy_request_headers(request.headers)},
    )
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=whitelist_proxy_response_headers(response.headers),
    )


@router.api_route("/metrics", methods=["GET"], include_in_schema=False)
async def prometheus(request: Request) -> Response:
    response = requests.get(url="http://metrics:9000/")
    return Response(
        content=response.content,
        status_code=response.status_code,
    )


@router.get("/analysis/{root_id}", include_in_schema=False)
def get_analysis(request: Request, root_id: str, task_filter: Optional[TaskFilter] = None) -> Response:
    analysis = db.get_analysis_by_id(root_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")

    api_url_parameters = {"analysis_id": root_id}

    if task_filter:
        api_url_parameters["task_filter"] = task_filter.value

    return templates.TemplateResponse(
        "task_list.jinja2",
        {
            "request": request,
            "title": f"Analysis of { analysis['target'] }",
            "api_url": "/api/task-results-table?" + urllib.parse.urlencode(api_url_parameters),
            "task_filter": task_filter,
        },
    )


@router.get("/results", include_in_schema=False)
def get_results(request: Request, task_filter: Optional[TaskFilter] = None) -> Response:
    if task_filter:
        api_url_parameters = {"task_filter": task_filter.value}
    else:
        api_url_parameters = {}
    return templates.TemplateResponse(
        "task_list.jinja2",
        {
            "request": request,
            "title": "Results",
            "api_url": "/api/task-results-table?" + urllib.parse.urlencode(api_url_parameters),
            "task_filter": task_filter,
        },
    )


@router.get("/task/{task_id}", include_in_schema=False)
def get_task(task_id: str, request: Request, referer: str = Header(default="/")) -> Response:
    task = db.get_task_by_id(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return templates.TemplateResponse(
        "task.jinja2",
        {
            "request": request,
            "task": task,
            "referer": referer,
            "pretty_printed": json.dumps(task, indent=4, cls=JSONEncoderAdditionalTypes),
        },
    )
