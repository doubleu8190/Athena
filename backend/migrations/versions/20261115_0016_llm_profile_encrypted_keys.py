"""Store encrypted administrator supplied LLM API keys."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261115_0016"
down_revision: str | None = "20261114_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_model_profile_versions", sa.Column("api_key_ciphertext", sa.LargeBinary()))
    op.add_column("llm_model_profile_versions", sa.Column("api_key_nonce", sa.LargeBinary(12)))
    op.add_column("llm_model_profile_versions", sa.Column("api_key_key_version", sa.Integer()))
    # 旧版本只记录环境变量名，无法从数据库恢复密钥，需管理员重新录入配置。
    op.drop_column("llm_model_profile_versions", "credential_env")
    op.execute(
        "UPDATE llm_model_profile_versions SET enabled = false "
        "WHERE api_key_ciphertext IS NULL OR api_key_nonce IS NULL "
        "OR api_key_key_version IS NULL"
    )
    # 保留不可变历史版本及 Run 快照，只移除新 Run 不可安全使用的当前指针。
    op.execute(
        "DELETE FROM llm_default_model_profile AS selected "
        "USING llm_model_profile_versions AS profile "
        "WHERE selected.profile_id = profile.id AND ("
        "profile.api_key_ciphertext IS NULL OR profile.api_key_nonce IS NULL "
        "OR profile.api_key_key_version IS NULL)"
    )
    op.execute(
        "DELETE FROM llm_current_compression_policy AS current "
        "USING llm_compression_policy_versions AS policy, "
        "llm_model_profile_versions AS profile "
        "WHERE current.policy_version_id = policy.id "
        "AND policy.compression_model_profile_id = profile.id AND ("
        "profile.api_key_ciphertext IS NULL OR profile.api_key_nonce IS NULL "
        "OR profile.api_key_key_version IS NULL)"
    )


def downgrade() -> None:
    op.add_column(
        "llm_model_profile_versions",
        sa.Column("credential_env", sa.String(128), nullable=False, server_default=""),
    )
    op.drop_column("llm_model_profile_versions", "api_key_key_version")
    op.drop_column("llm_model_profile_versions", "api_key_nonce")
    op.drop_column("llm_model_profile_versions", "api_key_ciphertext")
