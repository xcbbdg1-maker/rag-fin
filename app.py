"""本地 RAG 服务：自带账号体系 + 权限感知问答。

启动：
  uvicorn app:app --host 127.0.0.1 --port 8000
默认只绑本机；如需局域网访问改 --host 0.0.0.0，并务必配合反向代理/防火墙。
"""
import os
import json
import time
import requests
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import db
import security
from config import LLM_MODEL, MAX_UPLOAD_MB
from ingest import EXTS, LAYER_DIRS, ingest_file
from permissions import is_admin, allowed_layers
from rag import answer, retrieve, stream_generate, list_docs, get_doc, sources_for


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

app = FastAPI(title="财务知识库 RAG（本地自建）")
db.init_db()

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


# ---------- 数据模型 ----------
class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str
    roles: list[str]


class PasswordIn(BaseModel):
    new_password: str


class QueryIn(BaseModel):
    question: str


# ---------- 认证依赖 ----------
def current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    username = security.decode_token(authorization[7:])
    if not username:
        raise HTTPException(401, "登录已过期，请重新登录")
    user = db.get_user(username)
    if not user:
        raise HTTPException(401, "用户不存在")
    return user


def require_admin(user=Depends(current_user)):
    if not is_admin(user["roles"]):
        raise HTTPException(403, "仅管理员可执行此操作")
    return user


# ---------- 账号 ----------
@app.post("/api/login")
def login(body: LoginIn):
    user = db.get_user(body.username)
    if not user or not security.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    return {
        "access_token": security.create_token(user["username"]),
        "username": user["username"],
        "roles": user["roles"],
    }


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"username": user["username"], "roles": user["roles"]}


@app.post("/api/password")
def change_password(body: PasswordIn, user=Depends(current_user)):
    if len(body.new_password) < 8:
        raise HTTPException(400, "密码至少 8 位")
    db.set_password(user["username"], security.hash_password(body.new_password))
    return {"ok": True}


@app.post("/api/register")
def register(body: RegisterIn, user=Depends(require_admin)):
    if db.get_user(body.username):
        raise HTTPException(400, "用户名已存在")
    valid = {"employee", "finance", "admin"}
    bad = set(body.roles) - valid
    if bad or not body.roles:
        raise HTTPException(400, f"角色不合法：{sorted(bad) or '不能为空'}，可选 {sorted(valid)}")
    db.create_user(body.username, security.hash_password(body.password), body.roles)
    return {"ok": True}


@app.get("/api/users")
def users(user=Depends(require_admin)):
    return db.list_users()


# ---------- 问答 ----------
@app.post("/api/query")
def query(body: QueryIn, user=Depends(current_user)):
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    # 角色来自服务端令牌解析出的用户，前端无法伪造 → 权限可信
    try:
        return answer(body.question, user["roles"])
    except requests.exceptions.ConnectionError:
        raise HTTPException(503, "模型服务不可用，请确认 Ollama 已启动（命令行运行 ollama serve）")
    except requests.exceptions.Timeout:
        raise HTTPException(504, "模型响应超时。首次调用需加载模型，可稍后重试；若持续超时请改用更小的模型")
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        raise HTTPException(503, f"模型服务返回错误（{code}）。请确认模型已拉取：ollama pull {LLM_MODEL}")
    except Exception as e:
        raise HTTPException(500, f"问答失败：{type(e).__name__}: {e}")


