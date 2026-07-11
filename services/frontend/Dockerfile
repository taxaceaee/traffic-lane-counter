# TrafficFlow Frontend SPA
# Build from repo root:
#   docker build -f services/frontend/Dockerfile -t trafficflow-frontend .
FROM nginx:1.27-alpine

COPY services/frontend /usr/share/nginx/html
# Remove deploy-only files from the static root
RUN rm -f /usr/share/nginx/html/Dockerfile \
          /usr/share/nginx/html/nginx.conf.template \
          /usr/share/nginx/html/README.md \
          /usr/share/nginx/html/30-write-config.sh

COPY services/frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY services/frontend/30-write-config.sh /docker-entrypoint.d/30-write-config.sh
RUN chmod +x /docker-entrypoint.d/30-write-config.sh

ENV FRONTEND_PORT=3000
ENV API_BASE_URL=/

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O- http://localhost:3000/ >/dev/null 2>&1 || exit 1
