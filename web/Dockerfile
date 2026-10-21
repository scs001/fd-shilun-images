# fd-zhesi-web 前端镜像 — 多阶段：node 构建 → nginx 服静态面（/ /workbench /admin SPA）。
# 发布通道：GitHub Actions -> TCR（.github/workflows/image.yml，配方 docs/IMAGE-RELEASE.md）。
# 硬性要求（IMAGE-RELEASE）：上游源为默认（美国 runner 绕道国内镜像会把构建拖成小时级），
# 国内 npm 镜像源一律 ARG 且默认空。
FROM node:22-slim AS build

ARG NPM_REGISTRY=""

WORKDIR /app

COPY package.json package-lock.json ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm ci --no-audit --no-fund --registry="$NPM_REGISTRY"; else npm ci --no-audit --no-fund; fi

COPY . .
RUN npm run build

FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/ /usr/share/nginx/html/

EXPOSE 80
