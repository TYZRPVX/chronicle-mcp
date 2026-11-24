# Vivaldi 本机 stdio 安全审查

日期：2026-11-10（Asia/Singapore）。范围：当前 checkout 的入口、配置、查询、
写入调用链、日志、出站请求，以及本机锁定的运行依赖。不是完整渗透测试或安全保证。

## 结论

适合在本次修复后的本机 stdio 配置下进行只读历史检索。源码检查没有发现
搜索时自动上传浏览历史的调用链。动态测试阻断 socket connect/bind 后，
工具发现、搜索及禁写响应仍正常工作。返回给 Codex 的结果按用户要求不脱敏。

保留了完整工具列表，因此客户端仍能显式调用批量导出、书签、下载和分析工具。
不是 OS 沙箱：拥有同一用户权限的其他代码仍能访问文件，读取结果也会进入模型上下文。

## 已修复

| 问题 | 处理与位置 |
| --- | --- |
| MCP 默认参数写死 Chrome | `protocols/mcp.py` 对省略/null browser 解析配置，显式参数优先 |
| 每次查询复制整库，临时文件名可预测 | `connection.py` 直接只读打开，不再调用临时副本流程；旧 helper 仅为兼容保留 |
| 主库文件复制可能遗漏 WAL 提交 | SQLite 正常只读模式，合成 WAL 用例验证新提交可见 |
| 删除副本却报告成功，实际同步可写真实库 | service 层拒绝实际写入，统一 `WRITES_DISABLED`；保留预览 |
| 脱敏配置未生效 | `database/utils.py` 接通 `sanitize_urls` 和 `sensitive_params`；本机配置关闭脱敏 |
| 搜索词与异常内容进入日志 | 搜索日志移除查询内容；数据库、webhook、订阅、HTTP 异常只记录类别；协议/HTTP 库关闭 DEBUG/INFO 日志 |
| stdout 启动提示干扰协议 | `cli.py` 改用 stderr；两个 stdio 入口验证 JSON-RPC 输出 |
| FastMCP banner 自动联网检查版本 | `cli.py`、`server.py` 关闭 banner；配置示例同时禁用更新检查 |
| Starlette 版本上限阻止安全升级 | `pyproject.toml` 改为 `>=1.3.1,<2.0.0`；本机锁定 1.10.0 |

URL 过滤器仍只处理配置中的 query 参数名称。开启它不等于对标题、路径、
userinfo 或 fragment 做全面脱敏。当前用户配置关闭该过滤器。

## 依赖检查

- CPython 3.12.13，macOS arm64；`pip-audit 2.10.1`，默认 PyPI 漏洞服务。
- 最初解析到 Starlette 1.0.1：扫描返回 8 条记录，对应 4 个不同 PYSEC ID，
  存在重复来源。包括表单资源耗尽、URL authority 混淆、HTTP 方法分派、Windows UNC SSRF。
- 修改约束并重新解析后，70 个锁定运行依赖扫描结果为 **0 条已知漏洞**。
- 关键版本：FastMCP 3.4.11、MCP 1.30.0、Starlette 1.10.0、HTTPX 0.28.1。
- [运行依赖锁](../../requirements-runtime.lock)包含完整版本与哈希；
  [最终扫描 JSON](runtime-audit-2026-11-10.json)保留完整包清单。
- 扫描只涉及包名/版本与公开依赖元数据，不包含浏览历史。
- 复现：`pip-audit -r requirements-runtime.lock --no-deps --disable-pip --format json`。
  `--no-deps` 不遗漏间接依赖：锁文件已经列出全部 70 个包。

