from __future__ import annotations

import io
import math
import unittest

from core import minitoml

try:
    import tomllib
except ImportError:  # Python 3.9 / 3.10
    tomllib = None

# The defaults block from docs/contract.md section 2, verbatim.
CONTRACT_DEFAULTS = r"""
timezone = ""                       # IANA name; "" = system local (from /etc/localtime), else "UTC"
projects_dir = "~/.claude/projects"
workspace_roots = []                # e.g. ["~/workspace"]; project = first path part under a root
scan_since_days = 14                # first-run horizon; persisted in state.json as `since`
maybe_active_minutes = 30
review_days = 7
env_file = "~/.ghosted/.env"

[skip]
entrypoints = ["sdk-cli"]           # metadata skip (claude -p / SDK runs)
first_prompt_patterns = []          # regexes on the first human prompt (local only)
path_patterns = []                  # substrings of the transcript path

[detect]
ignore_tail_patterns = ['(?i)anything else\?\s*$', '(?i)does (that|this) (help|make sense)\?\s*$']

[notify]
adapter = "none"                    # none | ntfy | pushover | remote

[notify.ntfy]
server = "https://ntfy.sh"
topic = ""                          # ghosted init writes a random one: "ghosted-" + 32 url-safe chars

[notify.remote]
url = ""                            # https://ghosted.<subdomain>.workers.dev

[push]                              # used by local adapters; the Worker has its own copy in wrangler vars
digest_hour = 9
quiet_start = 22                    # quiet hours [quiet_start, 24) U [0, quiet_end)
quiet_end = 8
daily_budget = 3
"""

CONTRACT_DEFAULTS_EXPECTED = {
    "timezone": "",
    "projects_dir": "~/.claude/projects",
    "workspace_roots": [],
    "scan_since_days": 14,
    "maybe_active_minutes": 30,
    "review_days": 7,
    "env_file": "~/.ghosted/.env",
    "skip": {"entrypoints": ["sdk-cli"], "first_prompt_patterns": [], "path_patterns": []},
    "detect": {"ignore_tail_patterns": ["(?i)anything else\\?\\s*$",
                                        "(?i)does (that|this) (help|make sense)\\?\\s*$"]},
    "notify": {"adapter": "none", "ntfy": {"server": "https://ntfy.sh", "topic": ""},
               "remote": {"url": ""}},
    "push": {"digest_hour": 9, "quiet_start": 22, "quiet_end": 8, "daily_budget": 3},
}

# A personal config: multi-line arrays with comments and trailing commas, regexes in literal
# strings, the remote adapter, an unknown key.
PERSONAL = """\
# Ghosted config
timezone = "America/Chicago"
workspace_roots = [
  "~/workspace",          # everything lives here
  '~/clients',
]
scan_since_days = 21
maybe_active_minutes = 45

[skip]
entrypoints = ["sdk-cli", "headless"]
first_prompt_patterns = [
  '(?i)^run the overnight research',
  '(?i)weekly (finance )?audit',
  '^\\[readout\\]',   # metronome readouts
]
path_patterns = ["/subagents/", "-scratch-"]

[detect]
ignore_tail_patterns = [
    '(?i)anything else\\?\\s*$',
]

[notify]
adapter = "remote"

[notify.remote]
url = "https://ghosted.example.workers.dev"

[push]
digest_hour = 8
daily_budget = 2

[future]
feature_flag = true
"""

PERSONAL_EXPECTED = {
    "timezone": "America/Chicago",
    "workspace_roots": ["~/workspace", "~/clients"],
    "scan_since_days": 21,
    "maybe_active_minutes": 45,
    "skip": {
        "entrypoints": ["sdk-cli", "headless"],
        "first_prompt_patterns": ["(?i)^run the overnight research", "(?i)weekly (finance )?audit",
                                  "^\\[readout\\]"],
        "path_patterns": ["/subagents/", "-scratch-"],
    },
    "detect": {"ignore_tail_patterns": ["(?i)anything else\\?\\s*$"]},
    "notify": {"adapter": "remote", "remote": {"url": "https://ghosted.example.workers.dev"}},
    "push": {"digest_hour": 8, "daily_budget": 2},
    "future": {"feature_flag": True},
}

