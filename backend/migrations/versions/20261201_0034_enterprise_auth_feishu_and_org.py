"""企业级身份认证升级（角色分权、飞书 SSO 字段）与组织架构分权三表。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261201_0034"
down_revision: str | None = "20261130_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """升级 users 表并创建 departments、user_departments、dept_admin_scopes 三表。"""
    # 1. 放宽密码与邮箱非空约束，将全局邮箱唯一约束替换为未删除用户的局部唯一索引。
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_constraint("ck_users_email_normalized", "users", type_="check")
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=320),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_users_email_normalized",
        "users",
        "email IS NULL OR email = lower(email)",
    )
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL AND deleted_time IS NULL"),
    )

    # 2. 先解除旧角色 CHECK 约束再迁移历史数据，随后收紧为三层企业分权角色枚举。
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.execute("UPDATE users SET role = 'super_admin' WHERE role = 'admin'")
    op.execute("UPDATE users SET role = 'member' WHERE role = 'user'")
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="member",
    )
    op.create_check_constraint(
        "ck_users_role",
        "users",
        "role IN ('super_admin', 'dept_admin', 'member')",
    )

    # 3. 补充身份提供方、飞书外部标识及企业员工资料列，并建立约束与局部唯一索引。
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.String(length=16),
            nullable=False,
            server_default="local",
        ),
    )
    op.add_column("users", sa.Column("external_union_id", sa.String(length=128), nullable=True))
    op.add_column("users", sa.Column("external_open_id", sa.String(length=128), nullable=True))
    op.add_column("users", sa.Column("employee_no", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(length=1024), nullable=True))
    op.add_column("users", sa.Column("job_title", sa.String(length=120), nullable=True))
    op.add_column("users", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_users_auth_provider",
        "users",
        "auth_provider IN ('local', 'feishu', 'mock')",
    )
    op.create_check_constraint(
        "ck_users_local_password",
        "users",
        "(auth_provider != 'local') OR (password_hash IS NOT NULL)",
    )
    op.create_index(
        "uq_users_external_union_id",
        "users",
        ["external_union_id"],
        unique=True,
        postgresql_where=sa.text("external_union_id IS NOT NULL AND deleted_time IS NULL"),
    )
    op.create_index(
        "uq_users_external_open_id",
        "users",
        ["external_open_id"],
        unique=True,
        postgresql_where=sa.text("external_open_id IS NOT NULL AND deleted_time IS NULL"),
    )

    # 4. 新建部门树表（使用物化路径支持子树高效前缀查询）。
    op.create_table(
        "departments",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column(
            "parent_id",
            sa.String(length=64),
            sa.ForeignKey("departments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("path", sa.String(length=2048), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_id", name="uq_departments_external_id"),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_departments_status",
        ),
    )
    op.create_index("ix_departments_path", "departments", ["path"])

    # 5. 新建用户-部门多对多归属表。
    op.create_table(
        "user_departments",
        sa.Column(
            "user_id",
            sa.String(length=128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "department_id",
            sa.String(length=64),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_user_departments_department",
        "user_departments",
        ["department_id", "user_id"],
    )

    # 6. 新建部门管理员管辖范围表。
    op.create_table(
        "dept_admin_scopes",
        sa.Column(
            "user_id",
            sa.String(length=128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "department_id",
            sa.String(length=64),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "include_subdepartments",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "granted_by",
            sa.String(length=128),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dept_admin_scopes_dept", "dept_admin_scopes", ["department_id"])


def downgrade() -> None:
    """回滚组织分权三表及 users 表企业认证升级字段。"""
    # 1. 删除部门分权、归属及部门树表。
    op.drop_index("ix_dept_admin_scopes_dept", table_name="dept_admin_scopes")
    op.drop_table("dept_admin_scopes")
    op.drop_index("ix_user_departments_department", table_name="user_departments")
    op.drop_table("user_departments")
    op.drop_index("ix_departments_path", table_name="departments")
    op.drop_table("departments")

    # 2. 移除新增外部身份索引、约束与列。
    op.drop_index("uq_users_external_open_id", table_name="users")
    op.drop_index("uq_users_external_union_id", table_name="users")
    op.drop_constraint("ck_users_local_password", "users", type_="check")
    op.drop_constraint("ck_users_auth_provider", "users", type_="check")
    op.drop_column("users", "last_synced_at")
    op.drop_column("users", "job_title")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "employee_no")
    op.drop_column("users", "external_open_id")
    op.drop_column("users", "external_union_id")
    op.drop_column("users", "auth_provider")

    # 3. 回滚三层角色枚举为旧 admin/user 双角色。
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.execute("UPDATE users SET role = 'admin' WHERE role = 'super_admin'")
    op.execute("UPDATE users SET role = 'user' WHERE role IN ('dept_admin', 'member')")
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=16),
        nullable=False,
        server_default="user",
    )
    op.create_check_constraint("ck_users_role", "users", "role IN ('admin', 'user')")

    # 4. 恢复 email 与 password_hash 的旧非空和全局唯一约束。
    op.drop_index("uq_users_email_active", table_name="users")
    op.drop_constraint("ck_users_email_normalized", "users", type_="check")
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=320),
        nullable=False,
    )
    op.create_check_constraint("ck_users_email_normalized", "users", "email = lower(email)")
    op.create_unique_constraint("uq_users_email", "users", ["email"])
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.String(length=255),
        nullable=False,
    )
