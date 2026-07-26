"""配置。生产环境务必用环境变量覆盖 SECRET_KEY。"""
import os

# 认证
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-a-long-random-string-please")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("TOKEN_MINUTES", "480"))

# 本地模型（Ollama）
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3:8b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")

# 本地向量库（Chroma，内嵌，无需另起服务）
CHROMA_PATH = os.getenv("CHROMA_PATH", "./data/chroma")
COLLECTION = os.getenv("COLLECTION", "fin_kb")

# 用户库（SQLite）
DB_PATH = os.getenv("DB_PATH", "./data/users.db")

# 检索/分块
# CHUNK_SIZE=800 会把一份制度里多个不相干的口径塞进同一片，向量语义被稀释、
# 检索排名被挤到 TOP_K 之外（实测：问业务招待费，含答案的片段排第 8）。
# 调到 300 后同一片段排第 1。改动分块后必须删掉 data/chroma 重新 ingest。
TOP_K = int(os.getenv("TOP_K", "8"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "300"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "60"))

# 相关性闸门。向量检索永远会返回 TOP_K 条,哪怕全都不相关 ——
# 问「如何编制现金流量表」时报销管理办法照样被塞进上下文并列为来源。
# 两道闸:绝对距离上限(全库都不相关时直接判定为"查不到"),
# 以及相对最佳命中的容差(命中很准时,把勉强沾边的甩掉)。
MAX_DISTANCE = float(os.getenv("MAX_DISTANCE", "0.55"))
REL_MARGIN = float(os.getenv("REL_MARGIN", "0.08"))

# 展示给用户的「来源」比喂给模型的上下文更严格。
# 上下文宁可多给几片(答得全)；来源列出来就是在说"依据是它"，混进沾边文档等于误导。
# 实测：问「如何编制现金流量表」，第22号金融工具准则里一句"在估计现金流量时…"
# 距离 0.321、落在 0.08 容差内 —— 进上下文无妨，列进来源就是错的。
CITE_MARGIN = float(os.getenv("CITE_MARGIN", "0.05"))

# 上传
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
