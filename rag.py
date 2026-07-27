"""检索增强核心：全部走本地 Ollama（embedding + 生成）和本地 Chroma。

关键：retrieve() 在向量检索层用 layer 元数据过滤，
越权内容根本进不到大模型上下文，而不是事后删。
"""
import os
import re
import json
import time
import requests
import chromadb
import config
from config import (OLLAMA_URL, LLM_MODEL, EMBED_MODEL, CHROMA_PATH, COLLECTION,
                    TOP_K, MAX_DISTANCE, REL_MARGIN, CITE_MARGIN,
                    LLM_PROVIDER, ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY,
                    ANTHROPIC_MODEL, LLM_MAX_TOKENS)
from permissions import allowed_layers

_client = None

EMBED_BATCH = 16


def collection():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})


def _post(url: str, payload: dict, timeout: int, tries: int = 3, headers: dict = None):
    """带退避重试。模型冷加载时首次调用容易超时,重试一次通常就好了。"""
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(url, json=payload, timeout=timeout, headers=headers)
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


# 这些缩写满库都是,不具区分度,关键词补召回时跳过
_KW_STOP = {"US", "GAAP", "IFRS", "IAS", "ASC", "CAS", "HKFRS", "FASB", "IASB",
            "SEC", "EY", "KPMG", "PWC", "GLM"}


def _keyword_hits(where: dict, question: str, per_term: int = 3, dist: float = 0.40) -> list:
    """关键词精确补召回。向量检索对纯英文缩写(如"什么是CGU")召回弱 —— 三字母查询语义信号太少，
    真正含该词的片段反而进不了前几名。这里对查询里的英文词/缩写做子串精确匹配把它们补回来。
    给一个中等距离 0.40：向量命中很准(<0.32)时盖不过它，向量很弱(>0.48)时它能顶上。"""
    # 只补"生僻大写缩写"(CGU/ECL/LIFO 这种向量抓不到的);
    # 跳过 US/GAAP/IFRS 等满库都是的高频词,否则会捞回一堆随机片段挤掉真正相关的。
    terms = [t for t in re.findall(r'[A-Za-z]{2,}', question)
             if t.isupper() and 2 <= len(t) <= 6 and t not in _KW_STOP]
    if not terms:
        return []
    col = collection()
    out, seen = [], set()
    for t in terms:
        try:
            res = col.get(where=where, where_document={"$contains": t},
                          limit=per_term, include=["documents", "metadatas"])
        except Exception:
            continue
        for d, m in zip(res.get("documents") or [], res.get("metadatas") or []):
            m = m or {}
            key = (m.get("source"), d[:40])
            if key in seen:
                continue
            seen.add(key)
            out.append({"text": d, "source": m.get("source"), "layer": m.get("layer"),
                        "title": m.get("title"), "url": m.get("url"),
                        "doc_no": m.get("doc_no"), "distance": dist})
    return out


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
    # 关键词补召回(补向量对英文缩写的短板),去重后并入
    seen = {(h["source"], h["text"][:40]) for h in hits}
    for kh in _keyword_hits(where, question):
        key = (kh["source"], kh["text"][:40])
        if key not in seen:
            hits.append(kh)
            seen.add(key)
    if not hits:
        return []
    # 相关性闸门:向量库总会凑满 TOP_K,不过滤就会把无关文档塞进上下文并列为来源。
    best = min(h["distance"] for h in hits)
    if best > MAX_DISTANCE:
        return []                                  # 全库都不沾边 → 老实说查不到
    return [h for h in hits
            if h["distance"] <= MAX_DISTANCE and h["distance"] <= best + REL_MARGIN]


