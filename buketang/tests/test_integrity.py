"""发布完整性回归：备份迁移、恢复失败、配置继承和分段边界。"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="bt_integrity_")
        os.environ["BUKETANG_DATA_DIR"] = str(Path(cls.temp.name) / "data")
        import app
        cls.server = app
        cls.cfg = app.config

    @classmethod
    def tearDownClass(cls):
        cls.server.store._pool.shutdown(wait=True)
        cls.temp.cleanup()

    def setUp(self):
        shutil.rmtree(self.cfg.DATA_DIR, ignore_errors=True)
        self.cfg.ensure_dirs()
        self.server.store.reload()
        self.server.course_store.courses.reload()
        self.server.SUMMARY_JOBS.clear()
        self.client = self.server.app.test_client()

    def archive(self, entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name, value in entries.items():
                archive.writestr(name, value if isinstance(value, (str, bytes)) else json.dumps(value))
        return buf.getvalue()

    def restore(self, data):
        return self.client.post("/api/restore", data={"file": (io.BytesIO(data), "backup.zip")})

    def test_empty_backup_roundtrip_keeps_inheritance_and_keys(self):
        self.cfg.save({"llm": {"api_key": "local-test-secret"}})
        backup = self.client.get("/api/backup").data
        self.assertNotIn(b"local-test-secret", zipfile.ZipFile(io.BytesIO(backup)).read("config.json"))
        self.assertEqual(self.restore(backup).status_code, 200)
        self.assertEqual(self.cfg.load()["llm"]["api_key"], "local-test-secret")
        self.assertIsNone(self.cfg.load()["asr"])

    def test_missing_provider_credentials_are_preserved(self):
        self.cfg.save({"asr": {"provider": "openai", "providers": {"openai": {"api_key": "asr-test-secret"}}}})
        backup = self.archive({"manifest.json": {"app": "buketang"}, "config.json": {"asr": {"provider": "openai"}}})
        self.assertEqual(self.restore(backup).status_code, 200)
        self.assertEqual(self.cfg.load()["asr"]["providers"]["openai"]["api_key"], "asr-test-secret")

    def test_restore_rebases_windows_and_unix_paths(self):
        lesson = self.server.store.create("迁移测试")
        lid = lesson["id"]
        course = self.server.course_store.courses.create("课程")
        cid = course["id"]
        for relative, content in {
            f"lessons/{lid}/files/audio.wav": b"audio-fixture",
            f"lessons/{lid}/files/handout.pdf": b"pdf-fixture",
            f"lessons/{lid}/outputs/result.md": b"guide-fixture",
            f"courses/{cid}/summary.md": b"summary-fixture",
        }.items():
            path = self.cfg.DATA_DIR / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.server.store.update(lid,
            materials=[{"path": str(self.cfg.DATA_DIR / f"lessons/{lid}/files/handout.pdf"), "pdf_path": "", "status": "parsed"}],
            audios=[{"id": "audio", "filename": "audio.wav", "path": str(self.cfg.DATA_DIR / f"lessons/{lid}/files/audio.wav")}],
            guides={"extreme_md": str(self.cfg.DATA_DIR / f"lessons/{lid}/outputs/result.md")})
        self.server.course_store.courses.set_summary(cid, {"files": {"summary_md": str(self.cfg.DATA_DIR / f"courses/{cid}/summary.md")}})
        backup = self.client.get("/api/backup").data
        for old_root in ("C:\\old-computer\\data", "/home/old-computer/data"):
            with self.subTest(old_root=old_root):
                with zipfile.ZipFile(io.BytesIO(backup)) as archive:
                    entries = {name: archive.read(name) for name in archive.namelist()}
                for name, data in entries.items():
                    if name.endswith(".json"):
                        entries[name] = data.decode("utf-8").replace(
                            json.dumps(str(self.cfg.DATA_DIR))[1:-1], json.dumps(old_root)[1:-1])
                result = self.restore(self.archive(entries))
                self.assertEqual(result.status_code, 200, result.json)
                with self.client.get(f"/api/lessons/{lid}/audios/audio/file") as response:
                    self.assertEqual(response.data, b"audio-fixture")
                self.assertEqual(self.client.get(f"/api/lessons/{lid}/preview").json["content"], "guide-fixture")
                self.assertEqual(self.client.get(f"/api/courses/{cid}/summary/preview").json["content"], "summary-fixture")
                self.assertTrue(Path(self.server.store.get(lid)["materials"][0]["path"]).is_file())

    def test_invalid_backups_leave_existing_data_untouched(self):
        lesson = self.server.store.create("保留的课堂")
        cases = [
            {"manifest.json": {"app": "other"}},
            {"manifest.json": {"app": "buketang"}, "courses.json": "{"},
            {"manifest.json": {"app": "buketang"}, "courses.json": {"courses": "bad"}},
            {"manifest.json": {"app": "buketang"}, "config.json": []},
            {"manifest.json": {"app": "buketang"}, "../escape.txt": "bad"},
            {"manifest.json": {"app": "buketang"}, "lessons/test/lesson.json": {"id": "test", "guides": {"extreme_md": "/etc/passwd"}}},
        ]
        for entries in cases:
            with self.subTest(entries=list(entries)):
                self.assertEqual(self.restore(self.archive(entries)).status_code, 400)
                self.assertEqual(self.server.store.get(lesson["id"])["name"], "保留的课堂")
                self.assertTrue((self.cfg.LESSON_DIR / lesson["id"] / "lesson.json").is_file())

    def test_failed_restore_rolls_back(self):
        lesson = self.server.store.create("恢复失败也要保留")
        backup = self.client.get("/api/backup").data
        with patch.object(self.server, "_keep_secrets", side_effect=OSError("模拟写入失败")):
            self.assertEqual(self.restore(backup).status_code, 500)
        self.assertEqual(self.server.store.get(lesson["id"])["name"], "恢复失败也要保留")
        self.assertTrue((self.cfg.LESSON_DIR / lesson["id"] / "lesson.json").is_file())

    def test_restore_rejects_running_tasks(self):
        backup = self.client.get("/api/backup").data
        lesson = self.server.store.create("运行中")
        self.server.store.update(lesson["id"], status="analyzing")
        self.assertEqual(self.restore(backup).status_code, 409)

    def test_provider_patch_keeps_inherited_options(self):
        peer = {"provider": "openai", "providers": {"openai": {"api_key": "old", "base_url": "https://example.invalid/v1", "model": "model"}}}
        with patch.object(self.cfg, "_peer_asr", return_value=peer):
            cfg = self.cfg.asr_config({"asr": {"providers": {"openai": {"api_key": "new"}}}})
        self.assertEqual(cfg["providers"]["openai"], {**peer["providers"]["openai"], "api_key": "new"})

    def test_audio_tail_never_exceeds_provider_limit(self):
        from shared.audio_utils import plan_segments
        for duration in (600, 601, 1203, 1500.084, 1800.1):
            with self.subTest(duration=duration):
                plan = plan_segments(duration, 600)
                self.assertTrue(all(0 < length <= 600 for _, length in plan))
                self.assertAlmostEqual(sum(length for _, length in plan), duration)
                if len(plan) > 1:
                    self.assertGreaterEqual(plan[-1][1], 5)

    def test_libreoffice_fallback(self):
        from core import ingest
        source = self.cfg.DATA_DIR / "讲义.docx"
        source.write_bytes(b"fixture")
        target = self.cfg.DATA_DIR / "source.pdf"

        def convert(command, **kwargs):
            output = Path(command[command.index("--outdir") + 1]) / f"{source.stem}.pdf"
            output.write_bytes(b"%PDF-fixture")
            return ingest.subprocess.CompletedProcess(command, 0)

        with patch.object(ingest, "_office_com_to_pdf", return_value=False), \
             patch.object(ingest.shutil, "which", return_value="soffice"), \
             patch.object(ingest.subprocess, "run", side_effect=convert):
            self.assertTrue(ingest.office_to_pdf(source, target))
        self.assertEqual(target.read_bytes(), b"%PDF-fixture")


if __name__ == "__main__":
    unittest.main(verbosity=2)
