"""异常栈解析（设计 §4.4 的 analyze_traceback 工具）。

从原始 traceback 文本中提取异常链、根异常、出错文件、行号和关键错误字符串，
供诊断规划与检索查询使用。纯文本规则实现，不做任何模型调用。
"""

import re

from fastapi_doctor.domain.models import TracebackInfo

# Python 异常类名几乎总以 Error/Exception/Warning/Interrupt/Exit 结尾，
# 限定词尾避免把普通代码行（如 foo.bar: something）误判为异常。
EXCEPTION_RE = re.compile(
    r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|Exit))\s*:?\s*(.*)$"
)
FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)')

# 异常链分隔符：chained exception 的两个固定提示行。
CHAIN_SEPARATOR_RE = re.compile(
    r"\n(?:During handling of the above exception, another exception occurred:|"
    r"The above exception was the direct cause of the following exception:)\n"
)


def analyze_traceback(text: str) -> TracebackInfo:
    """解析 traceback 文本，返回结构化异常信息；无异常时返回空结果。"""
    info = TracebackInfo()
    blocks = CHAIN_SEPARATOR_RE.split(text)
    exception_chain: list[str] = []
    key_strings: list[str] = []

    for block in blocks:
        name, message = _last_exception_line(block)
        if name is None:
            continue
        exception_chain.append(name)
        if message and message not in key_strings:
            key_strings.append(message)

    if exception_chain:
        info.exception_chain = exception_chain
        info.root_exception = exception_chain[-1]
        info.root_message = key_strings[-1] if key_strings else ""
        info.key_error_strings = [info.root_exception] + key_strings

    # files 与 line_numbers 保持出现顺序，按下标配对。
    matches = list(FILE_LINE_RE.finditer(text))
    info.files = [match.group(1) for match in matches]
    info.line_numbers = [int(match.group(2)) for match in matches]
    return info


def _last_exception_line(block: str) -> tuple[str | None, str]:
    """取一个异常块中最后一行异常声明（块尾即该次抛出的异常）。"""
    for line in reversed(block.splitlines()):
        stripped = line.strip()
        match = EXCEPTION_RE.match(stripped)
        if match:
            return match.group(1), match.group(2).strip()
    return None, ""
