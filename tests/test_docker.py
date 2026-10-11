import unittest

from srvr.core.docker import Container, parse_ps, summarize


def container(component: str, state: str, status: str) -> Container:
    return Container("p", component, f"p-{component}-1", state, status, "abc")


class DockerTest(unittest.TestCase):
    def test_parse_ps(self) -> None:
        line = "vocab\tapi\tvocab-api-1\trunning\tUp 2 hours (healthy)\t1a2b3c"
        (parsed,) = parse_ps(line + "\nnoise\n")
        self.assertEqual(
            (parsed.project, parsed.component, parsed.health), ("vocab", "api", "healthy")
        )

    def test_summarize(self) -> None:
        up = container("a", "running", "Up 1 hour")
        healthy = container("a", "running", "Up 1 hour (healthy)")
        sick = container("b", "running", "Up 1 hour (unhealthy)")
        down = container("b", "exited", "Exited (0) 1 hour ago")
        self.assertEqual(summarize([]), "absent")
        self.assertEqual(summarize([healthy]), "healthy")
        self.assertEqual(summarize([up, healthy]), "running")
        self.assertEqual(summarize([healthy, sick]), "unhealthy")
        self.assertEqual(summarize([healthy, down]), "1/2 running")
        self.assertEqual(summarize([down]), "stopped")
