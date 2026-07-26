"""检索增强核心：全部走本地 Ollama（embedding + 生成）和本地 Chroma。

关键：retrieve() 在向量检索层用 layer 元数据过滤，
越权内容根本进不到大模型上下文，而不是事后删。
"""
import re
import json
import time
import requests
import chromadb
from config import (OLLAMA_URL, LLM_MODEL, EMBED_MODEL, CHROMA_PATH, COLLECTION,
                    TOP_K, MAX_DISTANCE, REL_MARGIN, CITE_MARGIN)
from permissions import allowed_layers

_client = None

EMBED_BATCH = 16


def collection():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})


def _post(url: str, payload: dict, timeout: int, tries: int = 3):
    """带退避重试。模型冷加载时首次调用容易超时,重试一次通常就好了。"""
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last = e
            if attempt < tries:
                time.sleep(2 * attempt)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise                      # 4xx 是请求本身的问题,重试没意义
            last = e
            if attempt < tries:
                time.sleep(2 * attempt)
    raise last


def embed_many(texts, batch_size: int = EMBED_BATCH, on_progress=None):
    """批量向量化。Ollama 的 /api/embed 支持数组入参,比逐片调用快很多。"""
    out = []
    for i in range(0, len(texts), batch_size):
        part = texts[i:i + batch_size]
        data = _post(f"{OLLAMA_URL}/api/embed",
                     {"model": EMBED_MODEL, "input": part}, timeout=300)
        out.extend(data["embeddings"])
        if on_progress:
            on_progress(len(out), len(texts))
    return out


def embed(text: str):
    return embed_many([text])[0]


def retrieve(question: str, layers, k: int = TOP_K, include_superseded: bool = False):
    """检索。默认排除已废止版本 —— 与权限过滤同理:不让它进上下文,而不是事后删。

    为什么必须默认排除:实测用旧术语提问时(如"套期保值",现行准则已改称"套期会计"),
    废止版的向量距离比现行版更近,会霸占检索结果前几名 —— 系统就会拿一份失效十年的
    准则给出权威答案,并附上财政部官方链接。每个字都是真的,只是已经不算数了。
    """
    col = collection()
    where = {"layer": {"$in": list(layers)}}
    if not include_superseded:
        where = {"$and": [where, {"superseded": {"$ne": True}}]}
    res = col.query(
        query_embeddings=[embed(question)],
        n_results=k,
        where=where,                              # 权限 + 时效过滤都发生在这里
    )
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    hits = []
    for d, m, dist in zip(docs, metas, dists):
        m = m or {}
        hits.append({"text": d, "source": m.get("source"), "layer": m.get("layer"),
                     "title": m.get("title"), "url": m.get("url"),
                     "doc_no": m.get("doc_no"), "distance": dist})
    if not hits:
        return []
    # 相关性闸门:向量库总会凑满 TOP_K,不过滤就会把无关文档塞进上下文并列为来源。
    best = min(h["distance"] for h in hits)
    if best > MAX_DISTANCE:
        return []                                  # 全库都不沾边 → 老实说查不到
    return [h for h in hits
            if h["distance"] <= MAX_DISTANCE and h["distance"] <= best + REL_MARGIN]


_PROMPT = """你是财务知识库助手。只依据下面的资料回答问题；资料里没有的，直接说“知识库中没有相关内容”，不要编造，尤其不要编造任何数字。

引用要求：答案中涉及具体规定时，在句中标明依据的文件名称和条款（例如“依据《企业会计准则第14号——收入》第五条”）。不要在末尾另起一行罗列来源——来源由系统单独展示。

资料：
{context}

问题：{question}
答案："""


def _context(contexts) -> str:
    """把命中片段拼成上下文。每段冠上标题和发文字号，模型才引得出准则号。"""
    blocks = []
    for c in contexts:
        head = c.get("title") or c.get("source") or ""
        if c.get("doc_no"):
            head += f"（{c['doc_no']}）"
        blocks.append(f"【资料来源：{head}】\n{c['text']}")
    return "\n\n---\n\n".join(blocks)


_THINK_TAG = re.compile(r"<think>.*?</think>\s*", re.S)


