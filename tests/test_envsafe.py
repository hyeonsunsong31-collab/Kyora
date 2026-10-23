"""kyora.util.envsafe: the allowlisted child environment and the redaction of registered secrets.

Run: ``python3 -m unittest tests.test_envsafe -v``
"""
from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

from kyora.util import envsafe
from kyora.util.envsafe import is_secret_name, redact, register_secret, safe_env

HOST_ENV = {
    # what a shell, Git Bash, node and npm need: passes
    "PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": "/tmp", "PWD": "/w",
    "SYSTEMROOT": r"C:\Windows", "ProgramFiles(x86)": r"C:\Program Files (x86)", "MSYSTEM": "MINGW64",
    "APPDATA": r"C:\Users\u\AppData\Roaming", "PREFIX": "/data/data/com.termux/files/usr",
    "TERMUX_VERSION": "0.118", "ANDROID_ROOT": "/system", "NODE_PATH": "/usr/lib/node_modules", "CI": "true",
    "LD_PRELOAD": "/data/data/com.termux/files/usr/lib/libtermux-exec.so", "https_proxy": "http://proxy:3128",
    "NODE_EXTRA_CA_CERTS": "/etc/ssl/corp.pem", "HTTP_PROXY": "http://me:hunter2pw@proxy:3128",
    # secrets the old denylist let through, and allowlisted-looking names that are secrets: dropped
    "OPENAI_KEY": "sk-proj-0123456789", "NVIDIA_KEY": "nvapi-0123456789", "NGC_KEY": "ngc0123456789",
    "GITHUB_PAT": "github_pat_0123456789", "HF_AUTH": "hf_0123456789", "NPM_CONFIG__AUTH": "dXNlcjpwYXNz",
    "DATABASE_URL": "postgres://app:hunter2hunter2@db:5432/app", "SSH_AUTH_SOCK": "/tmp/ssh-x/agent.1",
    "KYORA_CONFIG_DIR": "/home/u/.kyora/config", "KYORA_HOME": "/home/u/.kyora", "TERMUX_API_KEY": "abc123456789",
    "LC_TOKEN": "t0123456789", "ANTHROPIC_API_KEY": "sk-ant-0123456789", "AWS_SECRET_ACCESS_KEY": "a" * 20,
    "SOMETHING_ELSE": "1",
}


