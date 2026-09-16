---
title: "容器内连接 PostgreSQL 报 Connection refused"
components: [fastapi, docker, sqlalchemy]
error_types: ["OperationalError"]
risk_level: medium
source_url: "https://www.postgresql.org/docs/current/runtime-config-connection.html"
---

# 症状

FastAPI 应用在 Docker Compose 中运行，访问 PostgreSQL 报错；宿主机直连同一数据库正常，进容器就不行。

```text
sqlalchemy.exc.OperationalError: (psycopg2.OperationalError)
connection to server at "localhost" (::1), port 5432 failed: Connection refused
```

# 根因分析

容器拥有独立的网络命名空间，容器内的 localhost 指向容器自身，而不是宿主机或数据库容器。
连接串里写 localhost/127.0.0.1 时，容器内并没有 PostgreSQL 在监听，TCP 立即被拒绝——
表现为 refused（立即失败）而不是 timeout（等待后失败）。

# 解决方案

把连接串中的数据库主机改为 docker-compose 服务名（例如 postgres），依赖 Compose
内置 DNS 解析；端口使用容器内部端口 5432，而不是宿主机映射端口。

# 验证方式

进入应用容器执行 `pg_isready -h postgres -p 5432`，返回 accepting connections
说明网络与端口都可达。
