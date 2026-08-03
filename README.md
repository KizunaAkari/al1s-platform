# AL-1S Platform

AL-1S 脚本站的平台控制中心：FastAPI 后端、SQLite 数据卷、任务队列、失败证据、通知设置、脚本库和终端一键部署缓存。

前端源码单独维护在 [al1s-frontend](https://github.com/KizunaAkari/al1s-frontend)。本仓库用 Git submodule 引入前端，因此首次克隆请使用：

```powershell
git clone --recurse-submodules https://github.com/KizunaAkari/al1s-platform.git
```

如果已经克隆仓库：

```powershell
git submodule update --init --recursive
```

## 一键启动

Windows：

```powershell
Copy-Item .env.example .env
.\platform.ps1 start
```

Linux：

```bash
cp .env.example .env
bash platform.sh start
```

平台地址默认为 <http://127.0.0.1:8000>。数据保存在 Docker volume `maa-platform-data` 中，`backup`、`restore`、`export` 和 `import` 由平台脚本管理。

## 终端部署

当前仅支持带 NPU 的创龙 RK3576 ARM64 终端。终端镜像缓存可以在平台页面上传，平台会保存镜像并提供局域网下载地址，减少终端访问公网。

终端 Agent 和 NPU Compose 文件位于 [al1s-terminal](https://github.com/KizunaAkari/al1s-terminal)。
