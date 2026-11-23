"""长期记忆敏感文本检测的精确拦截与低误杀回归测试。"""

from __future__ import annotations

import pytest

from app.memory.sensitive import contains_sensitive_text


@pytest.mark.parametrize(
    "benign_text",
    [
        # Unix 毫秒时间戳（含恰好满足 Luhn 的尾数）不应被误判为银行卡号
        "事件发生在 1700000000000",
        "部署时间戳 1700000000006",
        # 普通年份前缀订单号与 ISBN-13 不应被误判
        "订单号 2026112300012345 已签收",
        "账单日期 2026-11-23 金额 500 元",
        "参考书籍 ISBN 978-0-13-468599-1",
        "全零占位 0000000000000000",
        # 裸 token 与裸 id 语境放行
        "本项目默认 context token 是 128000",
        "token = cl100k_base",
        "token_limit_128000",
        "我的用户 id 是 alice",
        # 英文高敏键后跟自然语言说明句放行
        "API key 是通过环境变量注入的",
        "API key 是从 .env 文件读取的",
        "API key 是由 AWS KMS 托管的",
        "API key = （暂无）",
        "API key is loaded from environment variables",
        "password is required for login",
        "secret 是由密钥管理系统托管的",
    ],
)
def test_sensitive_detector_allows_benign_technical_and_numeric_texts(benign_text: str) -> None:
    """常见技术配置、时间戳、订单号、ISBN 与说明性语句不应被误杀。"""
    assert contains_sensitive_text(benign_text) is False


@pytest.mark.parametrize(
    "sensitive_text",
    [
        # 真实满足 Luhn 的 Visa / MasterCard / Amex / 银联卡号（连续与分组格式）
        "4111-1111-1111-1111",
        "4532015112830366",
        "5500 0000 0000 0004",
        "2221 0000 0000 0009",
        "3782 822463 10005",
        "6222 0202 0000 1234 562",
        # 前置日期/金额等数字紧邻真卡号时，不能因贪婪吞并导致漏拦
        "账单 2026-11-23 4532015112830366",
        "2026-01-01 4111-1111-1111-1111",
        "付款日期 2026-11-23 5500 0000 0000 0004",
        "金额 500 4111 1111 1111 1111",
        "100 4532015112830366",
        # 明文密码、高敏凭据键赋值（含中英混排紧邻中文注释/全角括号）与既有测试样例
        "API key 是 abc123456（生产环境）",
        "我的 password=hunter2请保密",
        "api_key: prod_998877（仅限内网）",
        'password = "hunter2"（生产）',
        "api_key: abc123456【生产】",
        "请记住我的密码是 hunter2",
        "用户密码是 hunter2",
        "password=hunter2",
        "password: mysecret99",
        "password is hunter2",
        "api_key: sk_live_abc123456",
        "API key = prod_key_998877",
        "access_token = tok_12345678",
        "refresh_token: rt_xyz987654",
        "auth_token=abc123456",
        "bearer_token: b_12345678",
        "secret = my_super_secret",
        "id_number: 110101199001011234",
        "id_no = A12345678",
        "card_no = 4111111111111111",
        "card_number: 4111-1111-1111-1111",
        "access_token_value_123456",
        # 已知密钥格式、授权头、私钥头与 18 位身份证号
        "-----BEGIN RSA PRIVATE KEY-----",
        "sk-1234567890abcdef",
        "ghp_1234567890abcdef",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3OCJ9.abcdefghij",
        "Authorization: Bearer abc.def.ghi",
        "11010119900101123X",
    ],
)
def test_sensitive_detector_blocks_real_credentials_and_luhn_bank_cards(
    sensitive_text: str,
) -> None:
    """真实银行卡号、明文密码、高敏凭据键赋值与密钥格式必须 100% 被拦截。"""
    assert contains_sensitive_text(sensitive_text) is True
