# AL-1S Discord Worker

Discord Worker 使用 Sapphire 与 discord.js 接收新消息，只按平台下发的已启用 Discord 消息规则匹配文字、藏头或频率条件。命中事件经加密本地队列提交给平台；平台重新核对规则版本，并分别处理 QQ 私聊、QQ群和 SMTP 动作。Worker 不直接发送 QQ 消息。

Bot 配置只需 Discord Token 与应用 ID。来源服务器、频道、触发条件和目标在通知设置的统一「通知规则」中配置。旧提及指定用户及 `@everyone` 转发路径已停用，历史配置版本仅保留作为记录。规则为空时不会转发。

队列中的历史格式消息不会按新规则重放；Worker 将其加密隔离并记录 `legacy_path_retired`。已隔离内容按现有队列保留策略处理，不输出正文到普通日志。

生产部署使用 [Bot Compose](../../al1s-deployment/bots/compose.yaml)，配置从平台 Bot Worker 接口获取。`BOT_RUNTIME_CONFIG_PATH` 只供独立本地开发。Worker 健康状态不等于 Discord 账号或目标通知通道可用。

## 本地检查和容器构建

从本目录执行，本地开发需要 Node.js 24：

```sh
npm ci
npm run check
docker build -t al1s-plachta:local .
```

Dockerfile 会恢复锁定依赖并编译 TypeScript，构建镜像不需要先在宿主机执行 npm。也可从平台仓库根目录执行：

```sh
docker build -f backend/al1s-bot/Dockerfile -t al1s-plachta:local backend/al1s-bot
```

构建不代表已接入 Discord。运行前在平台创建 Discord 连接和一次性 Worker 注册码，将注册码安全放入 Worker 持久目录的 `registration-code` 文件；注册后 Worker 保存独立凭据，并使用平台下发的 Discord Token。Bot Compose 的外部控制网络必须与实际平台网络一致；首次部署还需配置日志收集入口，账号资料和队列保持持久化。完整配置入口见[平台 README](../../README.md)。

容器名沿用 `al1s-discord-worker`、`al1s-llbot`；镜像 tag 可以修改，账号目录、凭据、队列和部署名称不能随镜像更新被重建或改名。
