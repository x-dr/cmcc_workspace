# Docker 部署

使用 [docker-compose.yaml](docker-compose.yaml) 直接拉取 GitHub Actions 发布到 GHCR 的镜像 `ghcr.io/x-dr/cmcc_workspace:latest`。部署服务器不需要源码、Dockerfile、Python 或 Node.js，也不进行本地构建。

镜像同一进程运行网页服务、凭据维护和各模块定时任务，包含 Python 3.13、Node.js 24、Canvas 依赖及固定协议资源，不安装浏览器。业务限制和人工验证要求沿用现有实现。

## 部署准备

- 安装 Docker Engine 和支持 `up --wait` 的 Docker Compose v2 或更高版本，使用 `docker compose version` 检查。
- 镜像标签须已由 GitHub Actions 成功发布；工作流配置目标为 `linux/amd64`、`linux/arm64`，不支持 32 位 ARM。
- 默认使用 Docker 管理的数据卷，无需预建宿主机目录、证书或密钥文件；首次启动自动初始化数据和加密密钥。
- 公共镜像可直接拉取；若镜像为私有，先按 [GitHub Container Registry 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic)，使用具有 `read:packages` 权限且可读取该包的凭据执行 `docker login ghcr.io`。不要将令牌写进 Compose 文件或提交到仓库。

新部署可创建独立目录，再将仓库中的 **docker-compose.yaml** 下载或复制到其中：

```bash
mkdir -p ~/lingxi-workspace
cd ~/lingxi-workspace
# 将 docker-compose.yaml 保存到当前目录后继续。
```

只需要此配置文件即可部署，可选配置保存在同目录的 `.env`。已有 Compose 部署应继续使用原目录及项目名，避免因数据卷名称变化而启用空账号库。

## 拉取与启动

以下命令均在 `docker-compose.yaml` 所在目录执行：

```bash
docker compose -f docker-compose.yaml config --quiet
docker compose -f docker-compose.yaml pull
docker compose -f docker-compose.yaml up -d --no-build --pull never --wait
docker compose -f docker-compose.yaml ps
docker compose -f docker-compose.yaml logs --tail=100 lingxi
```

先拉取成功，再用已下载的镜像启动；`--no-build --pull never` 不进行本地构建或二次拉取。启动会等待镜像内置的健康检查通过；`ps` 应显示服务运行且为 `healthy`。如果拉取失败，先解决镜像标签、权限或网络问题，不需要本地构建。

默认仅发布宿主机 `127.0.0.1:5500`，访问 `http://127.0.0.1:5500/`。Docker 转发后的请求可能被识别为远程访问，首次进入时按页面提示设置访问密码。

配置中没有 `build`，普通 `up` 按 `pull_policy: always` 从 GHCR 拉取。仓库原有的 `compose.yaml` 保留为兼容入口，复用同一服务配置；文档统一显式指定 `-f docker-compose.yaml`。

## 端口、服务器访问与镜像版本

需要通过服务器 IP 直接访问时，在部署目录新建 `.env`：

```dotenv
LINGXI_BIND_ADDRESS=0.0.0.0
LINGXI_PORT=5500
LINGXI_IMAGE=ghcr.io/x-dr/cmcc_workspace:latest
```

保存后执行：

```bash
docker compose -f docker-compose.yaml config --quiet
docker compose -f docker-compose.yaml pull
docker compose -f docker-compose.yaml up -d --no-build --pull never --wait
```

随后访问 `http://服务器IP:5500/`，并按实际访问范围放行防火墙和云安全组。可保持默认本机绑定，由宿主机反向代理提供 HTTPS。首次远程访问需设置至少 8 位的访问密码。

`LINGXI_PORT` 只修改宿主机端口，例如设为 `8080` 后访问 `http://服务器IP:8080/`；容器内固定监听 `0.0.0.0:5500`。平台管理不能覆盖容器监听环境变量，网卡列表显示的是容器网络，实际访问地址以宿主机映射为准。

需要固定版本时，将 `LINGXI_IMAGE` 改成已发布的版本标签、`sha-<完整提交号>` 标签或镜像摘要，再执行同样的拉取与启动命令。工作流只有在标签填写 `latest` 时才更新 `:latest`；程序 Release 与镜像发布相互独立，不能假定所有 Release 都有对应镜像标签。

