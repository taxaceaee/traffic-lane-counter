FROM nginx:1.27-alpine

COPY frontend /usr/share/nginx/html
COPY deploy/frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY deploy/frontend/30-write-config.sh /docker-entrypoint.d/30-write-config.sh

RUN chmod +x /docker-entrypoint.d/30-write-config.sh

ENV FRONTEND_PORT=3000
ENV API_BASE_URL=/

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O- http://localhost:3000/ >/dev/null 2>&1 || exit 1
