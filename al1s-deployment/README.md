# AL-1S 部署

本项目负责 Compose、TLS、部署脚本和发布包。公开克隆入口见[平台 README](../README.md)；现场运行版本、数据身份和恢复条件在本机私有部署台账维护，不随源码发布。

已有平台的项目名为 `al1s-platform`，应用容器为 `al1s-platform-app`；Bot 项目为 `al1s-bot`，容器为 `al1s-llbot` 和 `al1s-discord-worker`；Linux 容器为 `al1s-terminal-linux`。升级和重建沿用既定名称，核对完整 Compose 覆盖结果和命令行项目名，避免基础文件或工作目录默认值改变现用环境。

## 新环境配置与构建

先按照[平台 README](../README.md#克隆完整源码)递归克隆三个源码仓库。下面的镜像构建命令从工作区根目录执行，不要求已经存在本机发布包：

```sh
docker build -f al1s-deployment/platform/Dockerfile -t al1s-platform:local .
docker build -f backend/al1s-bot/Dockerfile -t al1s-plachta:local backend/al1s-bot
```

新环境复制 `.env.example`、`.env.tls.example` 到本机环境文件，替换全部秘密占位值并设置管理员密码。TLS 的公开平台/S3/MQTT 地址与证书 SAN 要一致；默认配置以 `al1s.local` 为例。仅在新环境生成 CA，现有平台不得覆盖当前 CA：

```powershell
./al1s-deployment/scripts/Initialize-Tls.ps1 -DnsName al1s.local -IpAddress 127.0.0.1
```

Linux / Git Bash：

```sh
sh al1s-deployment/scripts/initialize-tls.sh al1s.local --ip 127.0.0.1
```

浏览器/终端需要信任公开 `ca.crt`。TLS 初始化生成的私钥留在本机，不进入 Git 或终端。

独立新环境可以先验证配置，再构建基础 Compose 所需的镜像：

```sh
docker compose -p al1s-platform --env-file al1s-deployment/.env --env-file al1s-deployment/.env.tls -f al1s-deployment/compose/compose.unified.yaml -f al1s-deployment/compose/compose.tls.yaml config --quiet
docker compose -p al1s-platform --env-file al1s-deployment/.env --env-file al1s-deployment/.env.tls -f al1s-deployment/compose/compose.unified.yaml -f al1s-deployment/compose/compose.tls.yaml build backend migrate s3-init
```

准备确认后，在全新环境中用同一组参数执行 `up -d --wait` 启动；数据库迁移和 S3 初始化是一次性服务。该基础组合为独立新环境使用，不包含现有验收平台的 external 数据卷、宿主管理、日志与 Bot 控制覆盖层；已有环境继续使用下方管理入口，不以这组参数改建现有平台。Bot 和 Linux 的 Compose 需要另行配置外部控制网络、日志收集器、身份和设备挂载。

## 已初始化平台的日常操作

在工作区根目录执行：

```powershell
./al1s-deployment/scripts/Manage-Platform.ps1 -Action Status
./al1s-deployment/scripts/Manage-Platform.ps1 -Action Validate
```

需要停启时使用同一脚本的 Start / Stop。Start 使用既定配置且不构建、不迁移，但可能重建配置发生变化的服务；操作前按台账核对镜像、活动任务与挂载。

脚本完整加载环境文件与 Compose 覆盖层，包括 compose.logging.yaml。先启动独立日志收集器，再切换业务容器的 syslog 驱动；收集器用独立持久卷按 Docker 事件时间保留最多 7 天并物理清理。现有 external 卷/网络的历史 smoke 名称是有效数据身份，不能省略 compose.platform.yaml，也不能作为临时测试残留删除。不要用旧 smoke 启动示例重建该实例。

平台所在电脑的访问地址、远端地址及 TLS 配置只在部署台账维护；不要把本机回环地址配置给远端终端。

## 构建与新环境

| 内容 | 入口 |
|---|---|
| 完整平台 | [platform/Dockerfile](platform/Dockerfile)，构建上下文为工作区根目录 |
| 前端专项更新 | [platform/Dockerfile.frontend-update](platform/Dockerfile.frontend-update)；先在 al1s-frontend 构建，以 dist 为上下文，基底必须为明确核对过的兼容镜像 |
| 平台基础 Compose | [compose/compose.unified.yaml](compose/compose.unified.yaml) |
| TLS / 宿主管理 / 当前平台覆盖 | compose.tls.yaml、compose.host-management.yaml、compose.platform.yaml |
| Linux 终端 | [linux/compose.arm64.yaml](linux/compose.arm64.yaml)；先安装本机回环 syslog 收集器服务，再重建终端容器 |
| Bot | [bots/compose.yaml](bots/compose.yaml)，配置/账号资料留在 bots，不进入发布包 |
| 导出产物 | packages/platform、packages/linux、packages/android |

`platform/Dockerfile.*-update` 是历史专项覆盖入口，不带默认基底。使用前须显式传入已核对 image ID 或 digest 的 `BASE_IMAGE`，逐项确认基底的数据库 revision、Python 依赖和被覆盖模块的调用合同与本次源码相容；带 `COPY --from=backend/frontend` 的入口还须分别提供对应命名构建上下文。无法确认兼容集合时使用完整平台 Dockerfile。专项入口不改变 Compose 项目、容器、网络或卷名。

平台应用包含静态前端、API 和六个 worker：events、media、scheduler、notifications、terminal-monitor、release-verifier。Supervisor 七进程与 API 依赖均正常才健康；停止总预算 390 秒，迁移不由常驻 worker 重复运行。

Bot 容器控制代理由同一平台 Compose 项目管理，随 `Manage-Platform.ps1` 的 Status、Start、Stop 一起操作。它仍是独立容器：只有代理挂载 Docker socket；平台应用不挂载。代理仅接入内部控制网络，不发布宿主端口，目标固定为部署配置中的 QQ 和 Discord 容器。Socket 本身具有宿主 Docker 的广泛权限，因此变更代理配置和镜像前须单独核对。

新开发环境可以从部署目录使用以下入口，先明确独立项目、端口、网络、卷和环境文件：

```powershell
./scripts/platform.ps1 config
./scripts/platform.ps1 start
```

Linux 对应 `sh ./scripts/platform.sh config` / `start`。这些脚本可能创建 .env、构建镜像、初始化存储和迁移数据库；不用于只恢复现有平台。实际端口以 Compose 解析结果为准，API 与前端由统一应用同源提供。

常用命令有 start、stop、restart、status、logs、config、test、kernel-test；TLS 使用 config-tls、start-tls、stop-tls、restart-tls。PowerShell 另提供 execution-test 与 import-scripts，以脚本参数为准。

kernel-test 会创建并删除专用 al1s_stage2_test 数据库，检查迁移及数据库/S3 契约；执行前核对目标服务器与隔离资源。临时 smoke 覆盖层 `compose.smoke.yaml` 必须设置本次唯一的 `AL1S_SMOKE_ID`，并与基础 `compose.unified.yaml` 合并检查项目、容器、网络、卷名称；不得带现用平台覆盖层或现用数据执行测试。现用 external 卷虽带历史 smoke 字样，仍是有效业务数据。

## TLS 与存储

新环境证书初始化：

```powershell
./scripts/Initialize-Tls.ps1 -DnsName al1s.local
```

Linux 对应 `sh ./scripts/initialize-tls.sh al1s.local`。默认使用稳定 DNS/mDNS SAN；实际使用固定 IP 时才显式增加 IP SAN。既有平台不要用 -Force 覆盖现用 CA。

HTTPS 默认 8443、MQTT TLS 默认 8883；长期凭据不通过明文局域网。终端只安装公开 ca.crt，不复制 CA 私钥或平台 server.key。域名/端口变更须同步证书 SAN 和浏览器 Origin 白名单，不能忽略证书错误。

宿主机对象目录使用可选 compose.storage-local.yaml，设置 AL1S_STORAGE_DIRECTORY 并预建 master、volume、filer 目录与权限。该覆盖层不复制旧卷数据，不能直接切换目录冒充数据迁移；业务仍经 S3 访问。

## Linux 宿主管理

宿主管理独立于业务容器，终端镜像不挂 Docker socket。安装入口为 [install-host-manager.sh](linux/install-host-manager.sh)，具体安装位置、数据路径和证书见部署台账。

启用升级前核对 AL1S_DEPLOY_ROOT、AL1S_DEPLOY_*、AL1S_HOST_CONTAINER 及 systemd 写入白名单，再使用已安装入口：

```text
python -m terminal_deployer --prepare-host
```

该命令只准备维护目录和锁，不创建缺失的身份/队列，不启动服务。平台来源采用固定 HTTPS origin、独立管理凭据与可信 CA；命令不接收任意 Shell、URL 或文件路径。

## Windows TUN 辅助与浏览器检查

可选局域网发现辅助，在工作区根目录执行：

```powershell
./al1s-deployment/scripts/Install-TerminalLanDiscovery.ps1 -TerminalHostname RK3576-Tronlong.local -ContainerName al1s-platform-app
```

它在当前用户登录期间每分钟解析唯一 RFC1918 IPv4，只更新指定容器的专属 hosts 行；不改全局代理/hosts、HTTPS URL、CA 或令牌。失败移除专属映射；用户会话或 Docker 不可用时不保证刷新。容器改名/项目移动后重新安装，追加 -Uninstall 可卸载并移除映射。

该辅助仅解决指定容器到指定终端主机名的解析。同源编辑通道由平台显式配置允许的 WSS 上游与公开 CA，不以此推断文件直传或其他网络链路通过。

浏览器登录自检入口为 [check-browser-login.cjs](scripts/check-browser-login.cjs)，需要本机 Playwright，可用 AL1S_PLAYWRIGHT_MODULE 指定安装路径、NODE_EXTRA_CA_CERTS 指向公开 CA。验证结果写测试计划，不在本页追加。