# Syntax coverage: dotted and quoted keys, escapes, numbers, nested arrays, a sub-table before its
# parent, CRLF line endings and tabs.
SYNTAX = (
    "title = \"Tab\\there, quote \\\" and backslash \\\\ and \\u00e9 \\U0001F600\"\r\n"
    "literal = 'C:\\Users\\nobody # not a comment'\r\n"
    "notify.adapter = \"ntfy\"   # dotted key at the root\r\n"
    "\"quoted key\" = 1\r\n"
    "'literal key' = -2\r\n"
    "site.\"sub key\".deep = false\r\n"
    "\tindented = +3\r\n"
    "big = 1_000_000\r\n"
    "hex = 0xDEAD_beef\r\n"
    "octal = 0o755\r\n"
    "binary = 0b1010\r\n"
    "pi = 3.141_59\r\n"
    "exp = 6.02e23\r\n"
    "neg_exp = -1.5E-3\r\n"
    "zero_float = 0.0\r\n"
    "nested = [[1, 2], [\"a\", 'b'], [], [[true]]]\r\n"
    "mixed = [ 1, \"two\", 3.0, false, ]\r\n"
    "empty_string = \"\"\r\n"
    "\r\n"
    "[tables.child]\r\n"
    "x = 1\r\n"
    "[tables]\r\n"
    "y = 2\r\n"
    "[ spaced . header ]  # comment after a header\r\n"
    "ok = true\r\n"
)

SYNTAX_EXPECTED = {
    "title": "Tab\there, quote \" and backslash \\ and \u00e9 \U0001F600",
    "literal": "C:\\Users\\nobody # not a comment",
    "notify": {"adapter": "ntfy"},
    "quoted key": 1,
    "literal key": -2,
    "site": {"sub key": {"deep": False}},
    "indented": 3,
    "big": 1000000,
    "hex": 0xDEADBEEF,
    "octal": 0o755,
    "binary": 10,
    "pi": 3.14159,
    "exp": 6.02e23,
    "neg_exp": -1.5e-3,
    "zero_float": 0.0,
    "nested": [[1, 2], ["a", "b"], [], [[True]]],
    "mixed": [1, "two", 3.0, False],
    "empty_string": "",
    "tables": {"child": {"x": 1}, "y": 2},
    "spaced": {"header": {"ok": True}},
}

EMPTY_DOCS = ["", "\n\n", "# only a comment\n", "   \t\n# another\n\n"]

INVALID_DOCS = {
    "duplicate key": "a = 1\na = 2\n",
    "unterminated string": 'a = "never closed\n',
    "invalid escape": 'a = "bad \\q escape"\n',
    "table twice": "[t]\nx = 1\n[t]\ny = 2\n",
    "missing equals": "a 1\n",
    "junk after value": "a = 1 b = 2\n",
    "unterminated array": "a = [1, 2\n",
    "leading zero": "a = 01\n",
    "value then table": "a = 1\n[a]\nb = 2\n",
    "table then sub-table over a value": "[a]\nb = 1\n[a.b]\nc = 2\n",
    "newline in string": 'a = "line\nbreak"\n',
    "missing value": "a =\n",
    "bad bare word": "a = maybe\n",
    "double comma": "a = [1,,2]\n",
    "dotted key into a header table": "[a.b]\nx = 1\n[a]\nb.y = 2\n",
    "header over a dotted table": "a.b = 1\n[a]\nc = 2\n",
    "control char in comment": "a = 1 # bell \x07\n",
}

UNSUPPORTED_DOCS = {
    "inline table": "a = {b = 1}\n",
    "multi-line basic string": 'a = """x"""\n',
    "multi-line literal string": "a = '''x'''\n",
    "array of tables": "[[servers]]\nname = 'x'\n",
    "date": "when = 2026-10-05\n",
    "datetime": "when = 2026-10-05T09:00:00Z\n",
    "time": "when = 09:00:00\n",
}

PARITY_DOCS = [CONTRACT_DEFAULTS, PERSONAL, SYNTAX] + EMPTY_DOCS


