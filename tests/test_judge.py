# -*- coding: utf-8 -*-
"""The judge's settings, its key, and the OpenAI-compatible backend (no network, no keychain).

    python3 -I -m unittest discover -s tests -p 'test_*.py'     (from the repository root)
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ.setdefault("DOCKET_HOME", tempfile.mkdtemp(prefix="docket-unit-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docket import config, daemon, pm  # noqa: E402
from judge import backend, jev, keys, openai_judge, relevance  # noqa: E402

KEY = "sk-test-0123456789abcdefSECRET"


def completion(content: str) -> io.BytesIO:
    return io.BytesIO(json.dumps({"model": "m", "choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 7}}).encode())


class Secrets(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.secrets = self.dir / "secrets.json"
        self.secrets.write_text(json.dumps({"typesafe": {"api_key": "from-secrets"}, "openai": {"api_key": "someone-elses"}}), encoding="utf-8")
        self.keychain = {}
        patcher = mock.patch.object(keys, "read", lambda service, account: self.keychain.get((service, account)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def key(self, settings, env):
        return backend.find_key(backend.resolve(settings, env), env, self.secrets)[:2]

    # Settings: config.json > environment > default.
    def test_settings_config_beats_env_beats_default(self):
        env = {"JEV_MODEL": "jev-env", "DOCKET_JUDGE_ENDPOINT": "https://env.example/v1/"}
        self.assertEqual(backend.resolve({"model": "jev-config"}, env)["model"], "jev-config")
        self.assertEqual(backend.resolve({}, env)["model"], "jev-env")
        self.assertEqual(backend.resolve({}, env)["endpoint"], "https://env.example/v1")
        self.assertEqual(backend.resolve({}, {}), {"backend": "systemone", "model": "jev-1.14.0", "endpoint": "https://api.typesafe.ai/v1/systemone", "keychain_service": "docket.judge"})
        self.assertEqual(backend.resolve({"backend": "nonsense"}, {})["backend"], "systemone")
        # JEV_MODEL names a System One model; it must not leak into the other backend.
        self.assertEqual(backend.resolve({"backend": "openai"}, {"JEV_MODEL": "jev-env"})["model"], "")

    # Key: keychain (named by config.json) > environment > secrets.json > none.
    def test_key_config_keychain_first(self):
        self.keychain[("docket.judge", "systemone")] = "from-keychain"
        self.assertEqual(self.key({}, {"TYPESAFE_API_KEY": "from-env"}), ("from-keychain", "keychain"))

    def test_key_env_second(self):
        self.assertEqual(self.key({}, {"TYPESAFE_API_KEY": "from-env"}), ("from-env", "env"))
        self.assertEqual(self.key({}, {"DOCKET_JUDGE_API_KEY": "generic", "TYPESAFE_API_KEY": "from-env"}), ("generic", "env"))

    def test_key_secrets_third(self):
        self.assertEqual(self.key({}, {}), ("from-secrets", "secrets"))

    def test_key_default_is_none(self):
        self.secrets.write_text("{}", encoding="utf-8")
        self.assertEqual(self.key({}, {}), (None, None))

    def test_keychain_service_comes_from_config(self):
        self.keychain[("my.service", "systemone")] = "custom"
        self.assertEqual(self.key({"keychain_service": "my.service"}, {}), ("custom", "keychain"))

    def test_openai_backend_never_borrows_other_keys(self):
        env = {"TYPESAFE_API_KEY": "typesafe", "OPENAI_API_KEY": "openai"}
        self.assertEqual(self.key({"backend": "openai"}, env), (None, None))
        self.keychain[("docket.judge", "openai")] = "mine"
        self.assertEqual(self.key({"backend": "openai"}, env), ("mine", "keychain"))

    def test_a_failed_keychain_read_falls_back_and_says_why(self):
        def broken(service, account):
            raise keys.KeychainError("reading docket.judge/systemone from the keychain timed out (an access prompt?)")

        with mock.patch.object(keys, "read", broken):
            key, source, problem = backend.find_key(backend.resolve({}, {}), {}, self.secrets)
        self.assertEqual((key, source), ("from-secrets", "secrets"))
        self.assertIn("timed out", problem)


class NeverLogged(unittest.TestCase):
    """The key appears in no log line, no error message and no settings response."""

    def setUp(self):
        self.records = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record.getMessage() + (logging.Formatter().formatException(record.exc_info) if record.exc_info else ""))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        patcher = mock.patch.object(keys, "read", lambda service, account: KEY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(backend.configure, None)

    def leaked(self, *texts):
        for text in list(texts) + self.records:
            self.assertNotIn(KEY, text)
            self.assertNotIn(KEY[:12], text)

    def echo(self, *args, **kwargs):
        body = io.BytesIO(f'{{"error": "Incorrect API key provided: {KEY}"}}'.encode())
        raise urllib.error.HTTPError("https://x/v1/chat/completions", 401, "Unauthorized", {}, body)

    def test_http_errors_that_quote_the_key(self):
        for name in ("systemone", "openai"):
            backend.configure({"backend": name, "model": "m", "endpoint": "https://x/v1"})
            with mock.patch("urllib.request.urlopen", self.echo), self.assertRaises(backend.JudgeError) as caught:
                backend.ask({"s": 1}, {"q": {"type": "noul", "instructions": "i", "criteria": {"true": "t", "false": "f"}}})
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
            self.leaked(str(caught.exception), repr(caught.exception))

    def test_settings_view_and_reload(self):
        with mock.patch.object(config, "PATH", Path(tempfile.mkdtemp()) / "config.json"):
            docket = daemon.Docket()
            view = docket.save_settings({"judge": {"backend": "openai", "model": "m", "endpoint": "https://x/v1"}})
        self.assertTrue(view["judge"]["backends"]["openai"]["configured"])
        self.assertEqual(view["judge"]["backends"]["openai"]["source"], "keychain")
        self.leaked(json.dumps(view))

    def test_a_key_field_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            config.clean({"judge": {"api_key": KEY}})
        self.leaked(str(caught.exception))


class Switch(unittest.TestCase):
    """relevance calls go to the backend that is configured."""

    QUESTION_IDS = ("which", "belongs", "new_work")

    def setUp(self):
        mock.patch.object(keys, "read", lambda service, account: "k").start()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(backend.configure, None)
        self.calls = []

    def fake(self, name):
        def ask(state, questions, **kwargs):
            self.calls.append((name, kwargs))
            return {
                "answers": {
                    "which": {"choice": "d1", "probabilities": {"d1": 0.9, "none": 0.1}, "confidence": 0.9},
                    "belongs": {"noul": 0.9},
                    "new_work": {"noul": 0.1},
                },
                "usage": {"input_tokens": 1},
            }

        return ask

    def run_attribute(self):
        message = {"id": "m1", "role": "user", "time": "11-22 10:00", "text": "那个修好了吗"}
        active = [{"id": "d1", "title": "修卡片", "instruction": "", "brief": "", "rounds": []}]
        return relevance.attribute(message, [], active)[0]

    def test_each_backend_gets_its_own_calls(self):
        mock.patch.object(jev, "ask", self.fake("systemone")).start()
        mock.patch.object(openai_judge, "ask", self.fake("openai")).start()
        backend.configure({})
        self.run_attribute()
        backend.configure({"backend": "openai", "model": "example-chat", "endpoint": "https://llm.example/v1"})
        answer = self.run_attribute()
        self.assertEqual([name for name, _ in self.calls], ["systemone", "openai"])
        self.assertEqual(self.calls[0][1]["endpoint"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(self.calls[1][1], {"model": "example-chat", "endpoint": "https://llm.example/v1", "key": "k"})
        self.assertTrue(relevance.should_move(answer))

    def test_openai_without_endpoint_is_off(self):
        backend.configure({"backend": "openai", "model": "m"})
        self.assertEqual(backend.current.missing(), "no endpoint")
        self.assertFalse(backend.available())


class OpenAIAdapter(unittest.TestCase):
    QUESTIONS = {
        "which": {"type": "choice", "instructions": "i", "criteria": {"d1": "a", "d2": "b", "none": "c"}},
        "belongs": {"type": "noul", "instructions": "i", "criteria": {"true": "t", "false": "f"}},
    }

    def replies(self, *contents):
        sent = []
        queue = list(contents)

        def urlopen(request, timeout=None):
            sent.append(json.loads(request.data))
            return completion(queue.pop(0))

        return sent, urlopen

    def test_answers_map_onto_system_one(self):
        sent, urlopen = self.replies('```json\n{"answers": {"which": {"probabilities": {"d1": 3, "d2": 1}}, "belongs": {"probability": 85}}}\n```')
        with mock.patch("urllib.request.urlopen", urlopen):
            result = openai_judge.ask({"s": 1}, self.QUESTIONS, model="m", endpoint="https://x/v1/", key="k")
        which = result["answers"]["which"]
        self.assertEqual(which["choice"], "d1")
        self.assertAlmostEqual(which["probabilities"]["d1"], 0.75)
        self.assertEqual(which["probabilities"]["none"], 0.0)
        self.assertAlmostEqual(which["confidence"], 0.75)
        self.assertAlmostEqual(result["answers"]["belongs"]["noul"], 0.85)
        self.assertEqual(result["usage"]["input_tokens"], 7)
        self.assertEqual(sent[0]["temperature"], 0)

    def test_a_bad_reply_is_asked_again(self):
        sent, urlopen = self.replies("我觉得属于 d1。", '{"answers": {"which": "d2", "belongs": 0.2}}')
        with mock.patch("urllib.request.urlopen", urlopen):
            result = openai_judge.ask({}, self.QUESTIONS, model="m", endpoint="https://x/v1", key="k")
        self.assertEqual(result["answers"]["which"]["choice"], "d2")
        self.assertEqual(len(sent), 2)
        self.assertIn("could not be used", sent[1]["messages"][-1]["content"])

    def test_three_bad_replies_fail_as_a_judge_error(self):
        _, urlopen = self.replies("no", '{"answers": {}}', '{"answers": {"which": {"probabilities": {}}, "belongs": 0.5}}')
        with mock.patch("urllib.request.urlopen", urlopen), self.assertRaises(backend.JudgeError):
            openai_judge.ask({}, self.QUESTIONS, model="m", endpoint="https://x/v1", key="k")

    def test_url(self):
        self.assertEqual(openai_judge.url("https://llm.example/v1/"), "https://llm.example/v1/chat/completions")
        self.assertEqual(openai_judge.url("https://x/v1/chat/completions"), "https://x/v1/chat/completions")


class ConfigFile(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "config.json"
        patcher = mock.patch.object(config, "PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(backend.configure, None)

    def test_save_keeps_other_keys_and_empty_means_default(self):
        self.path.write_text(json.dumps({"watch": ["agent:main:main"], "pm_model": "old"}), encoding="utf-8")
        loaded = config.save({"pm_model": " ", "pm_fallback_model": "provider/model-b", "judge": {"backend": "openai", "endpoint": "https://x/v1", "model": ""}})
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, {"watch": ["agent:main:main"], "pm_fallback_model": "provider/model-b", "judge": {"backend": "openai", "endpoint": "https://x/v1"}})
        self.assertIsNone(loaded["pm_model"])

    def test_bad_values_are_refused(self):
        for changes in ({"judge": {"backend": "claude"}}, {"judge": {"endpoint": "api.example.com"}}, {"pm_model": 3}):
            with self.assertRaises(ValueError):
                config.save(changes)
        self.assertFalse(self.path.exists())

    def test_daemon_picks_up_a_hand_edit_on_the_next_poll(self):
        with mock.patch.object(keys, "read", lambda s, a: None):
            docket = daemon.Docket()
            self.assertFalse(docket.reload_settings())
            self.path.write_text(json.dumps({"pm_model": "provider/model-b", "judge": {"backend": "openai", "model": "m", "endpoint": "https://x/v1"}}), encoding="utf-8")
            os.utime(self.path, (1, 1))
            self.assertTrue(docket.reload_settings())
        self.assertEqual(pm.MODEL, "provider/model-b")
        self.assertEqual(backend.current.settings["backend"], "openai")
        pm.configure(pm.LOCALE)


if __name__ == "__main__":
    unittest.main()
