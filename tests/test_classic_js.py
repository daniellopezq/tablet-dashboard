"""The tablet client (web/js/*.js): plain ES5 scripts that share globals, loaded in order by web/index.html."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "web", "js")
# Chrome 50 has no arrow functions in every context we need, no let/const in sloppy code, no template
# literals, classes, for-of, spread or default parameters we can rely on: keep the tablet code ES5.
ES6 = [
    (re.compile(r"^\s*(let|const|class)\s"), "let/const/class"),
    (re.compile(r"=>"), "arrow function"),
    (re.compile(r"`"), "template literal"),
    (re.compile(r"\bfor\s*\([^)]*\sof\s"), "for...of"),
    (re.compile(r"\.\.\.[A-Za-z_$\[]"), "spread"),
    (re.compile(r"\b(async|await)\s"), "async/await"),
]
TOP_LEVEL = re.compile(r"^(?:function\s+([A-Za-z_$][\w$]*)|var\s+([A-Za-z_$][\w$]*))")


def scripts():
    with open(os.path.join(ROOT, "web", "index.html")) as f:
        return re.findall(r'<script src="js/([^"]+)"></script>', f.read())


class ClassicJsTests(unittest.TestCase):
    def test_index_loads_every_file_core_first_main_last(self):
        loaded = scripts()
        self.assertEqual(sorted(loaded), sorted(n for n in os.listdir(JS_DIR) if n.endswith(".js")))
        self.assertEqual((loaded[0], loaded[-1]), ("core.js", "main.js"))
        self.assertEqual(len(loaded), len(set(loaded)))

    def test_es5_only(self):
        for name in scripts():
            with open(os.path.join(JS_DIR, name)) as f:
                for no, line in enumerate(f, 1):
                    code = re.sub(r"'(?:[^'\\]|\\.)*'|//.*$", "", line)  # ignore strings and line comments
                    for pattern, what in ES6:
                        self.assertIsNone(pattern.search(code), "%s:%d uses %s" % (name, no, what))

    def test_top_level_names_are_unique(self):
        # The files share one global scope: a second declaration would silently replace the first.
        seen = {}
        for name in scripts():
            with open(os.path.join(JS_DIR, name)) as f:
                for line in f:
                    m = TOP_LEVEL.match(line)
                    if m:
                        ident = m.group(1) or m.group(2)
                        self.assertNotIn(ident, seen, "%s declared in %s and %s" % (ident, seen.get(ident), name))
                        seen[ident] = name
        self.assertGreater(len(seen), 150)


if __name__ == "__main__":
    unittest.main()