_PROMPT = """你是财务知识库助手。只依据下面的资料回答问题；资料里没有的，直接说“知识库中没有相关内容”，不要编造，尤其不要编造任何数字或具体规定。

术语解释：如果问题是问某个术语/缩写是什么（如“什么是CGU”），而该术语在资料中出现过，请结合资料里它的上下文用法把它解释清楚（例如它属于哪个准则、用在什么场景），不要因为资料里没有一句正式定义就答“没有相关内容”。若该术语在资料中根本没出现，才回答没有相关内容。

排版要求：分段作答，不要挤成一大段。
- 若问题涉及不同准则/口径的对比（如 US GAAP 与 IFRS、中国准则与国际准则），必须分成独立段落：先一段讲一方的处理（如“US GAAP：……”），再一段讲另一方（如“IFRS：……”），最后可用一段点明核心差异。
- 每段用一个空行分隔，段首可用“US GAAP：”“IFRS：”“中国准则：”“差异：”这类小标题引导。

引用要求：涉及具体规定时，在句中标明依据的文件名称和条款（例如“依据《企业会计准则第14号——收入》第五条”）。不要在末尾另起一行罗列来源——来源由系统单独展示。

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


# ---- 运行时模型切换：切当前 provider 存到文件，不改 .env、不重启 ----
_PROVIDER_FILE = os.path.join(os.path.dirname(CHROMA_PATH) or ".", "active_provider.txt")


def available_providers() -> list:
    """当前可选的 provider：配了 key 的才算就绪；ollama 本地免费永远可选。"""
    out = ["ollama"]
    for name, (base, key, model) in config._OPENAI_FAMILY.items():
        if key:
            out.append(name)
    if ANTHROPIC_API_KEY:
        out.append("anthropic")
    return out


def active_provider() -> str:
    """当前生效的 provider：优先读运行时覆盖文件，否则回退 .env 的 LLM_PROVIDER。"""
    try:
        with open(_PROVIDER_FILE, encoding="utf-8") as f:
            p = f.read().strip().lower()
            if p:
                return p
    except FileNotFoundError:
        pass
    return LLM_PROVIDER


def set_active_provider(name: str):
    name = (name or "").lower()
    if name not in available_providers():
        raise ValueError(f"该模型不可选（可能没配 key）：{name}")
    os.makedirs(os.path.dirname(_PROVIDER_FILE) or ".", exist_ok=True)
    with open(_PROVIDER_FILE, "w", encoding="utf-8") as f:
        f.write(name)


def current_llm() -> tuple:
    """返回当前生效的 (provider, base_url, api_key, model)。每次调用实时读取，支持热切换。"""
    p = active_provider()
    fam = config._OPENAI_FAMILY.get(p)
    if fam and fam[1]:                                 # OpenAI 兼容且有 key
        return p, fam[0], fam[1], fam[2]
    if p == "anthropic" and ANTHROPIC_API_KEY:
        return "anthropic", ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, ANTHROPIC_MODEL
    return "ollama", "", "", LLM_MODEL                 # 兜底：本地免费


def _anthropic_headers(key: str) -> dict:
    return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}


def _complete(msgs, max_tokens: int = None) -> str:
    """按当前 provider 做一次非流式补全，返回纯文本。问答/翻译共用。"""
    provider, base, key, model = current_llm()
    if provider == "anthropic":
        data = _post(f"{base.rstrip('/')}/v1/messages",
                     {"model": model, "max_tokens": max_tokens or LLM_MAX_TOKENS,
                      "temperature": 0.1, "messages": msgs},
                     timeout=300, headers=_anthropic_headers(key))
        parts = [b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text"]
        return "".join(parts).strip()
    if provider != "ollama":                           # GPT / DeepSeek / Kimi
        body = {"model": model, "messages": msgs, "temperature": 0.1, "stream": False}
        if max_tokens:
            body["max_tokens"] = max_tokens
        data = _post(f"{base.rstrip('/')}/chat/completions", body,
                     timeout=300, headers={"Authorization": f"Bearer {key}"})
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    payload = {"model": model, "messages": msgs, "stream": False, "think": False,
               "options": {"temperature": 0.1}}
    try:
        data = _post(f"{OLLAMA_URL}/api/chat", payload, timeout=300)
    except requests.exceptions.HTTPError:
        payload.pop("think")       # 模型不支持 think 参数时退回普通调用
        data = _post(f"{OLLAMA_URL}/api/chat", payload, timeout=300)
    content = (data.get("message") or {}).get("content", "")
    return _THINK_TAG.sub("", content).strip()   # 兜底:万一仍带 <think> 标签就剥掉


def generate(question: str, contexts) -> str:
    prompt = _PROMPT.format(context=_context(contexts), question=question)
    return _complete([{"role": "user", "content": prompt}])


def translate(texts: list) -> list:
    """把一组英文段落逐条翻成简体中文，顺序对齐返回。用当前选中的模型翻。"""
    texts = [t for t in (texts or []) if t and t.strip()]
    if not texts:
        return []
    numbered = "\n\n".join(f"[{i+1}] {t}" for i, t in enumerate(texts))
    prompt = ("把下面每一条英文（会计准则相关）准确翻译成简体中文。规则：\n"
              "1) 严格按 [序号] 开头逐条输出译文，条与条之间空一行；\n"
              "2) 只输出译文，不要重复英文原文，不要加解释；\n"
              "3) 专业术语用会计通行译法（如 CGU=现金产出单元、carrying amount=账面价值、"
              "recoverable amount=可收回金额、fair value=公允价值）。\n\n" + numbered)
    raw = _complete([{"role": "user", "content": prompt}], max_tokens=2000)
    out = {}
    for m in re.finditer(r'\[(\d+)\]\s*(.*?)(?=\n\s*\[\d+\]|\Z)', raw, re.S):
        out[int(m.group(1))] = m.group(2).strip()
    return [out.get(i + 1, "") for i in range(len(texts))]


def stream_generate(question: str, contexts):
    """流式生成：逐块 yield 文本，避免用户盯着静止的「检索中」等几十秒。"""
    ctx = _context(contexts)
    prompt = _PROMPT.format(context=ctx, question=question)
    msgs = [{"role": "user", "content": prompt}]
    provider, base, key, model = current_llm()
    if provider == "anthropic":
        with requests.post(f"{base.rstrip('/')}/v1/messages",
                           json={"model": model, "max_tokens": LLM_MAX_TOKENS,
                                 "temperature": 0.1, "messages": msgs, "stream": True},
                           headers=_anthropic_headers(key), timeout=600, stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                s = line.decode("utf-8") if isinstance(line, bytes) else line
                if not s.startswith("data:"):
                    continue
                try:
                    ev = json.loads(s[5:].strip())
                except ValueError:
                    continue
                if ev.get("type") == "content_block_delta":
                    piece = (ev.get("delta") or {}).get("text", "")
                    if piece:
                        yield piece
        return
    if provider != "ollama":                           # GPT / DeepSeek / Kimi
        with requests.post(f"{base.rstrip('/')}/chat/completions",
                           json={"model": model, "messages": msgs, "temperature": 0.1, "stream": True},
                           headers={"Authorization": f"Bearer {key}"},
                           timeout=600, stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                s = line.decode("utf-8") if isinstance(line, bytes) else line
                if not s.startswith("data:"):
                    continue
                s = s[5:].strip()
                if s == "[DONE]":
                    break
                piece = (json.loads(s).get("choices") or [{}])[0].get("delta", {}).get("content", "")
                if piece:
                    yield piece
        return
    payload = {"model": model, "messages": msgs, "stream": True, "think": False,
               "options": {"temperature": 0.1}}
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


def used_passages(contexts, k: int = 4) -> list:
    """回答实际依据的原文片段(供前端自动展示"参考原文")。去掉入库时加的【标题】前缀。"""
    out = []
    for c in contexts[:k]:
        txt = re.sub(r'^【[^】]*】\s*', '', c.get("text", "")).strip()
        out.append({
            "source": c.get("source"), "title": c.get("title") or c.get("source"),
            "layer": c.get("layer"), "text": txt[:600],
        })
    return out


def answer(question: str, roles) -> dict:
    layers = allowed_layers(roles)
    ctxs = retrieve(question, layers)
    if not ctxs:
        return {"answer": "在你有权限访问的资料里没有找到相关内容。", "sources": []}
    ans = generate(question, ctxs)
    return {"answer": ans, "sources": sources_for(ans, ctxs)}
