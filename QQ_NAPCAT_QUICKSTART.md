# AstrBot 接入 QQ 群的最短路径

适用场景：`AstrBot + NapCat + QQ 个人号/QQ群`

当前主流链路是：

`QQ <- 登录在 NapCat 里的账号 <- OneBot v11 / 反向 WebSocket -> AstrBot`

也就是说，AstrBot 不直接连 QQ，而是通过 NapCat 这类 OneBot v11 协议实现端接进去。

## 1. 启动

在当前目录执行：

```bash
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) docker compose -f compose.qq.yml up -d
```

查看容器状态：

```bash
docker compose -f compose.qq.yml ps
```

## 2. 先登录 NapCat

查看 NapCat 日志，拿到 WebUI 地址和 token：

```bash
docker logs napcat
```

然后打开：

- NapCat WebUI: `http://你的机器IP:6099`
- AstrBot WebUI: `http://你的机器IP:6185`

AstrBot 默认账号密码通常是：

- 用户名：`astrbot`
- 密码：`astrbot`

NapCat 登录后，用要作为机器人的 QQ 号扫码。

## 3. 在 AstrBot 里创建 QQ 机器人

进入 AstrBot WebUI：

1. 左侧进入 `机器人`
2. 点击 `+ 创建机器人`
3. 选择 `OneBot v11`
4. 填写：

- `ID`：随便起，例如 `qq`
- `启用`：打开
- `反向 WebSocket 主机地址`：`0.0.0.0`
- `反向 WebSocket 端口`：`6199`
- `反向 WebSocket Token`：先留空

保存后，AstrBot 就会在容器内部等 OneBot 客户端连进来。

## 4. 管理员 ID

在 AstrBot WebUI 的 `配置文件 -> 平台配置` 里，把 `管理员 ID` 改成你的 QQ 号。

这里填你本人账号，不是机器人账号。

## 5. NapCat 这边怎么配

优先按当前官方的 Docker Compose 方案理解：

- 这个 `compose.qq.yml` 已经给 NapCat 设置了 `MODE=astrbot`
- NapCat 和 AstrBot 在同一个 Docker 网络里
- 正常情况下，你只需要完成 NapCat 登录，再在 AstrBot 启用 `OneBot v11`

如果你发现 NapCat 没有自动连上，再手动去 NapCat WebUI 增加一个 `WebSockets 客户端`：

- `URL`：`ws://astrbot:6199/ws`
- `消息格式`：`Array`
- `心跳间隔`：`5000`
- `重连间隔`：`5000`

注意：

- URL 最后必须带 `/ws`
- 不要把 NapCat 里要连接的地址写成 `0.0.0.0`

## 6. 怎么判断成功

去 AstrBot WebUI 的 `控制台` 看日志。

如果出现：

```text
aiocqhttp(OneBot v11) 适配器已连接。
```

说明 AstrBot 已经接到 NapCat。

这时候把机器人 QQ 拉进群里，或者先私聊机器人发 `/help`，就能验证通路。

## 7. 常用命令

启动：

```bash
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) docker compose -f compose.qq.yml up -d
```

看日志：

```bash
docker compose -f compose.qq.yml logs -f
```

停止：

```bash
docker compose -f compose.qq.yml down
```

## 8. 如果镜像下载太慢

你现在这台机器在中国大陆网络环境里，大镜像拉取可能比较慢。

可以直接带环境变量切到镜像站，不需要改文件：

```bash
ASTRBOT_IMAGE=m.daocloud.io/docker.io/soulter/astrbot:latest \
NAPCAT_IMAGE=m.daocloud.io/docker.io/mlikiowa/napcat-docker:latest \
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) \
docker compose -f compose.qq.yml up -d
```
