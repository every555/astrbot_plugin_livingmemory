"""P1-② 反射弧③ 集成测试：四处挂载的源码级断言。"""
import os

PLUG = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def _read(rel):
    with open(os.path.join(PLUG, rel), encoding="utf-8") as f:
        return f.read()

def test_engine_pipeline_mount():
    """search_memories 尾部必须挂 self_checker.check。"""
    src = _read(os.path.join("core", "managers", "memory_engine.py"))
    assert "self.self_checker.check(results)" in src
    assert "RetrievalSelfCheck(" in src

def test_hybrid_result_field():
    """HybridResult 必须有 conflict_warnings 字段。"""
    src = _read(os.path.join("core", "retrieval", "hybrid_retriever.py"))
    assert "conflict_warnings" in src

def test_injection_render():
    """注入格式化必须呈现标注。"""
    src = _read(os.path.join("core", "utils", "__init__.py"))
    assert "[逻辑自检]" in src
    assert "conflict_warnings" in src

def test_reflex3_in_memory_recall():
    """反射弧③：memory_recall 必须调 get_recent_confirmed_alerts 拼提醒。"""
    src = _read(os.path.join("core", "event_handler_modules", "memory_recall.py"))
    assert "get_recent_confirmed_alerts" in src
    assert "已确认矛盾提醒" in src