class SafeEnvTests(unittest.TestCase):
    def test_allowlist_keeps_what_tools_need_and_drops_secrets(self):
        env = safe_env(environ=HOST_ENV)
        for name in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PWD", "SYSTEMROOT", "ProgramFiles(x86)", "MSYSTEM",
                     "APPDATA", "PREFIX", "TERMUX_VERSION", "ANDROID_ROOT", "NODE_PATH", "CI", "LD_PRELOAD", "https_proxy",
                     "NODE_EXTRA_CA_CERTS"):
            self.assertEqual(env.get(name), HOST_ENV[name], name)
        for name in ("OPENAI_KEY", "NVIDIA_KEY", "NGC_KEY", "GITHUB_PAT", "HF_AUTH", "NPM_CONFIG__AUTH", "DATABASE_URL",
                     "SSH_AUTH_SOCK", "KYORA_CONFIG_DIR", "KYORA_HOME", "TERMUX_API_KEY", "LC_TOKEN",
                     "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "SOMETHING_ELSE", "HTTP_PROXY"):
            self.assertNotIn(name, env)

    def test_names_compare_case_insensitively_like_windows(self):
        env = safe_env(environ={"SystemRoot": r"C:\Windows", "Path": r"C:\bin", "ComSpec": "cmd.exe"})
        self.assertEqual(set(env), {"SystemRoot", "Path", "ComSpec"})

    def test_ld_preload_only_for_termux_exec(self):
        self.assertNotIn("LD_PRELOAD", safe_env(environ={"LD_PRELOAD": "/tmp/evil.so"}))

    def test_credential_values_are_dropped_whatever_the_name(self):
        env = safe_env(environ={"NODE_PATH": "https://u:pw0123@host/x", "TERM": "xterm"},
                       extra={"KYORA_X": "postgres://a:b0123@h/db", "KYORA_Y": "ghp_0123456789abcdef"})
        self.assertEqual(env, {"TERM": "xterm"})

    def test_extras(self):
        env = safe_env({"KYORA_RUN": "r1", "PATH": None, "EXTRA_TOKEN": "z", "FORCE_COLOR": 0},
                       environ={"PATH": "/bin", "HOME": "/h"})
        self.assertEqual(env, {"HOME": "/h", "KYORA_RUN": "r1", "FORCE_COLOR": "0"})

    def test_secret_names(self):
        for name in ("OPENAI_KEY", "API_KEY", "MY_APIKEY", "GITHUB_PAT", "HF_AUTH", "OAUTH_CLIENT", "SSH_AUTH_SOCK",
                     "NPM_CONFIG__AUTH", "DB_PASS", "DB_PASSWORD", "GH_TOKEN", "SENTRY_DSN", "SLACK_WEBHOOK_URL",
                     "AZURE_CONNECTION_STRING", "KEY", "KEYS_FILE", "SESSION_COOKIE", "SIGNING_PRIVATE_KEY"):
            self.assertTrue(is_secret_name(name), name)
        for name in ("PATH", "NODE_PATH", "PATHEXT", "PWD", "OLDPWD", "XAUTHORITY", "GIT_AUTHOR_NAME", "KEYBOARD",
                     "SSH_ASKPASS", "HOME", "TERM", "LANG"):
            self.assertFalse(is_secret_name(name), name)


class RedactTests(unittest.TestCase):
    def setUp(self):
        envsafe.clear_secrets()
        self.addCleanup(envsafe.clear_secrets)

    def test_registered_values_are_replaced_by_their_label(self):
        register_secret("nvidia.kimi.api_key", " nvapi-kimi-0123456789\n")
        register_secret("short", "1234567")  # under 8 characters: never redacted (it would hit ordinary text)
        register_secret("none", None)
        text = "Authorization: Bearer nvapi-kimi-0123456789 (1234567)"
        self.assertEqual(redact(text), "Authorization: Bearer [redacted nvidia.kimi.api_key] (1234567)")

    def test_longest_value_first(self):
        register_secret("short", "abcdefgh-0123456789")
        register_secret("long", "abcdefgh-0123456789-ijkl")
        self.assertEqual(redact("abcdefgh-0123456789-ijkl abcdefgh-0123456789"), "[redacted long] [redacted short]")

    def test_placeholders_and_short_words_are_not_registered(self):
        # local servers take "password" or "not-needed"; replacing those words would mangle a game's own code
        for value in ("password", "not-needed", "sk-no-key-required", "changeme", "hunter22"):
            register_secret("local.api_key", value)
        register_secret("kimi", "nvapi-abc12345")  # short, but a known credential prefix
        self.assertEqual(redact("password not-needed sk-no-key-required changeme hunter22 nvapi-abc12345"),
                         "password not-needed sk-no-key-required changeme hunter22 [redacted kimi]")

    def test_secret_looking_env_values_of_this_process(self):
        with mock.patch.dict(os.environ, {"MY_SERVICE_TOKEN": "tok-zz-0123456789",
                                          "DATABASE_URL": "postgres://app:hunter2hunter2@db/app",
                                          "PLAIN_SETTING": "visible-0123456789"}):
            out = redact("a tok-zz-0123456789 b postgres://app:hunter2hunter2@db/app c visible-0123456789")
        self.assertEqual(out, "a [redacted MY_SERVICE_TOKEN] b [redacted DATABASE_URL] c visible-0123456789")

    def test_non_text_and_no_secrets(self):
        self.assertIsNone(redact(None))
        self.assertEqual(redact("nothing to hide here"), "nothing to hide here")

    def test_thread_safe(self):
        errors: list[BaseException] = []

        def work(i: int) -> None:
            try:
                for j in range(50):
                    register_secret(f"k{i}", f"secret-{i}-{j:04d}-value")
                    self.assertNotIn(f"secret-{i}-{j:04d}-value", redact(f"x secret-{i}-{j:04d}-value y"))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
