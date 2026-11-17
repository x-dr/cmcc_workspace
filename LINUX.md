# Linux 运行说明

Linux 使用 Python 本地网页服务入口 `app.py`，无需桌面环境。后台任务由同一个 Python 进程执行，关闭页面后继续运行，停止服务后暂停。

也可使用 Docker／Compose 运行，镜像构建、数据迁移和 GitHub Actions 操作见 [Docker 部署说明](DOCKER.md)。

## 安装与启动

使用 Python 3.12 或更高版本。以 Debian／Ubuntu 为例，先安装虚拟环境支持：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
```

在项目目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./start.sh
```

启动脚本自动切换到项目目录，优先使用 `.venv/bin/python`，否则使用 `python3`。可通过 `LINGXI_PYTHON=/path/to/python ./start.sh` 指定解释器。也可以直接运行 `.venv/bin/python -X utf8 app.py`。

默认端口为 `5500`，监听 IPv4 和系统支持的 IPv6；系统禁用 IPv6 时继续使用 IPv4。浏览器访问 `http://127.0.0.1:5500/`。从其他设备访问使用服务器地址，首次远程访问需按页面提示设置访问密码。

只监听本机：

```bash
LINGXI_HOST=127.0.0.1 ./start.sh
```

只监听 IPv4 并指定端口：

```bash
LINGXI_HOST=0.0.0.0 LINGXI_PORT=5500 ./start.sh
```

未设置监听环境变量时，也可在「平台管理 → 网络监听」中修改端口、IPv4／IPv6 和指定网卡地址。Linux 直接读取系统网卡列表，支持按网卡跟随地址变化，不依赖主机名解析。服务器防火墙或云安全组需按实际访问范围放行所用端口。

现有会员福袋风控运行组件额外依赖 Node.js 和 `recharge/package.json` 中的依赖；使用该功能时安装 Node.js 22+，在项目目录执行 `npm ci --prefix recharge`。普通网页服务和 Python 协议功能不要求安装浏览器。官方要求环境采集或人工验证而对应 Python 分支尚未接通的操作仍会明确报错；Linux 适配不代表这些业务步骤已完成。

## 数据与凭据

源码运行保留原有目录：账号、业务凭据及加密任务位于 `.local/accounts.sqlite3`，周三任务位于 `recharge/.local/`。如需集中保存数据，在首次启动前指定绝对路径：

```bash
LINGXI_DATA_DIR=/path/to/lingxi-data ./start.sh
```

此时账号数据库位于该目录，周三任务位于其 `recharge/` 子目录。后续启动使用相同目录和系统用户，避免意外创建另一套账号库。

Linux 使用 Fernet 认证加密保存原有加密字段，密钥为数据库旁的 `credentials.key`，首次创建权限为 `0600`，新数据目录为 `0700`。重启复用原密钥及仍有效的业务凭据，不因系统切换逻辑而重复登录。密钥丢失、损坏或与数据库不匹配时明确报错并保留数据，不生成替代密钥覆盖旧记录。

备份或迁移时先停止服务，再完整保存数据目录以及源码默认布局下的 `recharge/.local/`；数据库、密钥和任务记录必须配套保存，运行中的 SQLite WAL／SHM 也属于数据库状态。Linux 数据可连同原密钥迁移到另一台 Linux 设备，密钥权限应保持 `0600`。Windows 的 DPAPI 和安卓系统密钥绑定原系统身份，不能直接在 Linux 解密；应使用新的 Linux 数据目录重新登录，保留原平台数据。

定时任务继续按各模块的北京时间执行，不要求更改 Linux 系统时区。机器休眠、关机或服务退出期间无法发送定时请求；启动后的补做规则沿用各模块原有约定。

## 作为 systemd 服务运行

以下为手动安装示例，替换项目路径及系统用户名后保存为 `/etc/systemd/system/lingxi-workspace.service`。保持与手动运行相同的用户和数据目录。

```ini
[Unit]
Description=Lingxi Workspace
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/lingxi-workspace
ExecStart=/path/to/lingxi-workspace/start.sh
Environment=LINGXI_DATA_DIR=/path/to/lingxi-data
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

`LINGXI_DATA_DIR` 如需复用已有数据，应先在停服后将完整数据及密钥迁入该目录，再启用服务。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lingxi-workspace
sudo journalctl -u lingxi-workspace -f
```

此示例不会自动安装服务或修改本机防火墙。
