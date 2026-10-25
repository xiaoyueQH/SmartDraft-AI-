# 账号、双令牌与多标签页

已实现：JWT access/refresh、登录续期、退出、改密、管理员开通账号与列表、角色校验、标签页会话隔离及已有登录选择。

## 生命周期

- Access token 默认 10 分钟，固定 HS256，校验签名、issuer、独立 audience、用途、sub/sid/jti/iat/exp。角色不从客户端或 JWT 角色字段读取，每次访问查询账号及登录会话，退出/停用后旧 access 也失效。
- Refresh token 是不同用途与 audience 的 JWT，仅放 HttpOnly / SameSite Strict Cookie，路径 `/api/auth`。数据库保存 jti 的 SHA-256 摘要，不保存原始令牌。
- 每次刷新原子轮换摘要和 Cookie。旧 refresh 在有效期内再次使用会撤销对应登录会话；其他登录会话不受影响。严格轮换下，服务端已提交但刷新响应丢失时可能需要重新登录，不盲目重放请求。
- 默认闲置 7 天失效；持续访问可滑动续期，但单次登录最多 30 天，不能无限延期。用户关闭浏览器后，Cookie 在有效期内仍保留；打开新标签后可以选择继续已保存的登录。
- 修改密码重新核验原密码及当前会话，更新密码与撤销该账号全部登录会话在同一事务完成。其他账号不受影响。

## 同一浏览器多个账号

每次密码登录分配独立 `sid`。Cookie 名为 `sf_refresh_<sid>`，不是所有账号共用一个 refresh Cookie。当前标签页的 access 仅在内存，sessionStorage 只保存非秘密定位 ID；页面刷新通过该 ID 选取相应的 HttpOnly Cookie 恢复。

新标签或重新打开浏览器没有定位 ID 时，登录页通过 `POST /api/auth/sessions` 查询该浏览器中有效的已保存登录，展示账号选择。接口必须通过 Origin 检查，并验证每个 Cookie 的签名、用途、sid 和数据库当前摘要，只返回公开账号信息与定位 ID，不返回 access/refresh。点击后仍通过 refresh 校验与轮换。

选择已有登录会话不自动退出其他会话；密码登录时若当前标签已有选中的登录，则撤销被替换的会话并删除它自己的 Cookie。复制标签页可能复制定位 ID，此时两个标签使用同一登录会话：Web Locks 按 sid 串行刷新，退出会通知同一 sid 的其他标签。不同 sid 的标签不会被该退出通知清除。

账号变化时取消旧请求、使旧响应失效并重建受保护的业务页面组件。登录等公开页面保持稳定，避免清理旧身份时触发登录页重新挂载并中断正在进行的登录。原账号操作不得在等待续期后改用新账号凭据提交。订单/草稿等后续缓存必须按账号/会话隔离。HTTP 请求只携带该标签的 Bearer；服务器不从浏览器其他 Cookie 猜测业务账号。

这是业务上下文隔离，不是浏览器安全沙箱。共享同一浏览器配置的人可以访问其中保持登录的账号；不互信的人员应使用不同浏览器配置或退出账号，不能依赖标签页作为安全边界。

## 并发及来源保护

- refresh 及改密按账号 → 登录会话的固定顺序获取 PostgreSQL 行锁，刷新重放撤销在事务提交后返回 401。
- 前端单标签刷新合并，同 sid 跨标签用 Web Locks。目标为支持该能力的现代浏览器；部署使用 HTTPS，本地 `127.0.0.1` / `localhost` 也可提供安全上下文。缺少能力时明确提示，不能发送可能误触发重放保护的并发刷新。
- 登录、刷新、退出及已有登录查询必须携带允许的 Origin；默认同源开发代理，不开放任意 CORS。Bearer 操作若带 Origin 也会检查。
- 登录预算保存在 PostgreSQL，多个 API 进程共享；默认每账号 20 次/分钟、每客户端 200 次/分钟。不同账号不会共用同一个 20 次预算。记录仅保存摘要键，十分钟前的过期桶自动清理。
- 客户端地址来自受控代理链。启动器默认只信任 `127.0.0.1` 的转发头；部署代理需覆盖传入的伪造头，并配置 `SF_APP__TRUSTED_PROXY_IPS` 为实际可信代理，不能任意信任公网转发头。
- Argon2 密码计算在线程中执行，默认 4 个名额；请求取消后，正在进行的计算结束才释放名额。校验密码和签发令牌期间不持有 SQL 会话。

## 初始化及配置

```powershell
conda activate D:\code\env\supportflow
python scripts/migrate.py
python scripts/create_user.py admin --role admin --display-name 管理员
python scripts/start_api.py
```

密码通过隐藏交互输入，不提供固定默认密码或公开注册。其他账号由管理员页面开通，也可使用同一 CLI。staff是项目成员角色，当前与客户一样仅能访问本人记录；跨客户评审与管理仅允许admin。

随机签名密钥默认原子创建并持久化于 Git 忽略的 `storage/auth/jwt.key`。生产可通过 `SF_AUTH__SIGNING_KEY` 注入至少 32 字节密钥，多实例必须共享密钥及数据库。HTTPS 部署开启 `SF_AUTH__COOKIE_SECURE=true` 并配置 `SF_AUTH__ALLOWED_ORIGINS` 为精确来源 JSON 数组。

## 接口

| 接口 | 凭据与用途 |
| --- | --- |
| `POST /api/auth/login` | 账号密码、可信 Origin；可携带待替换的 X-Session-ID |
| `POST /api/auth/refresh` | 可信 Origin、X-Session-ID 及该 sid 的 HttpOnly Cookie |
| `POST /api/auth/sessions` | 可信 Origin 及已有 Cookie，查询可恢复登录 |
| `POST /api/auth/logout` | 可信 Origin、X-Session-ID 及对应 Cookie/Bearer，退出该登录 |
| `GET /api/auth/me` | Bearer，返回当前账号 |
| `POST /api/auth/password` | Bearer，当前密码及新密码，撤销此账号全部登录 |
| `GET /api/admin/users` | admin Bearer，分页读取账号 |
| `POST /api/admin/users` | admin Bearer，开通账号 |

接口不把 token 放 URL；响应标记 no-store。错误不回显密码、Cookie、令牌或连接信息。模拟账号仅用于测试，验收结束清理，不作为交付账号。

设计参考：[RFC 9700 的刷新轮换与重放检测](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14)、[OWASP 浏览器存储建议](https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html#local-storage)。本项目是自有账号 JWT 鉴权，不是完整 OAuth 授权服务器。
