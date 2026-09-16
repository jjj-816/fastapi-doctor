"""脱敏规则单元测试：发送给模型或落库前遮蔽凭据。"""

from fastapi_doctor.security import mask_secrets


def test_masks_openai_style_key() -> None:
    text = "我的 key 是 sk-abc123def456ghi789，别泄露"
    assert "sk-abc123def456ghi789" not in mask_secrets(text)
    assert "sk-***" in mask_secrets(text)


def test_masks_authorization_header() -> None:
    text = "Authorization: Bearer eyJhbGciOi.eyJzdWIi.Lf9x"
    masked = mask_secrets(text)
    assert "eyJhbGciOi" not in masked
    assert "Authorization: ***" in masked


def test_masks_key_value_credentials() -> None:
    for text in (
        "password=hunter2",
        "PASSWORD: hunter2",
        "api_key=abc123def",
        "token: eyJhbGc",
        "secret = topsecret",
    ):
        masked = mask_secrets(text)
        assert "hunter2" not in masked
        assert "abc123def" not in masked
        assert "eyJhbGc" not in masked
        assert "topsecret" not in masked


def test_masks_database_url_password() -> None:
    text = "postgres://admin:s3cr3t@db.internal:5432/app"
    masked = mask_secrets(text)
    assert "s3cr3t" not in masked
    assert "postgres://admin:***@db.internal:5432/app" == masked


def test_plain_text_untouched() -> None:
    text = "连接 postgresql://db.internal:5432/app 失败，组件 database 启动异常"
    assert mask_secrets(text) == text


def test_masking_is_idempotent() -> None:
    text = "password=hunter2 and sk-abc123def456ghi789"
    once = mask_secrets(text)
    assert mask_secrets(once) == once
