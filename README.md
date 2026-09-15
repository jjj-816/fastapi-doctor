# FastAPI Doctor

面向 Python Web 服务的智能故障诊断 Agent。

当前里程碑实现了一个不依赖 LLM 的最小 LangGraph：

```text
START -> analyze_input -> clarify_if_needed -> plan -> END
```

它用于先验证状态定义、节点职责和条件路由。后续会把规则节点逐步替换为结构化模型输出，并接入混合检索。

## 本地运行

```powershell
uv sync --extra dev
.venv\Scripts\python -m pytest
.venv\Scripts\python -m uvicorn fastapi_doctor.api:app --reload
```

项目使用 `uv` 管理 Python 3.11 环境，依赖版本与本地
`agentic-rag-for-dummies` 学习项目保持一致。父子分块、父块存储和本地 BM25
稀疏向量实现由该项目迁移并进行包结构与可测试性改造。


创建一次最小诊断：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/diagnose `
  -ContentType application/json `
  -Body '{"description":"FastAPI returns 422 when creating a user","logs":"422 Unprocessable Entity"}'
```
