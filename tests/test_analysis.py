"""analyze_traceback 异常栈解析测试。"""

from fastapi_doctor.analysis import analyze_traceback

SINGLE = """Traceback (most recent call last):
  File "app/main.py", line 12, in <module>
    engine = create_engine(DATABASE_URL)
sqlalchemy.exc.NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres
"""

CHAINED = """Traceback (most recent call last):
  File "app/main.py", line 12, in <module>
    engine = create_engine(DATABASE_URL)
  File "sqlalchemy/engine/create.py", line 544, in create_engine
    return _trio(n, **kwargs)
sqlalchemy.exc.NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:postgres

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "app/main.py", line 15, in <module>
    raise RuntimeError("数据库初始化失败") from error
RuntimeError: 数据库初始化失败
"""


def test_single_traceback_extracts_root_and_locations() -> None:
    info = analyze_traceback(SINGLE)

    assert info.exception_chain == ["sqlalchemy.exc.NoSuchModuleError"]
    assert info.root_exception == "sqlalchemy.exc.NoSuchModuleError"
    assert info.root_message == "Can't load plugin: sqlalchemy.dialects:postgres"
    assert info.files == ["app/main.py"]
    assert info.line_numbers == [12]
    assert "sqlalchemy.exc.NoSuchModuleError" in info.key_error_strings


def test_chained_traceback_keeps_full_chain() -> None:
    info = analyze_traceback(CHAINED)

    assert info.exception_chain == [
        "sqlalchemy.exc.NoSuchModuleError",
        "RuntimeError",
    ]
    assert info.root_exception == "RuntimeError"
    assert info.root_message == "数据库初始化失败"
    # 文件与行号保持出现顺序，按下标配对。
    assert info.files == ["app/main.py", "sqlalchemy/engine/create.py", "app/main.py"]
    assert info.line_numbers == [12, 544, 15]
    assert info.key_error_strings[0] == "RuntimeError"
    assert "Can't load plugin: sqlalchemy.dialects:postgres" in info.key_error_strings


def test_text_without_traceback_returns_empty_info() -> None:
    info = analyze_traceback("服务启动后一直卡住，没有任何报错输出。")

    assert info.exception_chain == []
    assert info.root_exception is None
    assert info.files == []
    assert info.line_numbers == []
    assert info.key_error_strings == []
