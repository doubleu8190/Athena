"""为 PostgreSQL 集成测试生成与生产格式一致的 LLM 凭据密文。"""

from domain.mcp import McpSecretPurpose
from infrastructure.security.mcp_secrets import McpSecretCipher

TEST_LLM_CIPHER = McpSecretCipher(bytes(range(32)))


def encrypted_llm_key_fields(profile_id: str, api_key: str = "test-secret") -> dict[str, object]:
    """返回 ORM 可直接持久化的凭据密文字段。"""
    sealed = TEST_LLM_CIPHER.seal(
        profile_id,
        McpSecretPurpose.LLM_API_KEY,
        {"api_key": api_key},
    )
    return {
        "api_key_ciphertext": sealed.ciphertext,
        "api_key_nonce": sealed.nonce,
        "api_key_key_version": sealed.key_version,
    }
