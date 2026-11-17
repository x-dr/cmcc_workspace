# Docker 部署

镜像沿用 `python -X utf8 app.py`，同一进程启动网页服务、凭据维护和各模块定时任务。使用 Python 3.13，包含现有会员风控组件需要的 Node.js 24、Canvas 依赖及固定协议资源，不安装浏览器。业务限制和人工验证要求沿用现有实现，容器打包不代表尚未接通的业务步骤已完成。

## 用 GitHub Actions 构建镜像

将本次文件同步到 GitHub 默认分支后，在仓库 **Actions → Docker image → Run workflow** 手动执行。普通提交不触发镜像发布，也不创建 Release 或打包桌面／安卓客户端。

镜像固定发布到 `ghcr.io/x-dr/cmcc_workspace`。工作流使用自带的 `GITHUB_TOKEN` 和 `packages: write` 权限，无需配置 Docker Hub 账号；执行工作流的仓库必须具有该 GHCR 包的写入权限。

运行时填写镜像标签，默认 `latest`；例如填写 `1.9.0` 就发布 `:1.9.0`，同时生成 `:sha-<完整提交号>`。只有填写 `latest` 时才更新 `:latest`，镜像标签不修改程序内置版本。支持 `linux/amd64` 和 `linux/arm64`。

发布前先在 amd64 上执行隔离冒烟检查：非 root 启动、网页及静态资源、访问密码保护、Node Canvas、重建容器后的密钥／加密凭据／任务数据保留。检查使用临时数据卷并禁用外网，不访问真实账号。通过后构建并推送双架构镜像；arm64 在此工作流中仅构建，未进行真实设备业务验收。

首次发布后，在 GitHub 的 Packages 中查看镜像。需要匿名拉取时，将 Package 的可见性设为 Public；私有镜像需先按 GitHub 文档使用具备 `read:packages` 权限的凭据登录 `ghcr.io`。

工作流依据 Docker 官方的 [多架构构建](https://docs.docker.com/build/ci/github-actions/multi-platform/) 和 [GHCR 登录方式](https://github.com/docker/login-action#github-container-registry) 配置。

## 拉取与启动

需要 Docker Engine 和 Docker Compose 插件。拉取默认 `latest` 标签：

```bash
docker pull ghcr.io/x-dr/cmcc_workspace
```

镜像须已发布成功。在保存 `compose.yaml` 的项目目录执行：

```bash
docker compose config --quiet
docker compose pull
docker compose up -d --no-build --wait
docker compose logs --tail=100 lingxi
```

默认仅发布宿主机 `127.0.0.1:5500`，访问 `http://127.0.0.1:5500/`。Docker 转发后的请求可能被识别为远程访问，首次进入时按页面提示设置访问密码。

需要通过服务器 IP 直接访问时，在该目录的 `.env` 中填写：

```dotenv
LINGXI_BIND_ADDRESS=0.0.0.0
LINGXI_PORT=5500
LINGXI_IMAGE=ghcr.io/x-dr/cmcc_workspace:latest
```

然后重新执行 `docker compose up -d --no-build --wait`。`LINGXI_PORT` 配置宿主机端口，容器内固定为 `5500`；容器中的 `LINGXI_HOST` 由环境固定，平台管理不能修改容器监听地址。网卡列表显示容器网络，实际访问地址以宿主机端口映射为准。需要公网访问时按实际范围设置防火墙；也可保持默认本机绑定，由宿主机反向代理提供 HTTPS。

容器按 UID/GID `10001:10001` 运行，默认使用 Compose 管理的 `lingxi-data` 数据卷，挂载到 `/data`。无需预建绑定目录、提供证书或另设加密密钥。新数据卷自动生成密钥；停机和容器重建保留原有数据。一个数据卷只运行一个服务实例，避免重复调度。

## 本地构建与验证

在源码项目目录执行：

```bash
docker build -t lingxi-workspace:local .
python3 build_tools/smoke_docker.py lingxi-workspace:local
LINGXI_IMAGE=lingxi-workspace:local docker compose up -d --no-build --pull never --wait
```

如果已有 `.env`，本次命令行变量优先；后续要一直使用本地镜像，应将 `.env` 中的 `LINGXI_IMAGE` 同样设为 `lingxi-workspace:local`。构建上下文只包含运行所需源码和公开资源，排除本机账号库、密钥、分析文件、缓存、虚拟环境和 `.env`。

## 数据迁移、备份与升级

`/data` 包含 `accounts.sqlite3`、`credentials.key`、`recharge/` 和其他业务数据。必须一起保存数据库、原密钥和任务记录，包括存在的 SQLite WAL／SHM 文件。Windows DPAPI 和安卓系统密钥不能在 Linux 容器直接解密，原平台数据应保留，容器使用新的数据目录登录。

迁入已有 Linux 源码数据时，先停止原服务，在宿主机创建专用目录，例如 `./docker-data`；将原 `.local/` 的**完整内容**放入其中，再将原 `recharge/.local/` 的完整内容放入 `./docker-data/recharge/`。若原服务已经使用 `LINGXI_DATA_DIR`，直接完整复制该目录即可。保留原 `credentials.key`，将目录属主设为 `10001:10001`，目录权限设为 `0700`、密钥权限设为 `0600`，然后将 Compose 的服务挂载改为：

```yaml
    volumes:
      - ./docker-data:/data
```

迁移完成后再启动容器，不同时运行原服务。绑定目录需要事先存在且容器用户有写权限；不要混入另一套账号库或生成替代密钥。

使用默认数据卷时，以下命令先停服，再将完整数据复制到新备份目录；在项目目录执行：

```bash
docker compose stop lingxi
backup_dir="./backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
docker compose cp lingxi:/data/. "$backup_dir/"
docker compose start lingxi
```

保存备份后升级：

```bash
docker compose pull
docker compose up -d --no-build --wait
docker compose ps
docker compose logs --tail=100 lingxi
```

切换版本时修改 `.env` 的 `LINGXI_IMAGE`，再执行同样命令。停止服务使用 `docker compose stop`；重建容器保留数据卷，日常升级不要使用 `docker compose down -v`。定时任务仍按北京时间执行，停服期间的补做和有效凭据复用继续遵守各模块原有规则。