上游依据：[Starlette advisories](https://github.com/Kludex/starlette/security/advisories)，
[表单 DoS 修复版本 1.3.1](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq)。
已知漏洞扫描不检测所有恶意依赖、未知漏洞或供应链篡改；哈希固定的是下载内容，
不等于信任证明。没有逐行审计所有依赖，也未审计 Python/OS 本身。
FastMCP 的 OpenTelemetry API 在此锁定环境没有配置 SDK/exporter；不是历史上传服务。

## 保留的问题与非启用功能

- `protocols/http.py`：没有认证；CORS 不等于认证。没有启动该服务。
- `cli.py`：`mcp --sse` 仍调用 REST server，不是真正 MCP SSE。本次不使用。
- `database/search_advanced.py` 和原删除逻辑：低层写入代码仍保留，写入一致性、
  浏览器关联表维护没有修复。service 层已经拦截，不提供解禁开关。
- `webhooks.py`：显式注册/启动后能够向 URL POST 事件。搜索未连接此调用链；
  stdio 入口不启动 worker。HTTP 文档页另有 jsDelivr CDN 引用。
- `core/realtime.py`：订阅管理器使用线程池；普通查询不提交订阅任务。
  现有代码未形成完整浏览器变更监听链，本次不承诺实时订阅完整可用。
- `cli.py`：HTTP daemon 使用临时 PID/log 文件和 subprocess；本次不启用。
  `Dockerfile` 默认 HTTP 绑定 `0.0.0.0`，不能按本次 stdio 结论部署到网络。
- `.github/workflows/security_scan.yml` 的扫描命令允许失败继续，CI 绿灯不能作为安全依据。
- 安装使用 setuptools 标准 backend，未发现项目自定义安装执行脚本；本机启动不执行安装。

## 验证与性能

相关测试：84 passed、3 skipped。覆盖配置、查询、只读限制、WAL、锁超时、
原始 URL、日志和两个真实 stdio 子进程入口。ruff check/format 通过。
Starlette 升级后用进程内 TestClient 验证 `/health` 为 200；未启动 HTTP 端口。

早期 stdio 测试的路径 mock 不完整，调用失败且没有返回历史记录；随后修复了
connection 模块的绑定，并给子进程使用临时 HOME 和 SQLite 路径白名单。
最终验收数据全部来自合成库。
未对真实浏览器执行写入，也未导出真实历史；没有将真实浏览历史用作性能样本。

性能样本：140,795,904 bytes（约 134 MiB），240,000 条合成记录；每种模式
先测首次调用，再轮换测量 30 次。没有清除 OS 文件缓存，“首次”不代表磁盘冷启动。

| 模式 | 首次 ms | 中位 ms | P95 ms |
| --- | ---: | ---: | ---: |
| 原复制整库 + 连续词组 SQL | 175.51 | 156.44 | 178.35 |
| 只读直连 + 相同词组 SQL | 122.89 | 122.01 | 126.69 |
| 只读直连 + 新拆词查询 | 127.24 | 122.15 | 128.20 |

最终方案中位耗时约减少 22%。两种 SQL 在该样本返回相同 5 条记录；
新方案还包含 schema 检测、时间格式化和 URL 处理，原 SQL 对照未包含这些开销。
不能直接推断真实浏览器分布或其它机器的耗时。

复现：`.venv/bin/python scripts/benchmark_history.py`。
[原始结果](history-benchmark-2026-11-10.json)。没有测量模型生成耗时，也没有测量
完整 Codex 端到端耗时；它们不能用数据库耗时替代。


## 同日修正：浏览器保持打开时的独占锁

用户实际使用发现 `DATABASE_LOCKED`。前一版只读直连兼容性结论过宽：
本机结构探测返回 SQLITE_BUSY，存在 rollback journal、没有 WAL。
只读连接仍会被独占锁阻止，不是 MCP 未部署。

`connection.py` 现在先以 50 ms 锁等待探测，可直读时仍保持快速路径；
锁住时尝试至多 3 次私有临时副本。仅接受无 WAL、journal 不存在或头部
清零、主库/journal/WAL 文件的 inode/size/mtime/ctime 在复制前后一致的情况。
副本目录权限 0700、文件权限 0600，quick_check 通过后只读查询，正常或异常
退出均清理。活动 journal、锁住的 WAL、源文件变化均拒绝；不修改源库。

这是尽力检查文件稳定性的兼容方案，不是数据库锁保护下的事务级备份，
不能承诺获得最新事务；quick_check 也不能证明所有语义一致性。
[SQLite 官方备份注意事项](https://www.sqlite.org/howtocorrupt.html#_backup_or_restore_while_a_transaction_is_active)
说明并发复制的限制。未采用忽略锁的 immutable 读取原库。

新增/相关测试 24 passed。另在真实 Vivaldi 保持运行时仅执行结构探测，
通过临时副本成功，耗时约 306 ms；没有输出浏览记录，未做真实历史检索测试。
此前 122 ms 性能数据只代表无独占锁的直读路径，不代表回退路径。
旧 MCP 进程需重新加载才会采用新代码；无需退出 Vivaldi。
