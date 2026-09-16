# Connection refused when connecting to PostgreSQL from a container

When a FastAPI application runs inside a Docker container, `localhost` refers to
the container itself, not to the host machine or the database container. A
connection string that works on the host therefore fails inside the container
with `Connection refused`.

## Service names instead of localhost

Inside a Compose network, reach the database by its service name. Compose
provides DNS resolution between services on the same network:

```yaml
services:
  app:
    image: myapp
    environment:
      DATABASE_URL: postgresql+psycopg://user:pass@postgres:5432/db
  postgres:
    image: postgres:16
    ports:
      - "5432:5432"
```

Use the container-internal port (5432) in the connection string even when the
host publishes a different port.

## Verifying connectivity

Run `pg_isready -h postgres -p 5432` inside the application container. If it
reports `accepting connections`, the network path and port are correct and the
problem lies in credentials or the database name.
