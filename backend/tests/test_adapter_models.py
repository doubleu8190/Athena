"""共享 ORM metadata 聚合测试。"""

from adapter.models import Base


def test_metadata_contains_all_domains_and_owner_foreign_keys() -> None:
    assert {
        "auth_sessions",
        "conversation_messages",
        "execution_messages",
        "projects",
        "sessions",
        "users",
    }.issubset(Base.metadata.tables)

    project_foreign_keys = {
        foreign_key.target_fullname for foreign_key in Base.metadata.tables["projects"].foreign_keys
    }
    session_foreign_keys = {
        foreign_key.target_fullname for foreign_key in Base.metadata.tables["sessions"].foreign_keys
    }
    assert "users.id" in project_foreign_keys
    assert "users.id" in session_foreign_keys
