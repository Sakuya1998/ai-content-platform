# ADR-003：同主域 Cookie 会话优先，反向代理作为单 origin 备选

## 状态

Accepted

## 决策

生产优先使用 \`app.example.com\` + \`api.example.com\`，使用 Secure、HttpOnly、SameSite Cookie 和 CSRF 校验；若部署环境不支持稳定跨域，则由 Nginx/网关提供单一 origin，并将 \`/api/v1\` 代理到后端。

## 原因

- 会话令牌不进入 localStorage，降低 XSS 后的长期窃取风险。
- 独立前端仍可以独立发布，同时保留清晰的 API 域名。
- 单 origin 方案可作为云服务器部署的低复杂度回退。

## 必须验证

- CORS 只允许明确的生产前端 origin，不使用 \`*\`。
- Cookie 包含 \`Secure\`、\`HttpOnly\`、正确的 \`SameSite\` 和必要的 Domain/Path。
- 所有 Cookie 写操作验证 CSRF token。
- 可信代理头、HTTPS 终止和健康检查在部署 smoke test 中验证。

## 被拒绝方案

- 将 access token 存入 localStorage：不符合生产安全要求。
- 允许任意 origin：会扩大会话和 CSRF 风险。
- 先做第三方身份平台强绑定：会阻塞首期自托管部署，后续可增加 OIDC 适配层。
