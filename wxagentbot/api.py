"""FastAPI adapter. Imported only by serve; the core and tests work offline."""
import contextlib
import fcntl
import json
import secrets

from .config import ConfigError
from .service import Bridge
from .store import Conflict


def create_app(settings, *, bridge=None):
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    try:
        token = settings.token_file.read_text().strip()
    except FileNotFoundError as e:
        raise ConfigError("请先运行 ./run.sh init 生成接口 Token") from e
    if len(token) < 32:
        raise ConfigError("接口 Token 至少需要 32 字符")
    if settings.token_file.stat().st_mode & 0o077:
        raise ConfigError("Token 文件权限必须为 600，请运行 chmod 600 .secrets/api-token")
    service = bridge if bridge is not None else Bridge(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        settings.database.parent.mkdir(parents=True, exist_ok=True)
        with (settings.database.parent / "bridge.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise ConfigError("同一个数据库的服务已在运行") from e
            await service.start()
            try:
                yield
            finally:
                await service.stop()

    app = FastAPI(title="wxagentbot", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bridge = service
    bearer = HTTPBearer(auto_error=False)

    async def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not secrets.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "访问 Token 无效", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(authorize)]

    async def body(request):
        chunks, length = [], 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 65536:
                raise HTTPException(413, "请求体超过 64 KiB")
            chunks.append(chunk)
        try:
            value = json.loads(b"".join(chunks))
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(400, "请求体必须是有效 JSON") from e
        if not isinstance(value, dict):
            raise HTTPException(422, "请求体必须是 JSON 对象")
        return value

    @app.exception_handler(ConfigError)
    async def config_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/healthz")
    async def health():
        return {"status": "running"}

    @app.get("/v1/status", dependencies=auth)
    async def status():
        return service.status()

    @app.get("/v1/conversations", dependencies=auth)
    async def conversations():
        return [{"id": c.id, "name": c.name, "kind": c.kind, "enabled": c.enabled,
                 "allow_push": c.allow_push} for c in service.settings.conversations]

    @app.post("/v1/messages/send", dependencies=auth, status_code=202)
    async def send(request: Request):
        data = await body(request)
        if set(data) != {"conversation", "message"}:
            raise HTTPException(422, "只接受 conversation 和 message 两个字段")
        row = service.enqueue_push(data["conversation"], data["message"], request.headers.get("Idempotency-Key"))
        return {"id": row["id"], "status": row["status"], "status_url": "/v1/messages/" + row["id"]}

    @app.get("/v1/messages/{uid}", dependencies=auth)
    async def message(uid: str):
        row = service.store.get("deliveries", uid)
        if row is None:
            raise HTTPException(404, "发送记录不存在")
        return {k: row[k] for k in ("id", "conversation", "status", "next_part", "error", "created", "updated")}

    @app.get("/v1/tasks", dependencies=auth)
    async def tasks():
        return service.store.recent("tasks")

    @app.get("/v1/tasks/{uid}", dependencies=auth)
    async def task(uid: str):
        row = service.store.get("tasks", uid)
        if row is None:
            raise HTTPException(404, "任务不存在")
        return row

    @app.post("/v1/tasks/{uid}/cancel", dependencies=auth, status_code=202)
    async def cancel(uid: str):
        try:
            service.cancel(uid)
        except KeyError as e:
            raise HTTPException(404, "任务不存在") from e
        return {"id": uid, "status": "cancellation_requested"}

    @app.post("/v1/config/reload", dependencies=auth)
    async def reload_config():
        return {"conversations": service.reload()}

    return app
