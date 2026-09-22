import asyncio
import os


class TestDiagLearnNouns:
    def test_diag_no_table(self):
        import tempfile, sqlite3, time, logging
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "gate.db")
        c = sqlite3.connect(db)
        c.execute("""CREATE TABLE gate_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, speaker TEXT, content TEXT,
            score REAL, axes TEXT, metadata TEXT, source TEXT,
            status TEXT DEFAULT 'candidate', verdict TEXT, note TEXT,
            created_at REAL, reviewed_at REAL)""")
        c.execute("INSERT INTO gate_candidates (speaker, source, content, score, status, created_at) VALUES ('橘子','user','下周三我生日',0.6,'candidate',?)", (time.time(),))
        c.commit(); c.close()

        logging.basicConfig(level=logging.DEBUG)
        async def fake(prompt):
            return '{"verdicts": [{"id": 1, "action": "confirm", "word": "升级", "note": "x"}]}'

        sched = ReflectionScheduler(db_path=db, provider_fn=lambda: "FAKE", llm_fn=fake, clock=lambda: 1000.0)
        report = asyncio.run(sched.run_reflection())
        print("REPORT:", report)
        c = sqlite3.connect(db)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        print("TABLES:", tables)
        if "learned_nouns" in tables:
            print("WORDS:", [r for r in c.execute("SELECT word FROM learned_nouns")])
        c.close()
