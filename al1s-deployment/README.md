# AL-1S 部署

本项目负责 Compose、TLS、部署脚本和发布包。公开克隆入口见[平台 README](../README.md)；现场运行版本、数据身份和恢复条件在本机私有部署台账维护，不随源码发布。

已有平台的项目名为 `al1s-platform`，应用容器为 `al1s-platform-app`；Bot 项目为 `al1s-bot`，容器为 `al1s-bot-qq` 和 `al1s-bot-discord`；Linux 容器为 `al1s-terminal-linux`。升级和重建沿用既定名称，核对完整 Compose 覆盖结果和命令行项目名，避免基础文件或工作目录默认值改变现用环境。

AI执行部署前遵守[容器复用与宿主进程确认规则](../AGENTS.md#reuse-containers-and-confirm-host-processes)：现有容器适合承载时必须复用；新增宿主服务、后台/监听进程或启动项须先说明并由YY明确确认。文档中的安装命令本身不是新增进程的授权。

## 新环境配置与构建

先按照[平台 README](../README.md#克隆完整源码)递归克隆三个源码仓库。下面的镜像构建命令从工作区根目录执行，不要求已经存在本机发布包：

```sh
docker build -f al1s-deployment/platform/Dockerfile -t al1s-platform:local .
docker build -f backend/al1s-bot/Dockerfile -t al1s-plachta:local backend
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

基础 TLS Compose 不会自动把 `.env` 中的浏览器 Origin 传给后端。新环境要启动浏览器管理界面时，另建本机文件 `al1s-deployment/compose/compose.browser-origins.yaml`，为后端显式配置允许的来源；按实际域名和端口修改，不使用通配来源：

```yaml
services:
  backend:
    environment:
      AL1S_CORS_ORIGINS: "https://al1s.local:8443,https://localhost:8443,https://127.0.0.1:8443"
```

这沿用既有管理员 Origin/CSRF 校验，不信任任意转发来源；已有平台的 `compose.platform.yaml` 已负责同一项配置，不用此示例覆盖现有部署。独立新环境可以用这组配置先检查，再构建所需镜像：

```sh
docker compose -p al1s-platform --env-file al1s-deployment/.env --env-file al1s-deployment/.env.tls -f al1s-deployment/compose/compose.unified.yaml -f al1s-deployment/compose/compose.tls.yaml -f al1s-deployment/compose/compose.browser-origins.yaml config --quiet
docker compose -p al1s-platform --env-file al1s-deployment/.env --env-file al1s-deployment/.env.tls -f al1s-deployment/compose/compose.unified.yaml -f al1s-deployment/compose/compose.tls.yaml -f al1s-deployment/compose/compose.browser-origins.yaml build backend migrate s3-init
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

## 当前单机合并布局

平台常驻7个服务：backend、tls-gateway、postgres、storage、mosquitto、ops、log-collector；Bot保留llbot和discord-worker两个服务键，显示容器名为al1s-bot-qq/al1s-bot-discord。旧内部DNS别名仍兼容，不重写现有连接、对象键、队列或账号身份。

私有`.env.consolidation`登记组合镜像和布局，Manage-Platform自动加载compose.ops.yaml与compose.storage.yaml；常规Start不启动legacy profile或初始化服务。首次迁移/初始化使用完整配置的显式工具入口`run --rm --no-deps migrate` / `s3-init`，不要以整体up制造退出对象。旧profile只用于明确恢复，不与新存储同时运行。

存储镜像用storage/Dockerfile和明确的PLATFORM_IMAGE、SEAWEED_IMAGE构建；原四份卷分别挂至master/volume/filer/s3目录，镜像带布局标记并逐角色健康检查。ops/Dockerfile以已核对维护镜像构建，两个控制进程保留独立端口/Token；它保持平台停服执行和状态查询，日志独立。布局、备份和固定写入者停写/恢复一起校验；原始weed镜像不能直接替换组合存储。

## 组件版本与升级

管理员 → 组件管理统一展示运行版本、版本库及升级记录。MaaFramework随Linux终端镜像升级；PostgreSQL、SeaweedFS四服务和MQTT由独立维护执行器管理。上传/校验不安装，必须选择已发布包、通过预检并确认升级。普通升级只接受已验证的兼容数据格式；未经验证的跨主版本迁移拒绝执行。

初始化后，`Manage-Platform.ps1`自动加载已登记的`component-images.env`、私有`.env.component-host`及`compose.components.yaml`，日常Start不会覆盖所选版本。维护模块属于后端工程，以`ops`服务和`al1s-platform-ops`容器纳入同一平台Compose，统一启停、健康和日志，不发布宿主端口，不注册Windows登录任务。仅维护容器承载维护Docker权限，API和Bot控制代理权限不扩大。私有配置、证书、操作日志和恢复点保留在登记部署目录，不得作为临时制品清理或进入Git。

维护镜像用工作区根目录作为构建上下文：`docker build -f al1s-deployment/component-maintenance/Dockerfile --build-arg PLATFORM_IMAGE=<已核对的兼容平台镜像> -t al1s-component-maintenance:local .`。Docker CLI来源固定摘要；离线构建可用`DOCKER_CLI_IMAGE`指定已核对的本机CLI镜像。登记入口`component-maintenance/register.py`接受现有私有`host.json`、明确的`--engine-root`、`--platform-url`及`--image`，沿用原凭据/目录和现有CA生成内部服务证书。平台URL须是该网关证书覆盖的DNS地址；网关内部别名使用同一主机名。`--engine-root`必须是在Docker引擎和维护容器内指向同一物理目录的Linux绝对路径，Windows/Docker Desktop先以无网络子容器验证绑定源，不能把Windows路径直接当Linux挂载源。填写值使用私有`.env.component-host`，公开字段示例见`.env.component-host.example`。

正常维护容器随`Manage-Platform.ps1`的Start/Stop操作，未结算维护操作或无法核验状态时拒绝整体停止。`Stop-ComponentMaintenance.ps1 -ConfigPath <旧登记配置> -RemoveLogonTask`仅供旧Windows部署转换时精确移除裸进程/任务；旧Install入口已拒绝安装。结果未知时先在升级记录里检查实际部署并恢复，不重放操作、不自动清锁，也不在数据格式变化后盲目降级。平台/数据库停服期间的状态代理只接受指定操作的短期只读能力，没有浏览器Shell或Docker入口。

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

Bot 容器控制代理由同一平台 Compose 项目管理，随 `Manage-Platform.ps1` 的 Status、Start、Stop 一起操作。它是ops内的独立鉴权进程，与维护执行器共同受容器管理；只有ops承载Docker管理权限，平台应用不挂载。代理仅接入内部控制网络，不发布宿主端口，目标固定为部署配置中的 QQ 和 Discord 容器。Socket 本身具有宿主 Docker 的广泛权限，因此变更代理配置和镜像前须单独核对。

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

已登记的Linux宿主管理独立于业务容器，终端镜像不挂Docker socket；新增实例或宿主启动项须按AGENTS取得明确确认。安装入口为 [install-host-manager.sh](linux/install-host-manager.sh)，具体安装位置、数据路径和证书见部署台账。

启用升级前核对 AL1S_DEPLOY_ROOT、AL1S_DEPLOY_*、AL1S_HOST_CONTAINER 及 systemd 写入白名单，并在当前 Shell 中导出这些已核对的变量。安装器将模块安装到 `/opt/al1s-host-manager/venv`，手工命令不会自动读取 systemd 的 EnvironmentFile；使用该虚拟环境和对应权限执行：

```sh
sudo --preserve-env=AL1S_DEPLOY_ROOT,AL1S_DEPLOY_COMPOSE,AL1S_DEPLOY_ENV_FILE,AL1S_DEPLOY_DATA_DIR,AL1S_DEPLOY_ADB_HOME,AL1S_DEPLOY_MODEL_DIR,AL1S_DEPLOY_CERT_DIR,AL1S_HOST_CONTAINER /opt/al1s-host-manager/venv/bin/python -m terminal_deployer --prepare-host
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

### 表情包工坊兼容部署

LLBot网络出口由`AL1S_QQ_HTTP_PROXY`单独控制，默认留空直连；Discord及其表情后端继续使用`AL1S_BOT_HTTP_PROXY`。调整QQ出口使用QQ当前登记的私有环境文件及完整Compose层，只重建`llbot`服务，不重建正常运行的`discord-worker`。不关闭TLS验证；环境代理留空不等于绕过宿主机的VPN/TUN路由。

表情功能后端与Node Worker同处`al1s-bot-discord`，QQ网关为`al1s-bot-qq`（旧名称保留为内部DNS别名）；前端/鉴权转发随平台发布，不增加功能容器。Bot镜像使用`backend/`构建上下文；共用既有控制网络，不向宿主发布3101端口。内置的Python后端使用Bot环境中的`AL1S_MEME_*`数据/S3/解密能力和专用控制Token；平台仅需受控后端URL及同一控制Token。示例不提供真实秘密，实际权限/环境在授权发布时核对。

0054→0055、平台新Blob引用GC/鉴权转发/前端、Bot的Python/Node组成同一兼容单元；先在无受影响写入时按原流程保护数据并迁移，再启动同版本写入者。不得让旧GC与新素材写入者并行，不回退或删除已有模板数据。QQ事件WS和启用范围由管理员明确配置；账号资料、Worker队列/身份、固定项目/容器/卷/网络保持。只构建镜像不等于部署或真实账号送达通过。

网络不可用时，平台候选可通过`platform/Dockerfile.meme-workshop-update`复用已审阅的0054运行依赖，具名构建上下文`backend=backend/al1s-backend`、`frontend=al1s-frontend/dist`。先完成当前源码测试和前端构建，显式提供`BASE_IMAGE`；该方式复制一致的完整平台源码及迁移快照，避免新API工厂与旧基础模块混装。它不是可任意套在其他历史版本上的局部补丁，也不改变容器启动程序或执行迁移。
