from codecs import BOM_UTF16_LE
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from install import SOURCE, install, link_type


class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="impact tree & ")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.claude = self.home / ".claude"
        self.codex = self.home / ".codex"
        environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_links_both_agents_and_replaces_instructions_once(self):
        self.codex.mkdir()
        instructions = self.codex / "AGENTS.md"
        original = "# User settings\r\n\r\n<!-- change-tree:begin -->\r\nOld\r\n<!-- change-tree:end -->\r\n"
        instructions.write_bytes(BOM_UTF16_LE + original.encode("utf-16-le"))

        install("both", True, self.home, SOURCE)
        installed = instructions.read_bytes()
        install("both", True, self.home, SOURCE)

        expected_type = "junction" if os.name == "nt" else "symlink"
        for path in (self.claude / "skills" / "impact-tree", self.home / ".agents" / "skills" / "impact-tree"):
            self.assertEqual(link_type(path), expected_type)
            self.assertEqual(path.resolve(), SOURCE)
        self.assertEqual(instructions.read_bytes(), installed)
        self.assertTrue(installed.startswith(BOM_UTF16_LE))
        text = installed.decode("utf-16")
        self.assertTrue(text.startswith("# User settings\r\n\r\n"))
        self.assertEqual(text.count("<!-- impact-tree:begin -->"), 1)
        self.assertNotIn("Old", text)

    def test_conflict_leaves_the_other_agent_untouched(self):
        existing = self.home / ".agents" / "skills" / "impact-tree"
        existing.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "already exists"):
            install("both", True, self.home, SOURCE)
        self.assertFalse(self.claude.exists())
