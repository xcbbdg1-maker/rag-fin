# 财务知识库 RAG · 本地自建（含账号体系 + 权限分层）

完全本地部署的检索增强问答系统：**自带用户/登录/角色系统**，权限在向量检索层强制生效，文档、向量、模型全在你自己机器上，不调任何第三方 AI API。

## 它和之前几个版本的区别

| 版本 | 定位 |
|---|---|
| HTML 演示原型 | 面试讲解用的前端模拟，不能真用 |
| Claude 项目 | 云端，零搭建，个人查公开准则 |
| Ollama + AnythingLLM | 成品工具，本地私有，零代码 |
| **本项目（rag-fin）** | **自己的代码：自建账号体系、可深度定制、全本地** |

选本项目的理由只有一个：你要**自己的账号体系 + 深度定制**，成品满足不了。否则用 AnythingLLM 更省事。

## 架构

```
浏览器(登录/问答/建账号)
      │  JWT
   FastAPI (app.py)
      ├── 账号: db.py(SQLite) + security.py(bcrypt+JWT)
      ├── 权限: permissions.py  角色 → 可见内容层(all / finance)
      └── RAG: rag.py
              ├── embedding/生成 → Ollama(本地)
              └── 向量检索(带 layer 过滤) → Chroma(本地)
```

权限关键点：`/api/query` 的用户角色由**服务端从 JWT 解析**得到，前端无法伪造；检索时用 `layer` 元数据过滤，越权文档进不到大模型上下文。

## 部署步骤（本地）

### 1. 装本地模型运行器 Ollama
到 ollama.com 安装，然后拉模型：
```bash
ollama pull qwen3:8b       # 生成模型（config.py 的 LLM_MODEL）
ollama pull bge-m3         # 中文 embedding，1024 维
```
确认 Ollama 在跑：浏览器打开 http://localhost:11434 应显示 "Ollama is running"。

装完先做一次性调优，见下方「运维」的两个环境变量——不设的话每次问答会慢 2~3 倍。

### 2. 装 Python 依赖（建议 Python 3.10+）
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 改密钥、建初始账号
```bash
export SECRET_KEY="换成一段很长的随机字符串"        # Windows 用 set
python seed.py     # 生成 admin/finance/employee 三个账号（默认密码见输出）
```

### 4. 放文档并入库
把文件按可见层放进对应目录，再入库：
```
data/docs/all/        ← 全员可见（报销制度、发票要求、FAQ…）
data/docs/finance/    ← 财务专属（科目口径、月结手册、准则口径…）
```
```bash
python ingest.py
```
支持 PDF / DOCX / MD / TXT。扫描件 PDF 需先 OCR。

### 5. 启动
```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```
浏览器打开 http://localhost:8000 ，用 admin 登录 → 侧栏「新建账号」给同事建号并分配角色 → 开始提问。

## 验证权限分层

- 用 `employee` 登录问财务专属问题（如「月结折旧在哪步计提」）→ 应答不出（越权内容被过滤）。
- 用 `finance` 登录问同一问题 → 能答，并带来源。
- 这就是权限感知检索：不是事后删，而是越权内容根本没进上下文。

## 安全须知（务必看）

### 1. SECRET_KEY 必须换掉（最高优先级）

`config.py` 里的默认值 `change-me-to-a-long-random-string-please` 是**写死在代码里的公开字符串**。JWT 用它签名，因此任何拿到这份代码的人都能**伪造任意用户的令牌，包括 admin** —— 整个角色/分层权限体系当场失效，财务专属文档可被直接取走。改密码没用，必须换密钥。

生成并设置（Windows，用户级环境变量，设完重启服务）：
```powershell
$key = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 64 | % {[char]$_})
[Environment]::SetEnvironmentVariable("SECRET_KEY", $key, "User")
```
Linux/macOS：
```bash
export SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
```
确认已生效（应输出「已覆盖」而非「仍是默认占位串」）：
```bash
python -c "from config import SECRET_KEY; print('仍是默认占位串' if SECRET_KEY=='change-me-to-a-long-random-string-please' else f'已覆盖（长度 {len(SECRET_KEY)}）')"
```
> 换 `SECRET_KEY` 会让已签发的令牌全部失效，所有人需重新登录——这是预期行为。
> 密钥只放环境变量，**不要写回 `config.py`**，否则等于提交进 git。

