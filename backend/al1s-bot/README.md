# AL-1S Discord Worker

Discord Worker 使用 Sapphire 与 discord.js 接收新消息，只按平台下发的已启用 Discord 消息规则匹配文字、藏头或频率条件。命中事件经加密本地队列提交给平台；平台重新核对规则版本，并分别处理 QQ 私聊、QQ群和 SMTP 动作。Worker 不直接发送 QQ 消息。

Bot 配置只需 Discord Token 与应用 ID。来源服务器、频道、触发条件和目标在通知设置的统一「通知规则」中配置。旧提及指定用户及 `@everyone` 转发路径已停用，历史配置版本仅保留作为记录。规则为空时不会转发。

队列中的历史格式消息不会按新规则重放；Worker 将其加密隔离并记录 `legacy_path_retired`。已隔离内容按现有队列保留策略处理，不输出正文到普通日志。

正式Worker仅支持平台连接，使用 [Bot Compose](../../al1s-deployment/bots/compose.yaml) 并从平台 Bot Worker 接口获取配置；缺少`AL1S_CONTROL_BASE_URL`时在启动前明确拒绝。`BOT_RUNTIME_CONFIG_PATH`只用于`runtimeEnvironment`配合`loadConfig`的本地解析检查，不支持脱离平台完整运行。Worker 健康状态不等于 Discord 账号或目标通知通道可用。

## 本地检查和容器构建

从本目录执行，本地开发需要 Node.js 24：

```sh
npm ci
npm run check
docker build -f Dockerfile -t al1s-plachta:local ..
```

Dockerfile 会恢复锁定依赖并编译 TypeScript，构建镜像不需要先在宿主机执行 npm。也可从平台仓库根目录执行：

```sh
docker build -f backend/al1s-bot/Dockerfile -t al1s-plachta:local backend
```

构建不代表已接入 Discord。运行前在平台创建 Discord 连接和一次性 Worker 注册码，将注册码安全放入 Worker 持久目录的 `registration-code` 文件；注册后 Worker 保存独立凭据，并使用平台下发的 Discord Token。Bot Compose 的外部控制网络必须与实际平台网络一致；首次部署还需配置日志收集入口，账号资料和队列保持持久化。完整配置入口见[平台 README](../../README.md)。

容器名沿用 `al1s-discord-worker`、`al1s-llbot`；镜像 tag 可以修改，账号目录、凭据、队列和部署名称不能随镜像更新被重建或改名。

## 表情包功能后端

同一个`al1s-discord-worker`镜像包含Node Discord进程和Python 3.12表情功能后端，由容器内Supervisor分别管理。功能后端在内部3101端口提供模板/素材/预览/指令接口，并接收QQ OneBot事件；QQ不依赖Discord登录。`al1s-llbot`继续QQ登录/协议收发。平台仅转发受管理员/Worker鉴权保护的管理请求并发布前端，不新建功能容器。

Bot构建上下文是`backend/`，需要相邻`al1s-backend`的Python源码及锁文件；`backend/.dockerignore`排除凭据、运行资料与无关文件。普通构建安装锁定依赖并编译TS。网络不可用时，可先在本机运行`npm run check`，再使用`NODE_BUILD_STAGE=node-cached`及经核对依赖一致的`NODE_CACHED_RUNTIME_IMAGE`进行离线打包；此模式校验每个生产依赖的精确版本，dist必须来自本次源码检查，不能把旧dist当新源码。`PYTHON_IMAGE`也可指定依赖完全一致且经过核对的既有Python运行镜像；默认仍是固定Python基础镜像。

部署字段见Bot `.env.example`：独立的`AL1S_MEME_BACKEND_TOKEN`及功能后端数据库/S3/秘密解密能力。平台配置`AL1S_MEME_BACKEND_URL=http://al1s-discord-worker:3101`及相同控制Token。真实值只进入受保护部署环境，不入镜像/源码/浏览器；字段未配置时功能后端停用，现有Discord进程仍独立运行。实际授予运行能力和迁移仍走授权的兼容发布流程，后端不会自行创建schema或执行迁移。

管理员在Bot管理→表情包工坊编辑模板，在QQ/Discord连接的表情设置配置来源范围；QQ事件地址来自原生OneBot正向WS服务，复用已应用的HTTP地址和令牌。两端的表情结果、用法和错误回复分别遵守全平台10秒/30秒固定间隔；限流不补发提示或排队重放，预览不占外发名额，原通知/转发不受本功能门禁影响。
