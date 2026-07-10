# Database Deployment

TrafficFlow uses PostgreSQL as the primary database.

For a separate database deployment, provide the backend with:

- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `DATABASE_URL`

The integration stack example lives in `deploy/stack/docker-compose.yml`, but
production database hosting can use any managed or self-hosted PostgreSQL
service as long as `DATABASE_URL` points to it.