### 2. 其余

- **首次登录后立刻改默认密码**（页面侧栏「修改我的密码」）。`seed.py` 建的 `admin/admin123` 等是公开的默认口令。
- **知识库文档不进 git**：`data/docs/` 已在 `.gitignore` 中。真实财务文档一旦提交，就永久留在仓库历史里，事后 `rm` + commit 也删不掉（需 `git filter-repo` / BFG 改写历史）。同样被忽略的还有 `data/users.db`（密码哈希）和 `data/chroma`（向量库里含原文片段）。
- 默认 `--host 127.0.0.1` 只本机可访问。要给局域网用，改 `0.0.0.0` 并放在反向代理(带 HTTPS)后面、配好防火墙，别裸奔。
- LLM 和 embedding 都走本地 Ollama，数据不出机器；不要为了省事把生成接到云端 API。

## 灌准则/法规时的头号陷阱：已废止版本

**这是本项目最危险的一类错误——比模型幻觉更危险，因为每个字都是真的，只是已经不算数了。**

财政部官网把**同一部准则的现行版和已废止的旧版并列挂在索引页上**（如第22号同时有 2006 和 2017 两版）。若一并入库，会发生：

| 提问 | 未隔离时的检索结果 |
|---|---|
| 「金融资产转移终止确认」 | **第 1 名是 2006 废止版**（距离 0.201，比现行版还近） |
| 「**套期保值**的会计处理」 | 8 条里 **7 条是废止版** |
| 「**原保险合同**保费收入」 | **4 条全是废止版** |

废止版之所以常排在**更前面**，是因为用户会用**旧术语**提问——「套期**保值**」是 2006 年的叫法，现行准则叫「套期**会计**」；「原保险合同」这个概念本身已被 2020 年的《保险合同》准则取消。用旧词提问，向量自然更贴近旧准则。于是系统会拿一份失效十年的准则给出权威答案，并附上财政部官方链接背书。

### 本项目的处理方式（两层）

1. **检索层隔离（主防线）**：文档 frontmatter 写 `document_type: "historical_standard"` → `ingest.py` 打上 `superseded: true` 元数据 → `retrieve()` 默认在 `where` 里排除。**废止内容根本进不到模型上下文**，与权限过滤同一思路。需要追溯历史版本时调用 `retrieve(..., include_superseded=True)`。
2. **正文横幅（保险丝）**：废止文档的每个片段强制冠上 `【已废止版本·仅供历史查询·不得作为现行会计处理依据】`。万一被显式检索到，模型也能看见并声明。

隔离后，用旧术语提问会被**正确导向对应的现行准则**（问「套期保值」→ 答《套期会计（2017）》）。

### 新增法规文档时的检查清单

- [ ] 原文**逐字**来自官方源（财政部/税务总局/人大法规库），不要用第三方转载，更不要让 AI 生成——**生成的准则是给知识库注入幻觉**。
- [ ] 确认是**现行版本**。同号多版时只留文号年份最新的。
- [ ] 已废止的：frontmatter 标 `document_type: "historical_standard"`，否则它会和现行准则一起被检索。
- [ ] 入库后**抽查**：随机取几份，重新抓官网原文比对是否逐字一致。
- [ ] 用**旧术语**试问几个问题，确认不会召回废止版。
- [ ] 改动分块或元数据后，**删掉 `data/chroma` 重新 ingest**，否则旧片段残留。

## 深度定制入口（你要改的地方）

- **权限模型**：`permissions.py` 的 `allowed_layers()`。想按部门/数据行更细，改这里 + 入库时打更细的 `layer`/元数据。
- **角色**：`app.py` 里 `valid={"employee","finance","admin"}`，加角色改这里。
- **对接公司账号体系**：现在是自带 SQLite 账号。若要接公司 LDAP/AD/OAuth，把 `db.get_user`/登录校验换成向你们身份源验证，令牌里带回角色即可，其余不用动。
- **提示词/拒答**：`rag.py` 的 `_PROMPT`。
- **分块/检索参数**：`config.py` 的 `CHUNK_SIZE`/`TOP_K` 等。

## 运维

### 两个必设的环境变量

都写在**用户级**环境变量里（`setx` 或系统设置 → 环境变量），改完要重启 Ollama 才生效。

