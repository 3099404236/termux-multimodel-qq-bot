# Antigravity Manager 迁移与单模型分享 Key

> [!WARNING]
> Manager 是第三方工具。其项目说明明确警告：使用第三方工具访问 Antigravity、Gemini CLI 或 Gemini Code Assist 可能违反相关条款，并可能触发账号暂停。优先使用 Google 官方 `agy`；只有在理解风险后，才用自己的账号启用 Manager/API 代理。

## 1. 远程 Docker OAuth

Google 不接受公网 IP 上的明文 HTTP OAuth 回调。远程 Manager 不要设置成 `ABV_PUBLIC_URL=http://公网IP:端口`；通过 SSH 把本机 `8045` 转发到服务器，再从本机访问后台：

```powershell
ssh -N -L 127.0.0.1:8045:127.0.0.1:8045 -i "私钥路径" root@服务器地址
```

打开 `http://127.0.0.1:8045/`，进入“账号管理 → 添加账号 → OAuth”，只使用弹窗本次新生成的授权链接。授权后若没有自动完成，按 Manager 界面提示提交完整 localhost 回调 URL 或点击“我已授权，继续”。OAuth URL 和回调 code 都是一次性的，不要提交到仓库或聊天记录。

## 2. 确认真实模型 ID

Manager 的模型列表来自已登录账号的动态配额。没有账号或尚未刷新配额时，Gemini 3.7 Flash 不会出现在界面中，`/v1/models` 也可能返回 503。账号加入后先刷新配额，再查询：

```bash
curl -sS -H "Authorization: Bearer $ANTIGRAVITY_MANAGER_API_KEY" \
  http://127.0.0.1:8045/v1/models \
  | jq -r '.data[].id' | grep 'gemini-3\.7-flash'
```

必须使用查询到的真实 ID；账号可能暴露带 `-high`、`-low` 或其他动态后缀的变体，不要仅凭界面显示名猜测。

## 3. QQ 机器人切换到 Manager

在手机私有环境文件中设置以下变量，不要提交填好的文件：

```bash
export ANTIGRAVITY_MANAGER_BASE_URL="https://你的受信任网关.example/v1"
export ANTIGRAVITY_MANAGER_API_KEY="只供机器人使用的Key"
export ANTIGRAVITY_MANAGER_MODEL="从-v1-models-查询到的真实ID"
export ANTIGRAVITY_MANAGER_TIMEOUT="300"
```

桥检测到 `ANTIGRAVITY_MANAGER_BASE_URL` 后直接走 HTTP；删除或留空这三个核心变量即可回退到原有 `agy` CLI。显式 GPT 路由仍由 ChatGPT 网页桥处理。

如果这里填写第 4 节的单模型受限网关，所有 Antigravity/Opus 路由都会被服务端固定为 Gemini 3.7 Flash；只有需要多模型路由的自用客户端才应连接私有 Manager，并且绝不能把 Manager 主 Key 分享给第三方。

## 4. 只允许 Gemini 3.7 Flash 的分享 Key

Manager 自带 User Token 支持有效期、最大 IP 数与宵禁，但当前没有逐 Token 模型白名单。不要把 Manager 主 API Key 交给第三方。

本仓库的 `deployment/vps/antigravity_flash_gateway.py` 提供额外隔离：

- 分享 Key 与 Manager Key 完全不同；
- `/v1/models` 只返回一个模型；
- 所有聊天、Responses 和 Anthropic 请求的 `model` 都会被服务端强制覆盖；
- 账号、配置和其他管理路径全部不可访问；
- 即使客户端在请求体中填写 Opus 或其他模型，实际仍只能调用配置的 Gemini 3.7 Flash。

部署时把脚本放到 `/opt/antigravity-flash-gateway/`，填写私有 `/etc/antigravity-flash-gateway.env`，安装配套 systemd unit。服务默认只监听 `127.0.0.1:18046`，必须在 Nginx/Caddy 等受信任反向代理后以 HTTPS 对外提供；不要直接公开明文 HTTP 端口。

验证限制：

```bash
curl -sS -H "Authorization: Bearer $FLASH_GATEWAY_API_KEY" \
  https://你的网关.example/v1/models
```

返回列表应只有一个 ID。再故意以其他模型名发一次聊天请求，并在 Manager 流量日志中确认最终模型仍是配置的 Gemini 3.7 Flash。
