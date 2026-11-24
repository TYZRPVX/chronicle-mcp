# Vivaldi + Codex 本机历史检索

使用本机 stdio。保留完整工具列表；删除、去重删除和实际同步返回
`WRITES_DISABLED`，预览与 `dry_run` 可用。HTTP/SSE 不在本次接入范围。

## 已准备的本机环境

- checkout：`/Users/x/workspace/oss/forks/chronicle-mcp`
- Python：`.venv/bin/python`，CPython 3.12.13 / macOS arm64。
- 运行依赖：`requirements-runtime.lock`，包含直接与间接依赖的版本和哈希。
- 配置：`examples/vivaldi.toml`，默认 Vivaldi，URL 不脱敏，日志 WARNING。
- 数据源：`~/Library/Application Support/Vivaldi/Default/History`。

不需要每次通过 `uvx` 或 `pip` 下载和解析依赖。当前 `.venv` 已安装本地项目。
在相同平台重建环境：

```sh
cd /Users/x/workspace/oss/forks/chronicle-mcp
uv venv --python 3.12.13 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements-runtime.lock
uv pip install --python .venv/bin/python --no-deps -e .
```

锁文件针对本机 Python/平台解析；更换平台或 Python 版本时重新解析并审查。
`-e .` 使用当前 checkout，运行依赖固定不代表本地源码不可变。
构建工具的临时隔离环境不属于运行依赖锁定范围。

## 接入 Codex

将 `examples/codex-vivaldi.toml` 中的配置合并到 Codex 配置。
本次没有自动修改 `~/.codex/config.toml`。
移动 checkout 后需要修改示例中的三个绝对路径。

配置依据：[OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
使用 `command`、`args`、`cwd` 和 `env` 启动固定的本地 Python 进程。
保存配置后重新加载 MCP，确认 `chronicle` 可用。

可以问：

> 帮我查一下 Vivaldi 历史，我以前打开过一个 live config 页面，URL 是什么？

工具调用示例：

```json
{"query": "live config", "limit": 5}
```

省略 `browser` 使用 `CHRONICLE_BROWSER`，其次 TOML `[default].browser`，
最后保留项目原来的 `chrome` 默认值。显式 `browser` 优先。
本机示例同时固定配置文件路径和 `CHRONICLE_BROWSER=vivaldi`。

搜索按空白拆词，每个词可以命中标题或 URL，全部词都命中才返回。
`live config` 可以找到 `Live Configuration`、`live-config`，
也能找到标题含 `live`、URL 含 `config` 的页面。结果按最近访问排序。
基础搜索仍支持原有 SQL LIKE 通配符；高级搜索的 regex/fuzzy 行为不变。
没有结果时缩短关键词，或调用日期/域名搜索；不会自动抓取网页正文。

## 数据与运行边界

- 返回结果保留原始 URL，包括 query 和 fragment；匹配结果会进入 Codex 上下文。
- 如需恢复原有 query 参数过滤，将 `[security].sanitize_urls` 设为 `true`。
  这不是完整秘密检测：标题、路径、fragment 等不在该过滤器覆盖范围内。
- 查询优先以 `mode=ro` 和 `query_only` 直读；初次锁探测最多 50 ms，
  正常查询锁等待为 1 秒。遇到浏览器保留的独占锁时，只在无 WAL、rollback
  journal 不存在或头部已清零、复制前后文件状态一致时使用私有临时副本。
  副本通过 quick_check 后只读查询，退出即删除；不建立持久化索引。
  活动 journal、锁住的 WAL 或持续变化的源文件明确失败。
  这是尽力获得稳定副本，不是 SQLite 事务级备份，结果可能略滞后。
  SQLite 可能使用其 WAL/SHM 协调文件，不使用 `immutable` 忽略最新提交。
- stdio 入口关闭 FastMCP banner，避免其自动检查更新；示例额外设置
  `FASTMCP_CHECK_FOR_UPDATES=off`。查询不启动 HTTP、webhook worker 或订阅。
- 网页标题与 URL 是不可信数据，不能作为执行命令或发送数据的指令。
- 审查结果与性能数据见 [安全审查](security/2026-11-10-vivaldi-review.md)。
