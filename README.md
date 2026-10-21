# fd-zhesi

实论（zhesi）独立产品线门面单仓：web 三入口（`/` 官网 landing、`/workbench` 工作台、`/admin` 管理后台）+ FastAPI 服务端（个人层：立场卡/张力簿/思想年轮的自有存储）。

依据 `openspec/changes/zhesi-worldview-workshop`（task 6.1，骨架）。技术栈照 fd 舰队配方：React 19 + Vite + TS + Tailwind v4 + i18next 双语；服务端 Python FastAPI；部署 cheap fd-prod + TCR + ArgoCD。

参照系检索本体在独立仓 `fd-zhesi-reference`（MCP 服务），本仓不承载参照系实现——照 fd-lex 与 fd-legal-search-mcp 的分仓先例。

## 骨架期口径

- web 三入口均为真实可渲染占位页（task 6.2 落三闭环界面）
- server 提供 `/healthz`、`/api/version`；个人层 API 挂载点 task 3.x
- 隐私立场：个人层是用户信念记录，只落本仓自有存储，不进共享底座（spec「个人层私有归属」）

## 本地开发

web：`cd web && npm install && npm run dev`
server：`cd server && pip install -e ".[dev]" && pytest && uvicorn fd__api.main:app --port 8010`
