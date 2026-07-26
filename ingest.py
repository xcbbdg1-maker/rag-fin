"""把文档灌进本地向量库，并按目录打上 layer 标签。

目录约定：
  data/docs/all/       → 全员层
  data/docs/finance/   → 财务专属层
未知子目录默认归入 finance（最严格），避免误放导致越权可见。

用法：
  python ingest.py                 # 默认读 ./data/docs
  python ingest.py /path/to/docs
"""
import os, sys, glob, hashlib, re
from config import CHUNK_SIZE, CHUNK_OVERLAP
# 注意:rag(依赖 chromadb/Ollama)在 main() 里才导入,
# 这样 read_file/chunk/layer_of 可独立使用与测试。

LAYER_DIRS = {"all", "finance"}
EXTS = (".pdf", ".docx", ".md", ".txt")

_PARA_SEP = re.compile(r"\n\s*\n")
_SENT_END = re.compile(r"(?<=[。！？；.!?;])")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)

# frontmatter 里认这几个键,其余忽略。key → Chroma 元数据名。
_META_KEYS = {
    "title": "title",
    "source_url": "url",
    "document_number": "doc_no",
    "version_status": "version_status",
    "document_type": "doc_type",
}

# 已废止/历史版本的 document_type。这类文档必须能被识别出来:
# 实测过,用旧术语提问(如"套期保值"——现行准则已改叫"套期会计")时,
# 废止版的向量距离比现行版更近,会排在检索结果第一位。
# 若不隔离,系统会拿废止十年的准则当权威答案,还附财政部官方链接背书。
_HISTORICAL_TYPES = {"historical_standard", "superseded", "abolished"}
_STALE_BANNER = "【已废止版本·仅供历史查询·不得作为现行会计处理依据】"


def parse_frontmatter(text: str):
    """拆出 YAML frontmatter → (元数据, 正文)。

    没有 frontmatter 就原样返回。这里只做扁平的 key: value 解析——
    知识库文档不需要嵌套结构,不值得为此引入 yaml 依赖。
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in _META_KEYS and v:
            meta[_META_KEYS[k]] = v
    return meta, text[m.end():]


def read_file(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages)
    if ext == ".docx":
        import docx
        return "\n".join(p.text for p in docx.Document(path).paragraphs)
    if ext in (".md", ".txt"):
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    return ""


def _split_units(text: str):
    """切成最小语义单元:先段落,再句末标点。"""
    units = []
    for para in _PARA_SEP.split(text):
        for sent in _SENT_END.split(para.strip()):
            sent = sent.strip()
            if sent:
                units.append(sent)
    return units


def _hard_split(s: str, size: int, overlap: int):
    """兜底:单个单元本身就超长(如无标点的长表格行),只能按字数硬切。"""
    out, i = [], 0
    step = max(1, size - overlap)
    while i < len(s):
        piece = s[i:i + size].strip()
        if piece:
            out.append(piece)
        i += step
    return out


def chunk(text: str, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """在段落/句子边界处切分,避免把一个条款从中间截断;超长单元才退回硬切。

    重叠通过"把上一块尾部的若干完整句子带进下一块"实现,而非按字数截断。
    """
    text = text.strip()
    if not text:
        return []
    out, cur, cur_len, fresh = [], [], 0, False

    def flush():
        nonlocal cur, cur_len, fresh
        if not cur or not fresh:
            return
        out.append("\n".join(cur).strip())
        tail, tail_len = [], 0
        for u in reversed(cur):                 # 尾部完整句子作为重叠带入下一块
            if tail_len + len(u) + 1 > overlap:
                break
            tail.insert(0, u)
            tail_len += len(u) + 1
        cur, cur_len, fresh = tail, tail_len, False

    for unit in _split_units(text):
        if len(unit) > size:
            flush()
            cur, cur_len, fresh = [], 0, False
            out.extend(_hard_split(unit, size, overlap))
            continue
        if cur and cur_len + len(unit) + 1 > size:
            flush()
        cur.append(unit)
        cur_len += len(unit) + 1
        fresh = True
    flush()
    return [c for c in out if c]


def layer_of(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    top = rel.split(os.sep)[0].lower()
    return top if top in LAYER_DIRS else "finance"


def ingest_file(path: str, layer: str, on_progress=None) -> int:
    """把单个文件入库,返回片段数。命令行和网页上传共用这一条通道。

    两点保证,使得重复上传同名文件不会污染检索:
      1. 先向量化、成功后才删旧片段 —— 中途失败时库里仍是旧版本,不会出现空洞;
      2. 按 source 删干净再写 —— 新版本片段更少时不会残留上一版的尾巴
         (旧实现按 md5(path:i) upsert,片段数变少就会留下孤儿片段被检索到)。
    """
    from rag import embed_many, collection

    raw = read_file(path)
    meta, body = parse_frontmatter(raw)
    name = os.path.basename(path)
    title = meta.get("title") or os.path.splitext(name)[0]

    superseded = meta.get("doc_type", "") in _HISTORICAL_TYPES

    parts = chunk(body)
    if not parts:
        return 0
    # 每片冠上文档标题:片段正文里往往不出现准则名(如第14号收入准则中段只写"第X条…"),
    # 不加前缀就检索不到"收入准则怎么规定的"这类按名提问。
    # 废止版另加横幅:即便被检索到(如显式查历史版本),模型也能看见并声明其已失效。
    head = f"{_STALE_BANNER}\n【{title}】" if superseded else f"【{title}】"
    texts = [f"{head}\n{p}" for p in parts]

    embs = embed_many(texts, on_progress=on_progress)   # 先算,失败则不动库

    col = collection()
    col.delete(where={"source": name})                  # 再清旧版本
    base = {"source": name, "layer": layer, "title": title, "superseded": superseded}
    for k in ("url", "doc_no", "version_status"):
        if meta.get(k):
            base[k] = meta[k]
    col.add(
        ids=[hashlib.md5(f"{name}:{i}".encode()).hexdigest() for i in range(len(texts))],
        embeddings=embs,
        documents=texts,
        # idx 用于把片段按原顺序拼回整篇（前端「查看原文」弹窗要用）
        metadatas=[dict(base, idx=i) for i in range(len(texts))],
    )
    return len(texts)


def main(root="./data/docs"):
    files = [p for p in glob.glob(os.path.join(root, "**", "*"), recursive=True)
             if os.path.isfile(p) and os.path.splitext(p)[1].lower() in EXTS]
    if not files:
        print(f"没有可入库文件。请把文档放到 {root}/all 或 {root}/finance 下。")
        return
    total, failed = 0, []
    for n, path in enumerate(files, 1):
        name, layer = os.path.basename(path), layer_of(path, root)
        print(f"[{n}/{len(files)}] {name} [{layer}] — 向量化中…", flush=True)
        try:
            cnt = ingest_file(path, layer, on_progress=lambda done, tot: print(
                f"      {done}/{tot} 片", end="\r", flush=True))
        except Exception as e:
            print(f"      ✗ 向量化失败：{e}")
            failed.append(name)
            continue
        if cnt == 0:
            print(f"      — 没读到文本，跳过            ")
            continue
        total += cnt
        print(f"      ✓ 入库 {cnt} 片            ")
    print(f"\n完成，共入库 {total} 个片段。")
    if failed:
        print(f"失败 {len(failed)} 个文件：{', '.join(failed)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "./data/docs")
