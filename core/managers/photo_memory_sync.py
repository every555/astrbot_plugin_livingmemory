# -*- coding: utf-8 -*-
"""P2-13 多模态记忆 Slice1+2：写真馆照片 → 可检索视觉记忆。

数据流（只读写真馆 / 只写 livingmemory）：
    photo_index.json (photo_album 插件) ──差集──> documents 表视觉记忆

记忆体设计：
    content  = 【写真馆照片】文件名｜描述（或占位）
    metadata = media_type=photo, memory_origin=photo_album, file_name, file_path,
               hash, saved_at, desc_status(ok/pending), source=internal

幂等：按 metadata.file_name 对齐库内已有视觉记忆，差集才写。
调度：plugin_initializer 每日 tick + /lmem photosync 手动触发。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from astrbot.api import logger


class PhotoMemorySync:
    def __init__(self, index_path: str, db_path: str, memory_engine: Any):
        self.index_path = index_path
        self.db_path = db_path
        self.engine = memory_engine

    # ── 读侧 ──

    def load_index(self) -> dict[str, dict]:
        try:
            with open(self.index_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            logger.warning(f"[PhotoSync] 写真馆索引不存在: {self.index_path}")
            return {}
        except Exception as e:
            logger.warning(f"[PhotoSync] 索引读取异常: {e}")
            return {}

    def _existing_file_names(self) -> set[str]:
        """库内已有视觉记忆的 file_name 集合（幂等键）。"""
        names: set[str] = set()
        try:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
            rows = con.execute(
                "SELECT json_extract(metadata,'$.file_name') FROM documents "
                "WHERE json_extract(metadata,'$.memory_origin') = 'photo_album'"
            ).fetchall()
            con.close()
            names = {r[0] for r in rows if r[0]}
        except Exception as e:
            logger.warning(f"[PhotoSync] 库内已有查询异常(视为空): {e}")
        return names

    # ── 记忆体构造 ──

    @staticmethod
    def _build_content(file_name: str, entry: dict) -> str:
        desc = str(entry.get("description", "")).strip()
        if desc:
            body = desc
        else:
            saved = str(entry.get("saved_at", ""))[:10] or "未知日期"
            body = f"{saved} 入册的照片（描述待补，可后续用视觉理解补全）"
        return f"【写真馆照片】{file_name}｜{body}"

    @staticmethod
    def _build_metadata(file_name: str, entry: dict) -> dict:
        desc = str(entry.get("description", "")).strip()
        return {
            "media_type": "photo",
            "memory_origin": "photo_album",
            "file_name": file_name,
            "file_path": entry.get("file", ""),
            "hash": entry.get("hash", ""),
            "saved_at": entry.get("saved_at", ""),
            "size_kb": entry.get("size_kb", 0),
            "desc_status": "ok" if desc else "pending",
            "source": "internal",
            "topics": ["写真馆", "照片"],
            "key_facts": [desc[:100]] if desc else [],
            "sentiment": "positive",
        }

    # ── 主入口 ──

    async def sync(self, dry_run: bool = False) -> dict:
        index = self.load_index()
        existing = self._existing_file_names()
        todo = [(fn, e) for fn, e in index.items() if fn not in existing]
        report = {
            "dry_run": dry_run,
            "index_total": len(index),
            "already": len(index) - len(todo),
            "new": len(todo),
            "failed": 0,
            "preview": [fn for fn, _ in todo[:8]],
        }
        if dry_run or not todo:
            return report
        for file_name, entry in todo:
            try:
                await self.engine.add_memory(
                    content=self._build_content(file_name, entry),
                    session_id="photo_album",
                    importance=0.6,
                    metadata=self._build_metadata(file_name, entry),
                )
            except Exception as e:
                report["failed"] += 1
                logger.warning(f"[PhotoSync] 写入失败 {file_name}: {e}")
        if report["new"] or report["failed"]:
            logger.info(
                f"[PhotoSync] 同步完成: 新增 {report['new']} 失败 {report['failed']} "
                f"(索引 {report['index_total']} 已有 {report['already']})"
            )
        return report