"""Regression tests for staged transcripts, trusted config, evidence, and Word math."""
from __future__ import annotations

import copy
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


class UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="buketang_update_")
        os.environ["BUKETANG_DATA_DIR"] = str(Path(cls.temp.name) / "data")
        import app
        cls.server = app
        cls.config = app.config

    @classmethod
    def tearDownClass(cls):
        cls.server.store._pool.shutdown(wait=True)
        cls.temp.cleanup()

    def setUp(self):
        shutil.rmtree(self.config.DATA_DIR, ignore_errors=True)
        self.config.ensure_dirs()
        self.store = self.server.store
        self.store.reload()
        self.server.course_store.courses.reload()
        self.client = self.server.app.test_client()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.server.app.extensions["local_security_token"]

    def lesson(self):
        lesson = self.store.create("Test lecture")
        audio = self.store.files_dir(lesson["id"]) / "test.wav"
        audio.write_bytes(b"fake ASR source")
        self.store.update(lesson["id"], audios=[{"id": "audio", "filename": "test.wav", "path": str(audio), "duration": 1}],
                          materials=[{"id": "material", "filename": "handout.txt", "status": "parsed",
                                      "pages": [{"index": 1, "text": "z = wx + b. w is a weight."}]}])
        return lesson

    def test_config_never_exposes_provider_credentials(self):
        self.config.save({"llm": {"api_key": "llm-test-secret"},
                          "asr": {"provider": "volc", "providers": {"volc": {"api_key": "asr-test-secret", "access_key": "access-test-secret", "app_id": "app-test-secret"}}}})
        response = self.client.get("/api/config")
        for value in (b"llm-test-secret", b"asr-test-secret", b"access-test-secret", b"app-test-secret"):
            self.assertNotIn(value, response.data)

    def test_changed_endpoint_does_not_reuse_key(self):
        self.config.save({"llm": {"base_url": "https://first.example/v1", "api_key": "test-key"}})
        self.config.save({"llm": {"base_url": "https://other.example/v1"}})
        self.assertEqual(self.config.load()["llm"]["api_key"], "")

    def test_restore_preserves_local_connection(self):
        self.config.save({"llm": {"base_url": "https://first.example/v1", "api_key": "test-key"}})
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", '{"app":"buketang"}')
            archive.writestr("config.json", '{"llm":{"base_url":"https://different.example/v1"}}')
        response = self.client.post("/api/restore", data={"file": (io.BytesIO(buffer.getvalue()), "backup.zip")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config.load()["llm"]["base_url"], "https://first.example/v1")
        self.assertEqual(self.config.load()["llm"]["api_key"], "test-key")

    def test_cross_site_host_and_missing_token_rejected(self):
        self.assertEqual(self.client.post("/api/courses", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/security", headers={"Host": "evil.example:8759"}).status_code, 403)
        self.assertEqual(self.server.app.test_client().post("/api/courses").status_code, 403)
        self.assertEqual(self.client.post("/api/courses", json={"name": "allowed"}).status_code, 200)

    def test_local_url_requires_exact_host(self):
        from shared.local_security import local_url
        self.assertTrue(local_url("http://127.0.0.1:11434/v1"))
        self.assertFalse(local_url("https://localhost.evil.example/v1"))
        self.assertFalse(local_url("https://evil.example/127.0.0.1"))

    def test_clean_classifies_ids_and_preserves_exam_corrections(self):
        from core.preprocess import clean
        from shared.transcript import normalize, text
        raw = normalize([{"text": "今天吃什么", "start": 0, "end": 1},
                         {"text": "这个不考，分母刚才说错了", "start": 2, "end": 3}])
        class Client:
            def chat_json(self, *args, **kwargs):
                return {"items": [{"id": item["id"] + "_p0", "category": "irrelevant", "reason": "fixture"} for item in raw]}
        result = clean(raw, Client())
        self.assertNotIn("今天吃什么", text(raw, result, "clean"))
        self.assertIn("分母", text(raw, result, "clean"))
        self.assertIn("今天吃什么", text(raw, result, "raw"))
        class Incomplete:
            def chat_json(self, *args, **kwargs):
                return {"items": []}
        from core.llm import LLMError
        with self.assertRaises(LLMError):
            clean(raw, Incomplete())

    def test_manual_edit_export_does_not_modify_original(self):
        from shared.transcript import normalize
        lesson = self.lesson()
        raw = normalize([{"text": "original", "start": 1, "end": 2, "estimated_time": True}])
        self.store.update(lesson["id"], transcript=raw, transcript_text="[00:00:01] original")
        uri = f"/api/lessons/{lesson['id']}/transcript/segments/{raw[0]['id']}"
        response = self.client.patch(uri, json={"version": "", "text": "corrected"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.patch(uri, json={"version": "", "text": "stale"}).status_code, 409)
        self.assertEqual(self.store.get(lesson["id"])["transcript"][0]["text"], "original")
        clean = self.client.get(f"/api/lessons/{lesson['id']}/transcript?view=clean").json
        self.assertIn("corrected", clean["text"])
        self.assertTrue(clean["segments"][0]["estimated_time"])
        with self.client.get(f"/api/lessons/{lesson['id']}/transcript/download?format=json") as exported:
            self.assertEqual(json.loads(exported.data)["segments"][0]["text"], "original")

    def test_failed_rerun_preserves_published_guide(self):
        lesson = self.lesson()
        original = {"extreme_md": "old-guide"}
        self.store.update(lesson["id"], guides=original, analysis={"topic": "old"}, materials=[])
        self.store._run_guarded(lesson["id"])
        self.assertEqual(lesson["guides"], original)
        self.assertEqual(lesson["analysis"]["topic"], "old")
        self.assertEqual(lesson["status"], "failed")

    def test_failed_chunk_is_not_published_and_retry_reuses_checkpoint(self):
        from core.analyze import analyze, CourseContext
        from core.llm import LLMError
        material = [{"pages": [{"index": 1, "text": "A" * 30000}]}]
        class Client:
            calls = 0
            fail = True
            input_chars = 10000
            def chat_json(self, *args, **kwargs):
                self.calls += 1
                if self.fail and self.calls == 2:
                    raise LLMError("fixture failure")
                return {"key_points": [{"title": str(self.calls), "detail": "valid", "importance": "5.0", "pages": [1]}]}
        client = Client()
        cache = self.config.DATA_DIR / "checkpoints"
        with self.assertRaisesRegex(LLMError, "未发布不完整结果"):
            analyze(material, "lecture", client, checkpoint_dir=cache)
        self.assertEqual(len(list(cache.glob("*.json"))), 1)
        client.fail = False
        result = analyze(material, "lecture", client, checkpoint_dir=cache)
        self.assertTrue(result["stats"]["complete"])
        self.assertTrue(all(len(a) + len(b) <= 8000 for a, b in CourseContext(material, "lecture").chunk(limit=8000)))

    def test_full_pipeline_and_export_reuse_asr(self):
        from shared.transcript import normalize
        lesson = self.lesson()
        self.config.save({"llm": {"base_url": "http://127.0.0.1:11434/v1", "model": "fixture"}})
        raw = normalize([{"text": "weight is a parameter", "start": 0, "end": 1}])
        def response(client, messages, **kwargs):
            if "目标ID" in messages[-1]["content"]:
                return {"items": [{"id": raw[0]["id"] + "_p0", "category": "lecture", "reason": "课程"}]}
            return {"topic": "Network", "summary": "Model", "key_points": [{"title": "Weight", "detail": "parameter", "pages": [1],
                    "formulas": [{"latex": r"z=\frac{w}{x}", "pages": [1]}],
                    "tables": [{"columns": ["Symbol", "Meaning"], "rows": [["w", "weight"]]}]}]}
        with patch.object(self.store, "_transcribe_all", return_value=raw), patch("core.llm.LLMClient.chat_json", response):
            self.store._run(lesson["id"])
        self.assertEqual(lesson["status"], "done")
        first = lesson["current_revision"]
        with patch.object(self.store, "_transcribe_all", side_effect=AssertionError("ASR must not run during export")):
            self.store._run(lesson["id"], "export")
        self.assertNotEqual(lesson["current_revision"], first)
        self.assertEqual(lesson["history"][0]["id"], first)
        with zipfile.ZipFile(lesson["guides"]["detailed_docx"]) as archive:
            xml = archive.read("word/document.xml").decode()
        self.assertIn("oMath", xml)
        self.assertIn("w:tbl", xml)

    def test_word_does_not_read_arbitrary_local_images(self):
        from shared.document import write_docx
        root = self.config.DATA_DIR / "assets"
        root.mkdir()
        target = root / "test.docx"
        write_docx("# Proof\n\n$$x^2 + y^2$$\n\n![unsafe](file:///outside/private.png)", target, root)
        with zipfile.ZipFile(target) as archive:
            self.assertFalse(any(name.startswith("word/media/") for name in archive.namelist()))

    def test_memory_is_scoped_ordered_and_updates_with_source(self):
        from core.memory import retrieve
        course = {"id": "course", "memory_enabled": True}
        current = {"id": "current", "name": "now", "course_id": "course", "sequence": 2}
        prior = {"id": "prior", "name": "before", "course_id": "course", "sequence": 1,
                 "analysis": {"key_points": [{"title": "weight", "detail": "parameter", "pages": [1]}]}}
        future = {**copy.deepcopy(prior), "id": "future", "sequence": 3}
        other = {**copy.deepcopy(prior), "id": "other", "course_id": "another"}
        result = retrieve(current, course, [current, prior, future, other], "weight")
        self.assertEqual([item["lesson_id"] for item in result["sources"]], ["prior"])
        prior["analysis"]["key_points"][0]["detail"] = "revised"
        self.assertNotEqual(result["snapshot"], retrieve(current, course, [current, prior, future, other], "weight")["snapshot"])

    def test_raw_transcript_has_no_hidden_line_cap(self):
        from core.store import transcript_to_text
        raw = [{"text": "text", "start": n, "end": n + 1} for n in range(9000)]
        self.assertIn("[02:29:58]", transcript_to_text(raw))

    def test_external_image_addresses_are_restricted(self):
        from shared.resources import remote_image_url
        for url in ("file:///private.png", "https://127.0.0.1/a", "https://10.0.0.1/a", "https://user:pass@example.com/a", "http://example.com/a"):
            self.assertFalse(remote_image_url(url), url)
        self.assertTrue(remote_image_url("https://example.com/image.png"))

    def test_private_dns_image_cannot_be_fetched(self):
        from shared.resources import cache_image
        with patch("shared.resources.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(ValueError, "内网"):
                cache_image("https://example.com/image.png", self.config.DATA_DIR / "images")

    def test_cancel_during_export_keeps_previous_version(self):
        lesson = self.lesson()
        self.store.update(lesson["id"], guides={"extreme_md": "old"}, analysis={"key_points": []})
        def finish(*args, **kwargs):
            self.store.cancel(lesson["id"])
            return {"extreme_md": "new"}
        with patch("core.compose.build_guides", side_effect=finish):
            self.store._run_guarded(lesson["id"], "export")
        self.assertEqual(lesson["status"], "canceled")
        self.assertEqual(lesson["guides"], {"extreme_md": "old"})

    def test_screenshots_are_budgeted_and_deduplicated(self):
        from core.compose import render_detailed_md
        analysis = {"key_points": [{"title": "one", "pages": [1], "illustration_pages": [1]},
                                    {"title": "two", "pages": [1], "illustration_pages": [1]}]}
        rendered = render_detailed_md("fixture", analysis, {1: "assets/page.jpg"})
        self.assertEqual(rendered.count("![讲义第"), 1)

    def test_vision_evidence_preserves_page_source(self):
        from core import ingest
        lesson = self.lesson()
        pdf = self.store.files_dir(lesson["id"]) / "source.pdf"
        doc = ingest.pymupdf.open()
        doc.new_page().insert_text((50, 50), "x^2 + y^2")
        doc.save(str(pdf))
        doc.close()
        lesson["materials"][0]["pdf_path"] = str(pdf)
        self.store.update(lesson["id"], requested_formula_pages=[1])
        self.config.save({"llm": {"base_url": "http://127.0.0.1:11434/v1", "vision_model": "fixture-vision"}})
        captured = []
        def recognize(client, messages, **kwargs):
            captured.append(messages)
            return {"formulas": [{"latex": "x^2+y^2", "explanation": "fixture"}]}
        with patch("core.llm.LLMClient.chat_json", recognize):
            self.store._run(lesson["id"], "vision")
        evidence = lesson["materials"][0]["pages"][0]["formula_evidence"][0]
        self.assertEqual(evidence["pages"], [1])
        self.assertTrue(evidence["needs_review"])
        self.assertTrue(captured[0][-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_dependency_upgrade_retries_and_reinstalls_after_change(self):
        from shared import bootstrap
        root = self.config.DATA_DIR / "bootstrap"
        root.mkdir()
        requirements = root / "requirements.txt"
        requirements.write_text("fixture==1", encoding="utf-8")
        with patch.object(bootstrap.sys, "prefix", str(root)), patch.object(bootstrap.subprocess, "run") as run:
            run.return_value.returncode = 1
            self.assertFalse(bootstrap.ensure(requirements))
            self.assertFalse((root / ".requirements.sha256").exists())
            run.return_value.returncode = 0
            self.assertTrue(bootstrap.ensure(requirements))
            calls = run.call_count
            self.assertTrue(bootstrap.ensure(requirements))
            self.assertEqual(run.call_count, calls)
            requirements.write_text("fixture==2", encoding="utf-8")
            self.assertTrue(bootstrap.ensure(requirements))
            self.assertGreater(run.call_count, calls)

    def test_restore_swap_failure_rolls_back_existing_directory(self):
        lesson = self.lesson()
        with self.client.get("/api/backup") as response:
            backup = response.data
        rename = Path.rename
        def fail_swap(source, destination):
            if source.name.startswith("bt_restore_"):
                raise OSError("fixture rename failure")
            return rename(source, destination)
        with patch.object(Path, "rename", fail_swap):
            response = self.client.post("/api/restore", data={"file": (io.BytesIO(backup), "backup.zip")})
        self.assertEqual(response.status_code, 500)
        self.assertTrue((self.config.LESSON_DIR / lesson["id"] / "lesson.json").is_file())
        self.assertIsNotNone(self.store.get(lesson["id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