| 变量 | 值 | 作用 |
|---|---|---|
| `OLLAMA_KEEP_ALIVE` | `1h` | 模型常驻内存。默认只有 5 分钟，两次提问间隔稍久模型就被卸载，每次都要重新加载（实测多花 **17.6 秒**）。 |
| `OLLAMA_IGPU_ENABLE` | `1` | **启用集成显卡**。Ollama 默认会主动丢弃集显（日志里写 `dropping integrated GPU`），只用 CPU。打开后 prompt 处理从 **41 tok/s 提到 220 tok/s**，单次问答从 44 秒降到 **18 秒**。 |

设完确认生效：
```bash
ollama ps
# PROCESSOR 列应该是 100% GPU（不是 100% CPU）
# UNTIL 列应该是 59 minutes from now（不是 4 minutes）
```

> 开机自启：Ollama 的快捷方式在「启动」文件夹里，开机会自动拉起托盘版，并读取上面这两个用户级变量，**无需额外配置**。
> 若你手动跑过 `ollama serve`，它会占住 11434 端口，导致托盘版启动时报 `existing instance found` 而退出——这种情况只发生在手动起过服务时，重启电脑即恢复。

### 慢下来时先查孤儿进程

反复重启 Ollama 时，父进程被杀而子进程 `llama-server` 可能残留，**每个占几 GB 内存**。堆积几个之后内存被吃光，模型被迫 swap 到磁盘，单次问答能从 20 秒劣化到**几十分钟**（真实踩过）。

```bash
ollama ps                 # PROCESSOR 列突然变回 100% CPU 就是信号
```
任务管理器搜 `llama-server`，或：
```powershell
Get-Process llama-server | Select Id, @{n='MEM_GB';e={[math]::Round($_.WorkingSet64/1GB,2)}}
Get-Process llama-server | Stop-Process -Force    # 全杀掉，下次请求会自动重新加载
```

### 做性能基准测试时别被缓存骗了

**同一个问题连问两次，第二次会命中 prompt 前缀缓存**，prompt 处理速度会虚高到 800+ tok/s，看起来像"7 秒就答完了"，实际换个问题就打回 40 秒。

**基准测试必须用几个不同的问题**，并看 Ollama 返回的分项耗时（`/api/chat` 响应里的字段，单位纳秒）：

| 字段 | 含义 | 正常值（本机 + GPU） |
|---|---|---|
| `load_duration` | 模型加载 | 热态 0.2~0.5s；若每次都十几秒 → keep_alive 没生效 |
| `prompt_eval_duration` / `prompt_eval_count` | 上下文处理（**主要瓶颈**） | ~220 tok/s；若只有 ~40 tok/s → 在用 CPU |
| `eval_duration` / `eval_count` | 生成 | ~10 tok/s |

上下文越长 prompt 处理越慢，所以 `TOP_K` 和 `CHUNK_SIZE` 直接决定速度——调大它们是在拿速度换召回质量。

## 常见问题

- **答案残缺/答非所问**：多半是上下文不够。用更大上下文的模型，或减小 `TOP_K`/`CHUNK_SIZE`。
- **检索召不回明明存在的内容**：`CHUNK_SIZE` 太大会把一份制度里多个不相干的主题塞进同一片，向量语义被稀释、排名掉出 `TOP_K`。本项目实测：800 时含答案的片段排第 8（进不了 TOP_K=5），调到 300 后排第 1。**改完分块必须删掉 `data/chroma` 重新 ingest**，否则旧片段会残留。
- **中文检索差**：确认 embedding 用 `bge-m3`；PDF 用带文本层的。
- **慢**：先看「运维」一节——多半是没设那两个环境变量，或有孤儿 `llama-server` 吃光了内存。
- **改了文档要重灌**：重跑 `python ingest.py`（用 md5 id，同文件同位置会 upsert 覆盖）。
- **`pip install` 失败**：`chroma-hnswlib` 在 Python 3.13 上无预编译包、要 MSVC 才能编译 —— 用 `chromadb>=1.5`（Rust 索引，无此依赖）。另外 `passlib 1.7.4` 与 `bcrypt>=4.1` 不兼容，必须钉 `bcrypt==4.0.1`。两条都已写进 `requirements.txt`。