def generate(question: str, contexts) -> str:
    ctx = _context(contexts)
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": _PROMPT.format(context=ctx, question=question)}],
        "stream": False,
        "think": False,            # qwen3 等思考型模型:不要把推理过程混进答案
        "options": {"temperature": 0.1},
    }
    try:
        data = _post(f"{OLLAMA_URL}/api/chat", payload, timeout=300)
    except requests.exceptions.HTTPError:
        payload.pop("think")       # 模型不支持 think 参数时退回普通调用
        data = _post(f"{OLLAMA_URL}/api/chat", payload, timeout=300)
    content = (data.get("message") or {}).get("content", "")
    return _THINK_TAG.sub("", content).strip()   # 兜底:万一仍带 <think> 标签就剥掉


def stream_generate(question: str, contexts):
    """流式生成：逐块 yield 文本，避免用户盯着静止的「检索中」等几十秒。"""
    ctx = _context(contexts)
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": _PROMPT.format(context=ctx, question=question)}],
        "stream": True,
        "think": False,
        "options": {"temperature": 0.1},
    }
    with requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=600, stream=True) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            piece = (data.get("message") or {}).get("content", "")
            if piece:
                yield piece
            if data.get("done"):
                break


def list_docs(layers) -> list:
    """当前用户有权限看到的文档清单（去重），用于前端空状态展示。"""
    col = collection()
    res = col.get(where={"layer": {"$in": list(layers)}}, include=["metadatas"])
    seen = {}
    for m in res.get("metadatas") or []:
        src = m.get("source")
        if src and src not in seen:
            seen[src] = {"source": src, "layer": m.get("layer"), "chunks": 0}
        if src:
            seen[src]["chunks"] += 1
    return sorted(seen.values(), key=lambda d: (d["layer"], d["source"]))


def get_doc(source: str, layers):
    """按原顺序拼回整篇文档。layers 来自服务端解析的角色 —— 越权文档取不到。"""
    col = collection()
    res = col.get(
        where={"$and": [{"source": {"$eq": source}}, {"layer": {"$in": list(layers)}}]},
        include=["documents", "metadatas"],
    )
    docs, metas = res.get("documents") or [], res.get("metadatas") or []
    if not docs:
        return None                      # 不存在，或该用户无权访问 —— 一律当作不存在
    pairs = sorted(zip(metas, docs), key=lambda p: p[0].get("idx", 0))
    return {
        "source": source,
        "layer": pairs[0][0].get("layer"),
        "chunks": [{"idx": m.get("idx", i), "text": d} for i, (m, d) in enumerate(pairs)],
    }


def cited_sources(contexts) -> list:
    """命中片段 → 去重后的来源清单（含标题/发文字号/官方链接，供前端展示与溯源）。

    比上下文更严：只列出最佳片段落在 CITE_MARGIN 内的文档。
    进上下文的沾边文档不该出现在「来源」里 —— 那等于宣称它是依据。
    """
    if not contexts:
        return []
    cut = min(c["distance"] for c in contexts) + CITE_MARGIN
    seen, sources = set(), []
    for c in contexts:
        s = c.get("source")
        if not s or s in seen or c["distance"] > cut:
            continue
        seen.add(s)
        sources.append({k: c.get(k) for k in ("source", "layer", "title", "doc_no", "url")})
    return sources


NO_CONTENT = "知识库中没有相关内容"


def sources_for(answer_text: str, contexts) -> list:
    """答案说「没有相关内容」时不挂来源。

    距离闸门挡不住这一类：问「公司年会预算」，最佳距离 0.438 —— 比无关问题近
    （天气 0.612），又远不如真命中（0.27）。与其猜阈值，不如认模型的判断：
    它说没答上来，就没有依据可言。
    """
    if not answer_text or NO_CONTENT in answer_text:
        return []
    return cited_sources(contexts)


def answer(question: str, roles) -> dict:
    layers = allowed_layers(roles)
    ctxs = retrieve(question, layers)
    if not ctxs:
        return {"answer": "在你有权限访问的资料里没有找到相关内容。", "sources": []}
    ans = generate(question, ctxs)
    return {"answer": ans, "sources": sources_for(ans, ctxs)}
