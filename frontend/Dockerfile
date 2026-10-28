# Сборка статики и раздача через nginx. Адрес API задаёт переменная API_UPSTREAM (по умолчанию http://api:8000).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# true — собрать демо со встроенным моком API, без бэкенда
ARG VITE_MOCK_API=false
ENV VITE_MOCK_API=$VITE_MOCK_API
RUN npm run build

FROM nginx:1.27-alpine
ENV API_UPSTREAM=http://api:8000 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD wget -qO- http://127.0.0.1/healthz || exit 1
