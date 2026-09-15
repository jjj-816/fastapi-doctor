"""敏感信息脱敏（设计 §7）：发送给模型或写入持久化前遮蔽凭据。

规则基于常见凭据形态：OpenAI 风格 Key、Authorization 头、键值对形式的
password/token/api_key/secret，以及数据库连接串中的口令段。脱敏是尽力
而为的静态规则，README 需提示用户仍应避免提交生产凭据。
"""

import re

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI 风格密钥（sk- 开头的长随机串）
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    # Authorization: Bearer <token> / Authorization: <token>
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?\S+"),
        r"\1***",
    ),
    # 键值对：password/pwd/token/api_key/secret/access_key = 值
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd)"
            r"(\s*[:=]\s*)(?!\*)([^\s,;\"']+)"
        ),
        r"\1\2***",
    ),
    # 数据库连接串口令段：postgres://user:pass@host -> postgres://user:***@host
    (
        re.compile(
            r"(?i)\b((?:postgres(?:ql)?|mysql|redis|mongodb(?:\+srv)?|amqp)://[^:/\s@]+:)"
            r"[^@\s/]+@"
        ),
        r"\1***@",
    ),
]


def mask_secrets(text: str) -> str:
    """遮蔽文本中的常见凭据；对已脱敏文本重复调用应保持稳定。"""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
