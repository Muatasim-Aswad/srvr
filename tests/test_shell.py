import unittest
from unittest import mock

from srvr.core.shell import Shell


class ShellTest(unittest.TestCase):
    def test_sudo_only_when_not_already_root(self) -> None:
        with mock.patch("os.geteuid", return_value=1000):
            self.assertEqual(Shell._command(["tar"], sudo=True), ["sudo", "--", "tar"])
            self.assertEqual(Shell._command(["tar"], sudo=False), ["tar"])
        with mock.patch("os.geteuid", return_value=0):
            self.assertEqual(Shell._command(["tar"], sudo=True), ["tar"])

    def test_pipeline_reaps_the_producer_when_the_consumer_cannot_start(self) -> None:
        import subprocess
        import sys

        from srvr.core.errors import SrvrError

        children = []
        spawn = subprocess.Popen

        def record_child(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            return child

        def cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()

        self.addCleanup(cleanup)
        with (
            mock.patch("subprocess.Popen", side_effect=record_child),
            self.assertRaisesRegex(SrvrError, "command not found"),
        ):
            Shell().pipeline(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                ["/nonexistent-srvr-test-command"],
            )
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_interrupted_pipeline_reaps_both_children(self) -> None:
        import subprocess
        import sys

        children = []
        spawn = subprocess.Popen

        def record_child(*args, **kwargs):
            child = spawn(*args, **kwargs)
            children.append(child)
            if len(children) == 2:
                wait = child.wait
                calls = []

                def interrupted_wait(*args, **kwargs):
                    if not calls:
                        calls.append(True)
                        raise KeyboardInterrupt
                    return wait(*args, **kwargs)

                child.wait = interrupted_wait
            return child

        def cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()

        self.addCleanup(cleanup)
        command = [sys.executable, "-c", "import time; time.sleep(60)"]
        with (
            mock.patch("subprocess.Popen", side_effect=record_child),
            self.assertRaises(KeyboardInterrupt),
        ):
            Shell().pipeline(command, command)
        self.assertEqual(len(children), 2)
        self.assertTrue(all(child.poll() is not None for child in children))
