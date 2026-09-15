# 客户端协议 v1

只连接配置的 HTTPS 地址，拒绝重定向。地址可包含固定前缀，如 /quant-agent。

## 激活

POST /api/access/challenge，JSON {invite_code,channel,device_public_key}，返回 {challenge_id,challenge,expires_at,channel,device_public_key}。

Ed25519 签名以下 UTF-8 字节，换行 LF，末尾无换行：

```text
quant-agent-device-v1
<challenge_id>
<challenge>
<channel>
```

POST /api/access/activate，JSON {challenge_id,signature}，返回 {ok:true,access_token,account_id,device_id,channel,expires_at}。公钥与签名采用无填充 base64url。挑战 120 秒，会话 12 小时，网页与 MCP 独立设备槽。

## 请求签名

POST /api/mcp/call，JSON {tool,arguments}，响应 {ok,data?,error?:{code,message},request_id?}。

头部 Authorization: Bearer token、X-Quant-Timestamp（Unix 秒）、X-Quant-Nonce（随机 base64url）、X-Quant-Signature。签名字节：

```text
quant-agent-request-v1
<大写METHOD>
<包含固定前缀与query的URL path>
<实际请求body的SHA256小写hex>
<token的UTF-8 SHA256小写hex>
<timestamp>
<nonce>
```

服务器从 token 查设备公钥，时间窗口 ±60 秒，拒绝 nonce 重放。GET body 为空。验签必须使用实际收到的请求体，不重新序列化 JSON。

## 调度结果通道

调度结果复用 `query` 工具，不增加能够读取服务端文件或私有实现的接口。调用前先用 `catalog` 取得获准的精确 `model_id`。请求已发布的最新信号：

```json
{"tool":"query","arguments":{"module":"<model_id>","operation":"scheduled/latest","params":{"model_id":"<model_id>"}}}
```

成功响应的 `data` 包含 `model_id`、`result`、`signal`、`schedule`。可核实的 live latest 必须同时满足：

- `signal.schema_version == "quant-agent-current-signal/1.0"`
- `signal.eligible == true` 且 `signal.blockers` 为空
- `signal.model_id` 等于请求的 `model_id`
- `signal.data_as_of == schedule.data_as_of`
- `signal.signal_date == schedule.period`
- 在账户获准读取完整 signal 时，对 signal 使用 UTF-8 编码的规范 JSON（键排序、无空白、禁止 NaN）计算 SHA256，结果等于 `schedule.signal_sha256`

任一字段缺失、被权限投影隐藏或校验不一致时，客户端不得把结果标为 live，也不得从历史序列自行推导持仓。`execution_confirmed` 表示执行确认状态；值为 false 不会单独推翻其他完整的 live 证明。

请求最近一次获准的真实调度研究结果：

```json
{"tool":"query","arguments":{"module":"<model_id>","operation":"scheduled/research","params":{"model_id":"<model_id>","download":false}}}
```

研究结果必须保留 `channel="research"`、`research=true`、`is_live=false`、`header.research_result_is_live=false`。任务成功、日期较新、存在当前持仓或目标持仓都不会把该通道升级为 live；调用者还必须拥有任务本人或主动共享访问权，并通过当前字段、日期、底层数据及下载权限复核。

`model_result` 不属于调度 live 通道，只读取获准的定稿历史版本。HTTP 成功、任务 `completed` 或结果中的单个布尔字段均不足以证明 live latest。`scheduled/latest` 不可用时，客户端必须原样报告错误，不能改查 `scheduled/research`、`model_result` 或其他模型来替代。

## 网页组件

仅监听 http://127.0.0.1:47631。Host 必须匹配该 loopback 地址及端口，Origin 必须精确匹配已配置 HTTPS 服务器来源。支持 CORS 与本机网络访问预检。

- GET /v1/health：组件状态。
- POST /v1/web-activate，{invite_code}：组件直接向配置服务器激活 web 设备，返回 activation JSON。
- POST /v1/web-sign，{method,path,body,access_token}：返回 {ok,path,headers,body_text}。浏览器必须原样发送 body_text，GET 不发送 body。

网页通过签名 POST /api/access/web-session 建立中心服务器 cookie；cookie 不替代设备签名。组件拒绝任意主机、管理操作及通用签名，具体允许路径见代码并须与服务端同步。浏览器对本机访问的体验需要逐项验收。

The browser must fetch the returned normalized path (including the server prefix and encoded UTF-8 filename) and send body_text unchanged. The path is validated after decoding each segment; encoded slashes, traversal, NUL and double encoding are rejected.


## 结构化数据续页

db_query 保留 dataset、fields、filters、date_from、date_to、limit、download 参数，另接受可选 cursor。响应新增 pagination，包含 has_more、next_cursor、page_rows、page_size、end_of_query、complete、consistency、cursor_idle_seconds。

首次请求省略 cursor；后续请求原样传 next_cursor，其他查询参数保持一致。end_of_query=true 后累计结果才完整；complete 仅在单页即完整查询时为 true。服务端每页重新检查账户权限并验证源数据库未发生提交变化。data_cursor_restart_required 或 data_source_changed 要求丢弃未完成的分页结果并重新开始；客户端不得自动混合两个不同查询版本。游标不包含可读的私有位置键，也不能代替下载授权。

approval_request 只创建待确认申请。只有用户本人通过账密登录私有后台并确认具体内容，独立服务才可能执行；工具返回申请或入队成功不代表正式变更已执行。

## 会话恢复和本机控制

GET /v1/health 返回 {ok,version,channel:"web",server_origin,server_url,protocol:"quant-agent-device/1",device_public_key}，安装器必须比较完整 server_url、协议及本机 OS 保存的 web 公钥，不能仅凭 HTTP 200。

POST /v1/web-session 接受 {} 或 {refresh:boolean}，要求与激活相同的精确 Origin/Host，返回 {ok,access_token,expires_at,account_id,device_id,channel:"web"}。默认恢复有效加密会话，到期或 refresh=true 时仅使用 web 邀请码和原密钥重新挑战；没有已存邀请码返回 device_not_activated，服务器撤销不能回退旧 token。它不激活 MCP。浏览器以恢复结果重新签名建立中心 web-session；网络不明时不自动重提写任务。

POST /v1/control/stop 只供本机安装命令：Host 精确 loopback、没有 Origin，X-Quant-Device-Control 必须匹配 OS 加密的本机控制秘密。秘密不在健康/激活/网页响应中返回，网页也无此头部的 CORS 权限。成功返回 {ok:true,status:"stopping"} 后正常退出，supervisor 仅对异常退出有限重试。

解绑保留被禁用公钥，challenge 和 activate 均拒绝 device_revoked。管理员账密后台可显式恢复旧设备，新挑战激活才获得新 token，旧 token 保持撤销；普通用户的槽已有新设备时，不允许恢复旧设备顶替新设备。
