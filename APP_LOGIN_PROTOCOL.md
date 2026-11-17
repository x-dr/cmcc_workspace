# APP 短信登录与人工滑块

## 协议依据

2026-11-17 从[中国移动官网](https://www.10086.cn/cmccclient/)的移动下载入口，沿公开下载脚本定位并下载 [12.5.6 官方安装包](https://res.app.coc.10086.cn/downfile/apk/ChinaMobile10086.apk)。本次包的 SHA256 为 `db0de4c88154e3b208da9535004a5c304bb8f5c62fbfaa4b06d8d730a4234d4f`；固定下载地址今后可能更新文件。

静态读取登录模块的方法引用、常量和可解析指令，未连接手机、未使用 Frida、未发送短信或真实登录请求：

- `com.cmccit.loginmodule.helper.manager.LoginUtil.isMiddleRiskCode`：返回码 `214100` 对应中风险滑块。
- `LoginSlideBlockDialog.Companion.getAppId`：`ENV == 0` 使用 `02aecbb71abe2113e42a602056797e09`，其他环境使用 `f8e9e86637106b3ee5847bc6a4d2265f`。本地采用前者，未借用卡券活动的 appId。
- `LoginSlideBlockDialog`：官方验证域为 `https://yxfk.market.chinamobile.com:8081`，组件入口为 `/mgt/decision-captcha/zw-captcha/index.js`；成功事件读取结果中的 `token`。
- `SMSLoginClient$request$1$onHttpSuccess$2.onSuccess`：调用原请求对象的 `setRiskToken(token)`，再由 `SMSLoginClient.request` 发送原短信登录请求。
- `BaseLoginClient.tryToShowSlideBlockDialog`：原请求已携带 `riskToken` 时不再继续弹出滑块；本地同样不循环重放验证后的登录。

由此确认：滑块结果属于 `reqBody.riskToken`，不能填入设备标识字段 `devToken`。短信登录路径仍为 `/biz-orange/LN/uamsmscode/login`，沿用现有 Python 加密与 HTTP 传输。

## 本地处理

1. 首次登录收到 `214100` 时，Python 暂存在内存中的原请求（包含本次验证码及已加密的号码），生成绑定当前账号、设备会话和验证码批次的随机挑战 ID。此时没有加入账号池。
2. 本地账号管理页或账号管理弹窗显示官方人工验证组件。组件资源和人工操作请求经固定目标的 `/login-verification/` 转发，不向官方转发本地账号 Cookie 或 Authorization，也不开放任意目标地址。
3. 前端只接收当前 iframe、相同来源、当前挑战 ID 的成功消息；Python 再核对账号、有效期及挑战 ID。验证通过后给原请求增加 `riskToken`，仅继续一次登录，不重发短信、不重新初始化。
4. 短信过期、主动重发、切换号码或取消验证会丢弃页面旧回调。取消后可继续原批次验证；验证码或挑战不写入数据库、日志、账号导出数据。
5. 验证后的请求超时、再次要求滑块、缺少可核对的 APP Cookie 时停止继续提交，显示失败或结果未确认。只有官方登录成功并取得 APP 会话后才完成账号保存。若 APP 已登录、灵犀换票暂时失败，用户可点击“继续完成登录”复用原 Cookie。

关闭“我的卡券”功能不影响登录滑块。登录验证入口只提供公开验证资源，不开放卡券钱包接口；组件加载或人工验证失败后由用户手动重新加载，不自动循环请求。

## 验证边界

本地协议、路由和界面测试覆盖：原请求与 `riskToken` 的续接、账号与验证码批次隔离、过期与重发、取消与继续、伪造及迟到回调、提交结果不明时停止、灵犀换票失败后复用 APP Cookie，以及卡券开关关闭时登录验证的可用性。

浏览器验收使用隔离内存服务和模拟接口，检查桌面及窄屏布局、取消与继续、人工回调后仅续接一次。相关截图中的验证组件明确标为测试占位。

**尚未完成真人在本地页面操作官方滑块并成功取得 APP Cookie 的实测。** 静态协议及模拟回归不证明官方服务已接受网页组件产生的登录 token，也不证明所有账号的风控状态都能解除；需由用户在实际触发 `214100` 的号码上手动核对。未绕过官方校验或伪造成功结果。
