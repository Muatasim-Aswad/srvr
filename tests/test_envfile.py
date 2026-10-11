import unittest

from srvr.core import envfile


class EnvFileTest(unittest.TestCase):
    def test_parse(self) -> None:
        text = '# comment\n\nA=1\nexport B="two words"\nC=\nbroken line\nD=x=y\n'
        self.assertEqual(
            envfile.parse(text), [("A", "1"), ("B", "two words"), ("C", ""), ("D", "x=y")]
        )

    def test_secrets_are_hidden(self) -> None:
        hidden = "(set, hidden)"
        self.assertEqual(envfile.display_value("VOCAB_API_TOKEN", "abc"), hidden)
        self.assertEqual(envfile.display_value("GEMINI_API_KEY", "abc"), hidden)
        self.assertEqual(envfile.display_value("DB_URL", "postgres://u:p@host/db"), hidden)
        self.assertEqual(envfile.display_value("VOCAB_PORT", "3000"), "3000")
        self.assertEqual(envfile.display_value("VOCAB_API_TOKEN", ""), "(empty)")
