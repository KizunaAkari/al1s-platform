# AL-1S Platform Backend

全新平台后端项目，包含平台内核、执行、Maa内容、通知等模块，不包含旧平台业务兼容层。
完整源码与统一部署入口见[平台 README](../../README.md)。本 README 只说明本地检查和构建，不维护现场版本。

## 本地检查

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m mypy al1s
```

`requirements.lock` 和 `requirements-dev.lock` 是容器构建使用的已解析版本；
`pyproject.toml` 保留依赖意图。升级依赖时两者必须一起评审和验证。

生产运行由 `al1s-deployment/` 统一编排。数据库结构只允许通过 Alembic 迁移。

## 容器构建

从本目录执行，只构建后端 API 镜像：

```sh
docker build --target runtime -t al1s-backend:local .
```

该镜像监听 `8000` 并运行 Uvicorn；需另外配置 PostgreSQL、S3 和 MQTT，并先执行 Alembic 迁移。`.env.example` 仅作配置参考，真实秘密放本机环境文件或部署秘密存储。

完整平台镜像包含前端和受 Supervisor 管理的平台 worker，从平台仓库根目录执行：

```sh
docker build -f al1s-deployment/platform/Dockerfile -t al1s-platform:local .
```
