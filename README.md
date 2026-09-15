# 量化研究 MCP 客户端

本目录是独立的公开客户端发布边界。客户端只向管理员提供的 HTTPS 服务器提交结构化请求，模型计算、数据库查询和研究任务均在服务器执行。无需完整项目仓库；公开发行不得附带模型源码、公式、内部参数、数据、后台代码或凭据。

## 登录与设备

同一个邀请码对应同一用户，分别绑定一个网页设备槽和一个 MCP 设备槽。两端使用不同的 Ed25519 密钥，可以分别位于两台电脑。激活和后续请求均需设备签名，切换网络不改变绑定。管理员决定解绑、撤销及访问权限。

网页组件仅监听 `127.0.0.1:47631`，只接受安装时配置的网站来源，不提供公网端口、文件读取、通用代理、任意消息签名或管理后台操作。浏览器需允许该网站访问本机组件；隐私或组织策略可能要求授权。各浏览器组合必须实测，不能宣称已支持所有浏览器。

设备私钥、网页与 MCP 邀请码及会话凭据使用 Windows 当前用户 DPAPI 或 macOS Keychain 保护，没有明文回退。“设备”也受操作系统用户配置影响：更换 OS 用户、重装系统或损坏凭据后可能需要管理员重新绑定。操作系统保护不等于硬件不可复制承诺，不得导出或同步凭据目录。

## Windows + Codex

公开客户端仓库为 `https://github.com/tequilal1920-netizen/quant-strategy-agent-mcp`，正式服务地址为 `https://desktop-i22b489.tailf9d7ac.ts.net/quant-agent`。仓库首次发布完成后，可下载 GitHub ZIP，或执行 `git clone --depth 1 https://github.com/tequilal1920-netizen/quant-strategy-agent-mcp.git`，进入仓库根目录后安装。

