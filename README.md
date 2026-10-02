# AL-1S Platform

AL-1S 包含管理平台、独立 Bot 服务，以及 Linux / Android 终端。使用 [AL1S-next.code-workspace](AL1S-next.code-workspace) 打开多根工作区。

本机完整规格和运行台账在私有 `docs/` 目录维护，不随 GitHub 源码发布。各子项目 README 提供公开的构建与操作入口。

| 目录 | 内容 |
|---|---|
| [al1s-frontend/](https://github.com/KizunaAkari/al1s-frontend) | 平台前端 |
| [backend/al1s-backend/](backend/al1s-backend/README.md) | 平台后端、执行内核、Maa 与信息管理 |
| [backend/al1s-bot/](backend/al1s-bot/README.md) | 独立 Bot Worker |
| [terminals/al1s-terminal-linux/](https://github.com/KizunaAkari/al1s-terminal/tree/main/al1s-terminal-linux) | Linux 终端 |
| [terminals/al1s-terminal-android/](https://github.com/KizunaAkari/al1s-terminal/tree/main/al1s-terminal-android) | Android 终端 |
| [al1s-deployment/](al1s-deployment/README.md) | Compose、部署脚本与发布包 |
| docs/ | 本机私有的现行项目文档，不随克隆提供 |
| backups/ | 当前数据库恢复备份及原始迁移资产，本机保留 |
| data/、artifacts/、隐藏工具目录 | 运行资料、诊断证据与工具缓存 |

源码、运行数据、秘密与发布包分开保存。Dockerfile 跟随构建上下文，导出产物放在 `al1s-deployment/packages/{platform,linux,android}`。现场版本和停启状态仅在部署台账维护。

根 `.gitignore` 排除本机文档、备份、运行数据、诊断资料、工具与模型缓存、依赖及构建产物、真实环境文件和私钥；保留各子项目的 `.env.example` 等示例。复制示例时设置自己的凭据，不提交本机运行配置。依赖使用各子项目锁文件恢复，模型/终端资产按 Linux 子项目准备脚本维护。

## 克隆完整源码

需要 Git、Docker Engine 或 Docker Desktop（Linux 容器模式）、Docker Compose v2+。直接构建镜像不需要在宿主机安装 Node.js 和 Python；本地开发前端/Bot 使用 Node.js 24，后端使用 Python 3.12。

```sh
git clone --recurse-submodules https://github.com/KizunaAkari/al1s-platform.git AL1S
cd AL1S
```

前端子模块位于 `al1s-frontend/`，终端子模块位于 `terminals/`，均固定到平台提交记录的版本。已克隆但缺少子模块时执行：

```sh
git submodule update --init --recursive
```

## 构建容器镜像

以下命令从平台仓库根目录执行，只构建镜像，不启动服务、不迁移数据库。

```sh
# 完整平台：前端静态资源、API 和平台 worker
docker build -f al1s-deployment/platform/Dockerfile -t al1s-platform:local .

# 独立 Discord Worker
docker build -f backend/al1s-bot/Dockerfile -t al1s-plachta:local backend/al1s-bot

# 仅后端 API，供独立开发或迁移工具使用
docker build --target runtime -t al1s-backend:local backend/al1s-backend
```

完整平台构建会自行执行 `npm ci` 和前端生产构建，不需要先生成 `dist/`。单独前端镜像的命令见前端 README。

Dockerfile 固定基础镜像版本与摘要。无法访问默认镜像源时，可按对应 Dockerfile 的 `ARG`，使用 `--build-arg NODE_IMAGE=...`、`--build-arg PYTHON_IMAGE=...` 或前端的 `--build-arg NGINX_IMAGE=...` 指定可信、兼容的镜像。

Linux 终端使用 ARM64 构建，需先准备并校验离线资源包，见[终端构建说明](https://github.com/KizunaAkari/al1s-terminal#linux-终端容器)。Android 构建 APK，不运行在 Docker 容器中。

## 新环境的配置和 Compose 构建

下面用于新的独立环境。已有平台继续使用原部署配置和完整覆盖层，不以示例文件覆盖现有凭据、卷、网络或容器名称。

Linux / macOS / Git Bash：

```sh
cp al1s-deployment/.env.example al1s-deployment/.env
cp al1s-deployment/.env.tls.example al1s-deployment/.env.tls
```

PowerShell：

```powershell
Copy-Item al1s-deployment/.env.example al1s-deployment/.env
Copy-Item al1s-deployment/.env.tls.example al1s-deployment/.env.tls
```

编辑新文件，设置管理员密码（至少 12 字符），并为数据库、S3、MQTT、通知主密钥、离线许可签名密钥及 MQTT 会话签名密钥分别设置独立秘密。生产配置会拒绝公开占位值，32 字节密钥不能用短密码代替。在自己的机器上可分别生成随机值后填入配置：

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

TLS 示例使用 `al1s.local:8443`；修改域名或端口时同步配置公开平台地址、S3 公开地址、MQTT 地址和浏览器 Origin。长期凭据使用 HTTPS，管理 Cookie 保持 Secure。证书初始化与启动入口见[部署 README](al1s-deployment/README.md#新环境配置与构建)。

配置检查与 Compose 构建可单独执行：

```sh
docker compose -p al1s-platform --env-file al1s-deployment/.env -f al1s-deployment/compose/compose.unified.yaml config --quiet
docker compose -p al1s-platform --env-file al1s-deployment/.env -f al1s-deployment/compose/compose.unified.yaml build backend migrate s3-init
```

`config --quiet` 验证配置而不输出包含秘密的展开结果；`build` 不启动容器，数据库迁移只在另行启动的 `migrate` 服务中执行。Bot 的网络、账号目录和队列另行配置，见 Bot README。

上传前仍需检查 `git diff --cached`；忽略规则不能清除历史内容或保护已经跟踪的秘密。不要提交终端身份、QQ 账号目录或生成的 CA 私钥。
