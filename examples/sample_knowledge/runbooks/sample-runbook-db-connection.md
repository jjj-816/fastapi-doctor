---
title: "数据库连接失败排查手册"
components: [sqlalchemy, docker]
risk_level: medium
source_url: "https://docs.sqlalchemy.org/en/20/core/connections.html"
---

# 适用症状

应用启动或首个请求访问数据库时报 Connection refused 或连接超时。

# 第 1 步：确认网络可达

在应用容器内执行 `pg_isready -h <db主机> -p 5432`。失败说明网络不通、主机名解析错误或端口不对。

# 第 2 步：核对连接串主机名

容器内必须使用 compose 服务名作为数据库主机，不能使用 localhost——容器网络命名空间是隔离的。

# 第 3 步：核对端口与监听地址

确认连接的是容器内部端口 5432 而非宿主机映射端口；并确认 PostgreSQL 的
listen_addresses 配置包含容器网段（默认只监听 localhost 时容器间不可达）。

# 第 4 步：区分认证失败

网络可达但凭据错误时，报错是 password authentication failed，与 Connection refused
含义不同——后者根本没建立 TCP 连接。
