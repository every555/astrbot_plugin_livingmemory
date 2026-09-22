# -*- coding: utf-8 -*-
"""VectorStoreBase 契约测试（第一批升级#2，2026-09-16）

任何向量后端适配器接入前必须通过本契约：
1. 协议六件套可被完整实现（FakeStore + 单文件直接加载，零框架依赖）
2. 现有 VectorRetriever 已对齐协议（AST 静态检查，不 import 框架）
"""
import asyncio
import ast
import importlib.util
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# ── 1. 协议自洽：单文件加载 vector_store_base.py（只依赖 abc/typing） ──
_spec = importlib.util.spec_from_file_location(
    "vector_store_base", PLUGIN_ROOT / "core" / "retrieval" / "vector_store_base.py"
)
vsb_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vsb_mod)
VectorStoreBase = vsb_mod.VectorStoreBase


class FakeStore(VectorStoreBase):
    """最小合法实现——验证协议可被完整实现"""

    def __init__(self):
        self.docs: dict[int, str] = {}
        self._next = 1

    async def add_document(self, content, metadata=None):
        doc_id = self._next
        self._next += 1
        self.docs[doc_id] = content
        return doc_id

    async def update_document_content(self, doc_id, new_content):
        if doc_id not in self.docs:
            return False
        self.docs[doc_id] = new_content
        return True

    async def update_metadata(self, doc_id, metadata):
        return doc_id in self.docs

    async def delete_document(self, doc_id):
        return self.docs.pop(doc_id, None) is not None

    async def search(self, query, k=10, **kwargs):
        return [{"doc_id": i, "text": t} for i, t in list(self.docs.items())[:k]]

    async def count(self):
        return len(self.docs)


def test_protocol_completeness():
    store = FakeStore()
    doc_id = asyncio.run(store.add_document("春雪的记忆"))
    assert doc_id == 1
    assert asyncio.run(store.count()) == 1
    assert asyncio.run(store.update_document_content(doc_id, "编辑后的记忆")) is True
    assert asyncio.run(store.delete_document(doc_id)) is True
    assert asyncio.run(store.count()) == 0


def test_vector_retriever_aligns_protocol():
    """AST 静态检查：VectorRetriever 继承协议 + 六件套方法齐全"""
    src = (PLUGIN_ROOT / "core" / "retrieval" / "vector_retriever.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)

    vr_cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "VectorRetriever":
            vr_cls = node
            break
    assert vr_cls is not None, "VectorRetriever 类不存在"

    base_names = [b.id for b in vr_cls.bases if isinstance(b, ast.Name)]
    assert "VectorStoreBase" in base_names, f"未继承协议: {base_names}"

    methods = {
        n.name for n in vr_cls.body if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    required = {
        "add_document",
        "update_document_content",
        "update_metadata",
        "delete_document",
        "search",
        "count",
    }
    missing = required - methods
    assert not missing, f"VectorRetriever 缺协议方法: {missing}"


if __name__ == "__main__":
    test_protocol_completeness()
    test_vector_retriever_aligns_protocol()
    print("VectorStoreBase 契约测试: 全部通过 ✓")