需要可用的 Codex 客户端。安装器优先使用 Python 3.11+；缺失时，从 python.org 下载固定版本 Python 3.13.15，核对 SHA256 与 Python Software Foundation 签名后，仅为当前用户安装，不改系统 PATH。可用 `-Python` 指定现有 Python，或用 `-NoPythonDownload` 禁止下载。运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -ServerUrl "https://desktop-i22b489.tailf9d7ac.ts.net/quant-agent"
```

在无回显提示中输入邀请码，不把它写进命令行、Codex 对话、配置或 Git。脚本安装独立 Python 环境，保存 HTTPS 配置，创建设备凭据，登记本机组件随当前用户登录启动，并尝试注册 Codex STDIO MCP。脚本不改动正式服务器。

只安装网页组件时使用 `-WebOnly`（兼容 `-SkipActivation -SkipCodexRegistration`）。随后在网站输入邀请码，MCP 槽不会因此激活。默认注册当前用户登录计划任务；`-ManualStart` 可跳过自启动，之后每次系统登录须手动运行 `start-device`。

## macOS + Codex

优先使用 Python 3.11+。缺失时，安装器核验官方固定版本 universal2 包后打开 macOS Installer，由本机用户完成系统安装确认；可用 `--python /绝对路径/python3` 指定已有环境，或用 `--no-python-download` 禁止下载：

```sh
sh install.sh "https://desktop-i22b489.tailf9d7ac.ts.net/quant-agent"
```

只装网页组件时添加 `--web-only`；另支持 `--skip-activation`、`--skip-codex-registration`、`--manual-start`。使用独立环境、Keychain 和当前用户 LaunchAgent，安装时立即加载组件。**macOS 安装、Keychain、Python 引导和浏览器交互仍需 Mac 实机验收，Windows 测试不能替代。**

## Codex 配置

脚本使用官方支持的注册形式：

```text
codex mcp add quant-agent -- <安装环境的python绝对路径> -I -B -m quant_agent_mcp.server
```

CLI 不在 PATH 时，在 Codex 设置 → MCP servers 新增 STDIO 服务，command 填脚本输出的 Python 绝对路径，args 填 `-I -B -m quant_agent_mcp.server`，然后重启连接。不要将邀请码填进环境变量或工具参数。依据 [OpenAI 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

## 工具

| 工具 | 功能 |
|---|---|
| catalog / permissions / health | 已授权模型、权限与服务状态 |
| query / model_result | 正式模型查询及真实版本结果 |
| db_catalog / db_query | 按授权字段和范围读取数据库 |
| job_submit / job_list / job_status / job_result / job_cancel | 私有研究任务创建、查看、取消 |
| job_share / job_revoke_share / job_fork / job_delete | 共享、撤回共享、派生及删除获准研究任务 |
| job_artifacts / artifact_read | 枚举研究产物并按权限分段读取或下载 |
| code_save / code_read | 保存及读取账户私有研究代码 |
| approval_request | 仅提交关键操作申请，没有批准或发布工具 |

### 最新调度结果

先调用 `catalog`，从 `model_run_contract` 读取服务器当前支持的调度查询，并使用目录返回的精确 `model_id`。查询已发布的最新信号：

```json
{
  "module": "<model_id>",
  "operation": "scheduled/latest",
  "params": {"model_id": "<model_id>"}
}
```

这是 MCP 工具 `query` 的参数。成功响应仍使用统一信封 `{ok,data,request_id}`；结果位于 `data`，包含 `model_id`、`result`、`signal` 和 `schedule`。只有同时满足以下条件时，客户端才能把它解释为 live latest：`signal.schema_version` 为 `quant-agent-current-signal/1.0`、`signal.eligible=true`、`signal.blockers=[]`，且 `signal.model_id`、`signal.data_as_of`、`signal.signal_date` 分别与请求模型及 `schedule.data_as_of`、`schedule.period` 一致。获得完整 signal 字段授权时，还应按 UTF-8、键排序、无空白 JSON 计算 SHA256，并与 `schedule.signal_sha256` 比较。缺少任一判定字段或只有部分字段权限时，结果视为无法核实，不能补全或推断交易信号。`execution_confirmed=false` 可以表示已验证但尚未执行的模型目标，不能单独否定 live 状态。

查询最近一次获准的真实调度研究结果：

```json
{
  "module": "<model_id>",
  "operation": "scheduled/research",
  "params": {"model_id": "<model_id>", "download": false}
}
```

该通道还要求任务属于当前账户或已由创建者主动共享，并继续执行模型、底层数据、字段、日期和下载权限。成功结果明确包含 `channel="research"`、`research=true`、`is_live=false` 和 `header.research_result_is_live=false`。即使任务执行成功、带有最新日期、持仓或目标权重，它仍是 research-only，不能当作可执行最新信号。撤销共享后，原结果会立即失去访问权限。

`model_result` 永远不能作为 live latest 的判定入口。它读取管理员保留的定稿历史版本；任务状态 `completed`、历史结果里的 `is_live_signal` 字段或 HTTP 200 也都不能替代上述 live 判定。服务器没有可验证的已发布信号时，`scheduled/latest` 会明确返回不可用错误，不会回退到研究结果或其他模型。

任务类型由服务器声明，例如 `model.query`、`model.result`、`database.query`、`research.design`、`research.run`。每次逻辑提交提供唯一 `idempotency_key`，网络响应不明确时沿用原键并先查状态。

服务器执行字段、时间、下载和研究空间权限。获准用户可以查看数据库目录、模型框架、详细结果及策略配置；密钥、精确私密参数和正式算法源码不返回。结果为结构化 JSON，不得用演示值或其他版本替代实际结果。客户端没有搜索或下载正式模型源码、读取服务端文件或运行本地模型脚本的工具。删除研究任务不能删除正式数据，申请不能替代管理员网页确认。

## 数据分页

db_query 的 limit 是单页大小，服务器单页最多 10,000 行，不代表全部获准数据的总量。响应包含 pagination；has_more=true 时，将 next_cursor 作为下一次 cursor，并保持数据集、字段、过滤条件、日期、limit 和 download 完全一致。累计每页 rows，直到 end_of_query=true。

complete=true 只说明本次单页响应已包含完整查询，不能把最后一页单独当作全量结果。游标绑定账户和本次查询，不可转给其他用户；源库更新、权限变化、服务重启或游标过期后应从第一页重新查询，不能拼接新旧结果。无法证明稳定顺序的数据源会明确拒绝分页。

research.run 会在服务器读取输入查询的全部分页后才运行代码，并保存逐页授权来源。完整输入超过管理员的单任务容量时明确拒绝，不运行截断数据。

## 文件与验证

Windows 配置及 DPAPI 密文位于 `%LOCALAPPDATA%\QuantAgentClient\`，自启动使用当前用户 `QuantAgentDevice-<SID>` 计划任务，注册成功后移除本产品旧 Run 项。macOS 配置位于 `~/Library/Application Support/QuantAgentClient/`，凭据在 Keychain，自启动文件为 `~/Library/LaunchAgents/com.quant-agent.device.plist`。研究产物由服务器收纳在账户目录，客户端不向当前打开的项目输出文件。

核心依赖固定：MCP 1.27.1、HTTPX 0.28.1、cryptography 46.0.3、Pydantic 2.12.4、AnyIO 4.10.0、psutil 7.0.0，macOS Keyring 25.7.0；测试 pytest 8.4.2。目标系统的传递依赖和平台轮子仍需分别检验。

在 `quant_agent_mcp` 目录执行：

```text
python -B -m pytest -p no:cacheprovider tests
```

公开发布只包括明确的源码、安装脚本、测试与说明，排除虚拟环境、egg-info、凭据、缓存、数据库、日志及私有项目 Git 历史。上线与发布仍须管理员批准。本次代码修改不等于已上传、已上线或 Mac 已验收。


Research code operations require both `job_id` and `name`. `job_fork` takes the source `job_id` and creates an unchanged private copy; submit a new task to change its specification. `model_result`, `db_query` and `job_result` accept `download=true` when requesting export; the server checks the relevant download permissions. `model_result.result_version` must match a version actually supported by the server; an unknown version is an error.

## 会话恢复与组件维护

网页重开或换用同一 OS 用户下的浏览器时，可从本机组件恢复 web 会话；即将到期时沿用原 web 密钥和 OS 加密邀请码续期。MCP 同样自动续期，两槽独立。管理员禁用、撤销邀请码或解绑后由服务器拒绝；解绑的旧设备不能自动抢占空槽，须管理员显式恢复授权，旧 token 不会复活。

端口固定 47631。status 实际核验完整服务器地址、协议与本机 web 公钥，端口被其他程序占用时失败，不停止未知进程。restart-device 安全停止后重启并核验；崩溃由 supervisor 有限重试，明确停止不会自行复活。disable-startup 移除本产品自启动，保留凭据。使用安装器输出的 Python 绝对路径：

```text
<python> -I -B -m quant_agent_mcp.cli status
<python> -I -B -m quant_agent_mcp.cli restart-device
<python> -I -B -m quant_agent_mcp.cli stop-device
<python> -I -B -m quant_agent_mcp.cli disable-startup
```

公开包是源码和在线安装器，**不是离线免依赖发行包**。依赖从 Python/PyPI 获取，构建只在系统 Temp 并清理，不向项目生成 egg-info 或缓存。引导 URL/SHA256 来自 [Python 3.13.15 官方发行页](https://www.python.org/downloads/release/python-31315/)；升级需重新核验发行物和两平台测试。
