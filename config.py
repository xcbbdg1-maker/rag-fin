"""配置。生产环境务必用环境变量覆盖 SECRET_KEY。"""
import os
from dotenv import load_dotenv
load_dotenv()   # 读取项目根的 .env（含云端模型 API key 等），.env 已被 gitignore

# 认证
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-to-a-long-random-string-please")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("TOKEN_MINUTES", "480"))

# 生成模型：把各家 key 都填进 .env，切换只改 LLM_PROVIDER 这一行。
# 可选值：ollama(本地免费) / anthropic(Claude) / openai(GPT) / deepseek / kimi / bailian(阿里云百炼)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()

# OpenAI 兼容家族（GPT / DeepSeek / Kimi / 阿里云百炼 都走同一套协议）：
#   provider -> (base_url 固定, 各自的 key, 默认模型可用 *_MODEL 覆盖)
_OPENAI_FAMILY = {
    "openai":   (os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                 os.getenv("OPENAI_API_KEY", ""),   os.getenv("OPENAI_MODEL", "gpt-4o")),
    "deepseek": ("https://api.deepseek.com/v1",
                 os.getenv("DEEPSEEK_API_KEY", ""), os.getenv("DEEPSEEK_MODEL", "deepseek-chat")),
    "kimi":     ("https://api.moonshot.cn/v1",
                 os.getenv("MOONSHOT_API_KEY", ""), os.getenv("KIMI_MODEL", "moonshot-v1-32k")),
    "bailian":  ("https://dashscope.aliyuncs.com/compatible-mode/v1",
                 os.getenv("DASHSCOPE_API_KEY", ""), os.getenv("BAILIAN_MODEL", "qwen-plus")),
}
# 解析当前选中的 OpenAI 兼容 provider（选了 ollama/anthropic 时这三个为空，不影响）
OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL = _OPENAI_FAMILY.get(LLM_PROVIDER, ("", "", ""))

# Claude 原生（协议和 OpenAI 不同，单独一套）
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "1500"))

# 本地模型（Ollama）
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3:8b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")   # 向量化始终走本地,免费且检索够用

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
