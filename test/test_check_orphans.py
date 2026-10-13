"""check-orphans.py's own logic — the piece run.sh's orphan gate depends on
(T26 AC: an artificially left orphan makes run.sh print FAILURES with its
pid). It reads real `/proc`, so the platform it is authoritative on (Linux) is
faked here rather than required to run this file at all.
"""
import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("check_orphans", os.path.join(ROOT, "check-orphans.py"))
check_orphans = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_orphans)


class FakeProc:
    """A `/proc` with a handful of pids, some carrying CR_CLAUDE_ARGV."""
    def __init__(self, environs):
        self.environs = environs   # {pid: bytes or None (unreadable)}

    def isdir(self, path):
        return path == "/proc"

    def listdir(self, path):
        return [str(p) for p in self.environs]

    def open_environ(self, path, mode):
        pid = int(path.split("/")[2])
        data = self.environs.get(pid)
        if data is None:
            raise PermissionError(pid)
        return io.BytesIO(data)


class TestFindOrphans(unittest.TestCase):
    def test_no_proc_means_not_checkable(self):
        with mock.patch.object(check_orphans.os.path, "isdir", return_value=False):
            self.assertIsNone(check_orphans.find_orphans())

    def test_clean_system_has_no_orphans(self):
        fake = FakeProc({111: b"HOME=/root\x00PATH=/bin\x00"})
        with mock.patch.object(check_orphans.os.path, "isdir", fake.isdir), \
             mock.patch.object(check_orphans.os, "listdir", fake.listdir), \
             mock.patch("builtins.open", fake.open_environ), \
             mock.patch.object(check_orphans.os, "getpid", return_value=999):
            self.assertEqual(check_orphans.find_orphans(), [])

    def test_a_left_over_wrapper_is_reported_by_pid(self):
        fake = FakeProc({111: b"HOME=/root\x00",
                         222: b"CR_CLAUDE_ARGV=/usr/bin/claude\x1f\x00PATH=/bin\x00",
                         999: b"CR_CLAUDE_ARGV=should-be-excluded\x00"})  # our own pid
        with mock.patch.object(check_orphans.os.path, "isdir", fake.isdir), \
             mock.patch.object(check_orphans.os, "listdir", fake.listdir), \
             mock.patch("builtins.open", fake.open_environ), \
             mock.patch.object(check_orphans.os, "getpid", return_value=999):
            self.assertEqual(check_orphans.find_orphans(), [222])

    def test_unreadable_environ_is_skipped_not_fatal(self):
        fake = FakeProc({111: None, 222: b"CR_CLAUDE_ARGV=x\x00"})
        with mock.patch.object(check_orphans.os.path, "isdir", fake.isdir), \
             mock.patch.object(check_orphans.os, "listdir", fake.listdir), \
             mock.patch("builtins.open", fake.open_environ), \
             mock.patch.object(check_orphans.os, "getpid", return_value=999):
            self.assertEqual(check_orphans.find_orphans(), [222])

    def test_main_prints_failures_and_exits_nonzero_for_an_orphan(self):
        fake = FakeProc({222: b"CR_CLAUDE_ARGV=x\x00"})
        with mock.patch.object(check_orphans.os.path, "isdir", fake.isdir), \
             mock.patch.object(check_orphans.os, "listdir", fake.listdir), \
             mock.patch("builtins.open", fake.open_environ), \
             mock.patch.object(check_orphans.os, "getpid", return_value=999):
            self.assertEqual(check_orphans.main(), 1)

    def test_a_process_on_its_way_out_is_given_the_time_to_go(self):
        # There at the first look, gone by the second: a fake that had been
        # hung up on and had not finished dying. Not an orphan.
        fake = FakeProc({222: b"CR_CLAUDE_ARGV=x\x00"})
        looks = []

        def listdir(path):
            looks.append(path)
            return fake.listdir(path) if len(looks) == 1 else []

        with mock.patch.object(check_orphans.os.path, "isdir", fake.isdir), \
             mock.patch.object(check_orphans.os, "listdir", listdir), \
             mock.patch("builtins.open", fake.open_environ), \
             mock.patch.object(check_orphans.os, "getpid", return_value=999), \
             contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(check_orphans.main(settle=5), 0)
        self.assertEqual(len(looks), 2)
        self.assertIn("clean", said.getvalue())

    def test_one_that_stays_is_still_an_orphan_when_the_time_is_up(self):
        fake = FakeProc({222: b"CR_CLAUDE_ARGV=x\x00"})
        with mock.patch.object(check_orphans.os.path, "isdir", fake.isdir), \
             mock.patch.object(check_orphans.os, "listdir", fake.listdir), \
             mock.patch("builtins.open", fake.open_environ), \
             mock.patch.object(check_orphans.os, "getpid", return_value=999), \
             contextlib.redirect_stdout(io.StringIO()) as said:
            self.assertEqual(check_orphans.main(settle=0.3), 1)
        self.assertIn("[222]", said.getvalue())

    def test_main_is_clean_exit_when_nothing_found(self):
        fake = FakeProc({})
        with mock.patch.object(check_orphans.os.path, "isdir", fake.isdir), \
             mock.patch.object(check_orphans.os, "listdir", fake.listdir), \
             mock.patch.object(check_orphans.os, "getpid", return_value=999):
            self.assertEqual(check_orphans.main(), 0)


if __name__ == "__main__":
    unittest.main()
