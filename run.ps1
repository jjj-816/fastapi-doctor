# FastAPI Doctor 一键启动：后端 API + 前端界面（同端口 8000）。
# 用法：项目根目录下执行  .\run.ps1
# 若提示"禁止运行脚本"，先执行一次：
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
$env:PYTHONIOENCODING = "utf-8"
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn fastapi_doctor.api:app --port 8000