@app.post("/api/query_stream")
def query_stream(body: QueryIn, user=Depends(current_user)):
    """流式问答（SSE）。检索仍在服务端按角色过滤，越权内容进不到上下文。"""
    if not body.question.strip():
        raise HTTPException(400, "问题不能为空")
    layers = allowed_layers(user["roles"])

    def events():
        t0 = time.time()
        try:
            ctxs = retrieve(body.question, layers)
        except requests.exceptions.ConnectionError:
            yield sse("error", {"message": "模型服务不可用，请确认 Ollama 已启动（ollama serve）"})
            return
        except Exception as e:
            yield sse("error", {"message": f"检索失败：{type(e).__name__}: {e}"})
            return

        yield sse("meta", {"retrieve_ms": int((time.time() - t0) * 1000)})

        if not ctxs:
            yield sse("token", {"text": "在你有权限访问的资料里没有找到相关内容。"})
            yield sse("done", {"elapsed_ms": int((time.time() - t0) * 1000), "sources": []})
            return

        # 来源随 done 事件下发，不能在生成前就发：
        # 模型若答「知识库中没有相关内容」，就不该挂任何来源。
        text = ""
        try:
            for piece in stream_generate(body.question, ctxs):
                text += piece
                yield sse("token", {"text": piece})
        except requests.exceptions.ConnectionError:
            yield sse("error", {"message": "模型服务中断，请确认 Ollama 仍在运行"})
            return
        except Exception as e:
            yield sse("error", {"message": f"生成失败：{type(e).__name__}: {e}"})
            return
        yield sse("done", {"elapsed_ms": int((time.time() - t0) * 1000),
                           "sources": sources_for(text, ctxs)})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/docs")
def docs(user=Depends(current_user)):
    """当前用户有权限看到的文档清单。员工看不到 finance 层文档，连文件名都看不到。"""
    return {"docs": list_docs(allowed_layers(user["roles"]))}


@app.get("/api/doc")
def doc(source: str, user=Depends(current_user)):
    """按权限返回整篇文档内容，供前端弹窗滚动查看。

    安全：layers 来自服务端解析的角色。越权文档一律返回 404（不泄漏「存在但无权」）。
    """
    d = get_doc(source, allowed_layers(user["roles"]))
    if not d:
        raise HTTPException(404, "文档不存在或你无权访问")
    return d


# ---------- 文档上传 ----------
DOCS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "docs")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), layer: str = Form(...),
                 user=Depends(require_admin)):
    """上传文档并立即入库（仅管理员）。

    入库在本进程内完成，因此新文档马上可检索 —— 不必重启服务。
    （外部脚本写入的数据，本进程缓存的 Chroma 索引是看不到的。）
    """
    if layer not in LAYER_DIRS:
        raise HTTPException(400, f"层不合法，可选 {sorted(LAYER_DIRS)}")

    name = os.path.basename(file.filename or "").strip()
    if not name or name.startswith("."):
        raise HTTPException(400, "文件名不合法")
    if os.path.splitext(name)[1].lower() not in EXTS:
        raise HTTPException(400, f"不支持的格式，仅支持 {'/'.join(e[1:].upper() for e in EXTS)}")

    data = await file.read()
    if not data:
        raise HTTPException(400, "文件是空的")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB} MB")

    # 同名文件若已存在于另一层，直接拒绝：source 是检索的唯一标识，
    # 放行会让同一份文档在两层间跳变，权限归属变得不确定。
    other = "finance" if layer == "all" else "all"
    if os.path.exists(os.path.join(DOCS_ROOT, other, name)):
        raise HTTPException(409, f"同名文件已存在于「{other}」层，请先删除或改名")

    dest_dir = os.path.join(DOCS_ROOT, layer)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, name)
    replaced = os.path.exists(path)

    backup = path + ".bak"
    if replaced:
        os.replace(path, backup)          # 留一手：入库失败要能还原
    try:
        with open(path, "wb") as f:
            f.write(data)
        # ingest_file 先向量化、成功后才删旧片段，中途失败不会破坏现有检索
        chunks = ingest_file(path, layer)
        if chunks == 0:
            raise HTTPException(400, "没能从文件中读到文本（扫描件 PDF 没有文字层？）")
    except Exception as e:
        os.remove(path) if os.path.exists(path) else None
        if replaced:
            os.replace(backup, path)      # 还原旧版本；库里的旧片段本就没动过
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(500, f"入库失败：{type(e).__name__}: {e}")
    finally:
        if os.path.exists(backup):
            os.remove(backup)

    return {"source": name, "layer": layer, "chunks": chunks, "replaced": replaced}


# ---------- 前端 ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
