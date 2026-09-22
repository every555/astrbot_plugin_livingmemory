"""
向量存储抽象层（第一批升级#2，2026-09-16 橘子批准"做精不做量"）

借鉴 Mnemosyne 的 vector_db_base + factory 模式，但做精路线：
- 只定义协议 + 继承对齐现有 VectorRetriever（SQLite+FAISS 路线，够用不换）
- 不实写 Chroma/Milvus/Qdrant/Weaviate 适配器（无换库需求，写了就是死代码）
- 未来真要换后端：新写一个 VectorStoreBase 子类 + 契约测试通过即可接入，
  检索层（hybrid_retriever 等）代码零改动

设计取舍（与 Mnemosyne 的差异）：
- Mnemosyne 的 update = delete + re-insert（换新自增id，破坏外键）
- 本协议的 update_document_content = 同位向量置换（doc_id 不变，外键安全）
"""

from abc import ABC, abstractmethod
from typing import Any


class VectorStoreBase(ABC):
    """向量存储统一协议——六件套，任何后端必须完整实现"""

    @abstractmethod
    async def add_document(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> int:
        """插入文档（含向量计算与索引），返回整数 doc_id"""

    @abstractmethod
    async def update_document_content(self, doc_id: int, new_content: str) -> bool:
        """编辑后原地重嵌入（#1能力）：文档层由调用方更新，本方法只置换向量层"""

    @abstractmethod
    async def update_metadata(self, doc_id: int, metadata: dict[str, Any]) -> bool:
        """更新文档元数据"""

    @abstractmethod
    async def delete_document(self, doc_id: int) -> bool:
        """删除文档（含向量）"""

    @abstractmethod
    async def search(self, query: str, k: int = 10, **kwargs: Any) -> list[dict[str, Any]]:
        """语义检索 top-k"""

    @abstractmethod
    async def count(self) -> int:
        """当前文档总数"""
