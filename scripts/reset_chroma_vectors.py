#!/usr/bin/env python3
"""删除 Athena 的 Chroma 向量 collection，供 embedding 模型切换后重建使用。"""

from __future__ import annotations

import argparse

import chromadb

from athena.config.settings import get_settings


COLLECTIONS = ("athena_file_chunks", "athena_memory")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete Athena Chroma collections before a full vector rebuild."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认删除两个 Athena 向量 collection",
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("此操作会删除全部向量；请显式传入 --yes")

    settings = get_settings()
    client = chromadb.PersistentClient(path=str(settings.chroma_path))
    for name in COLLECTIONS:
        try:
            client.delete_collection(name=name)
        except Exception as exc:
            # Chroma 对不存在 collection 的异常类型在版本间不同；删除目标
            # 已不存在时视为成功，其他错误继续抛出。
            if "not found" not in str(exc).lower() and "does not exist" not in str(exc).lower():
                raise
            print(f"skipped {name}: collection does not exist")
        else:
            print(f"deleted {name}")
    print("Vector collections deleted. Restart Athena to recreate them with the configured embedding model.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