## 持久化与备份

容器按 UID/GID `10001:10001` 运行，Compose 管理的 `lingxi-data` 数据卷挂载到 `/data`，实际卷名默认带部署目录的项目名前缀。`/data` 包含 `accounts.sqlite3`、`credentials.key`、`recharge/` 和其他业务数据。重启、更新及重建容器复用此数据卷和仍有效的业务凭据；同一数据目录只运行一个实例，避免重复调度。

备份时先停服，再完整复制数据目录，包括原加密密钥、任务记录和存在的 SQLite WAL／SHM 文件：

```bash
docker compose -f docker-compose.yaml stop lingxi
backup_dir="./backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup_dir"
docker compose -f docker-compose.yaml cp lingxi:/data/. "$backup_dir/"
docker compose -f docker-compose.yaml start lingxi
```

备份成功后再升级。数据库和密钥必须配套保存，丢失密钥将无法解密原凭据；不要生成替代密钥覆盖已有数据。

## 更新与日常管理

保持原部署目录、项目名和数据挂载不变，先按上一节备份，再更新镜像：

```bash
docker compose -f docker-compose.yaml pull
docker compose -f docker-compose.yaml up -d --no-build --pull never --wait
docker compose -f docker-compose.yaml ps
docker compose -f docker-compose.yaml logs --tail=100 lingxi
```

停止与重新启动：

```bash
docker compose -f docker-compose.yaml stop lingxi
docker compose -f docker-compose.yaml start lingxi
```

日常升级不要使用 `docker compose down -v`，它会删除数据卷。定时任务继续按北京时间执行，停服期间的补做和有效凭据复用遵守各模块原有规则。

## 从已有 Linux 源码服务迁移

先停止原服务，在部署目录创建专用目录，例如 `./docker-data`。将原 `.local/` 的**完整内容**复制到其中，再将原 `recharge/.local/` 的完整内容复制到 `./docker-data/recharge/`。若原服务已使用 `LINGXI_DATA_DIR`，直接完整复制该目录即可。

保留原 `credentials.key`，将专用数据目录及内容属主设为 `10001:10001`，目录权限设为 `0700`、密钥权限设为 `0600`。例如在已复制数据后执行：

```bash
sudo chown -R 10001:10001 ./docker-data
sudo find ./docker-data -type d -exec chmod 700 {} +
sudo chmod 600 ./docker-data/credentials.key
```

将 `docker-compose.yaml` 中服务的挂载改为以下内容，顶层未使用的 `volumes` 声明可保留：

```yaml
    volumes:
      - ./docker-data:/data
```

目录须事先存在并可由容器用户写入。迁移完成后按前文启动，核对账号、任务和有效凭据；不要同时运行原服务，也不要混入另一套账号库。Windows DPAPI 和安卓系统密钥不能在 Linux 容器直接解密，应保留原平台数据，在新容器中重新登录。

## 镜像发布维护

部署者只需拉取已经发布的镜像。维护者需要发布新镜像时，在仓库 **Actions → Docker image → Run workflow** 手动执行现有工作流；普通提交不触发镜像发布，也不创建 Release 或打包桌面／安卓客户端。

工作流固定推送到 `ghcr.io/x-dr/cmcc_workspace`，使用 `GITHUB_TOKEN` 和 `packages: write`，运行仓库须具有该包的写入权限。填写标签，默认 `latest`，同时生成 `sha-<完整提交号>` 标签。首次发布后需要匿名拉取时，将 GitHub Package 可见性设为 Public。

发布前工作流在 amd64 上使用临时数据卷和禁用外网的容器验证非 root 启动、页面、访问保护、Node Canvas，以及重建后的密钥、合成凭据和任务保留；通过后再构建并推送双架构镜像。arm64 仅构建，未在此工作流进行真实设备业务验收。配置校验通过不代表远端标签已发布，实际部署须以拉取成功及健康检查结果为准。

Compose 的配置复用和启动参数可参考 Docker 官方的 [extends 说明](https://docs.docker.com/compose/how-tos/multiple-compose-files/extends/) 和 [compose up 参数](https://docs.docker.com/reference/cli/docker/compose/up/)。