class TestDirect(unittest.TestCase):
    """Expected values, checked on every Python (including 3.9 without tomllib)."""

    def test_contract_defaults(self):
        self.assertEqual(minitoml.loads(CONTRACT_DEFAULTS), CONTRACT_DEFAULTS_EXPECTED)

    def test_personal_config(self):
        self.assertEqual(minitoml.loads(PERSONAL), PERSONAL_EXPECTED)

    def test_syntax_coverage(self):
        self.assertEqual(minitoml.loads(SYNTAX), SYNTAX_EXPECTED)

    def test_types(self):
        doc = minitoml.loads("i = 7\nf = 7.5\nb = true\ns = 's'\nl = [1]\n")
        self.assertIs(type(doc["i"]), int)
        self.assertIs(type(doc["f"]), float)
        self.assertIs(type(doc["b"]), bool)
        self.assertIs(type(doc["s"]), str)
        self.assertIs(type(doc["l"]), list)

    def test_empty_documents(self):
        for doc in EMPTY_DOCS:
            with self.subTest(doc=doc):
                self.assertEqual(minitoml.loads(doc), {})

    def test_special_floats(self):
        doc = minitoml.loads("a = inf\nb = -inf\nc = +inf\nd = nan\ne = -nan\n")
        self.assertTrue(math.isinf(doc["a"]) and doc["a"] > 0)
        self.assertTrue(math.isinf(doc["b"]) and doc["b"] < 0)
        self.assertTrue(math.isinf(doc["c"]) and doc["c"] > 0)
        self.assertTrue(math.isnan(doc["d"]) and math.isnan(doc["e"]))

    def test_dotted_keys_share_a_table(self):
        doc = minitoml.loads("[notify]\nntfy.server = 'a'\nntfy.topic = 'b'\nadapter = 'ntfy'\n")
        self.assertEqual(doc, {"notify": {"ntfy": {"server": "a", "topic": "b"}, "adapter": "ntfy"}})

    def test_sub_table_of_dotted_table_allowed(self):
        doc = minitoml.loads("[fruit]\napple.color = 'red'\n[fruit.apple.texture]\nsmooth = true\n")
        self.assertEqual(doc, {"fruit": {"apple": {"color": "red", "texture": {"smooth": True}}}})

    def test_dotted_key_into_implicit_header_table(self):
        doc = minitoml.loads("[a.b.c]\nx = 1\n[a]\nb.d = 2\n")
        self.assertEqual(doc, {"a": {"b": {"c": {"x": 1}, "d": 2}}})

    def test_load_binary_file(self):
        self.assertEqual(minitoml.load(io.BytesIO(b"a = 'x'\n")), {"a": "x"})
        with self.assertRaises(TypeError):
            minitoml.load(io.StringIO("a = 1\n"))

    def test_invalid_documents_raise(self):
        for name, doc in INVALID_DOCS.items():
            with self.subTest(name=name):
                with self.assertRaises(minitoml.TOMLDecodeError):
                    minitoml.loads(doc)

    def test_unsupported_features_say_so(self):
        for name, doc in UNSUPPORTED_DOCS.items():
            with self.subTest(name=name):
                with self.assertRaises(minitoml.TOMLDecodeError) as ctx:
                    minitoml.loads(doc)
                self.assertIn("not supported", str(ctx.exception))

    def test_error_names_the_line_not_the_content(self):
        with self.assertRaises(minitoml.TOMLDecodeError) as ctx:
            minitoml.loads("a = 1\n\nsecret = \"sk-unterminated\n")
        message = str(ctx.exception)
        self.assertIn("line 3", message)
        self.assertNotIn("sk-unterminated", message)

    def test_decode_error_is_a_value_error(self):
        self.assertTrue(issubclass(minitoml.TOMLDecodeError, ValueError))


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
class TestParityWithTomllib(unittest.TestCase):
    def test_same_result_as_tomllib(self):
        for doc in PARITY_DOCS:
            with self.subTest(doc=doc[:40]):
                self.assertEqual(minitoml.loads(doc), tomllib.loads(doc))

    def test_tomllib_also_rejects_invalid_documents(self):
        for name, doc in INVALID_DOCS.items():
            with self.subTest(name=name):
                with self.assertRaises(tomllib.TOMLDecodeError):
                    tomllib.loads(doc)

    def test_unsupported_documents_are_valid_toml(self):
        # minitoml refuses these on purpose; tomllib must accept them (they are real TOML).
        for name, doc in UNSUPPORTED_DOCS.items():
            with self.subTest(name=name):
                tomllib.loads(doc)


if __name__ == "__main__":
    unittest.main()
