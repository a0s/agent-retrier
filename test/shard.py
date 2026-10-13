#!/usr/bin/env python3
"""One slice of one test file. `./test/run.sh` starts these; nothing else needs
to, except to run again the slice it says failed.

    shard.py K N FILE

runs every test in FILE whose place in the file's sorted list of tests leaves
remainder K when divided by N — so the N slices, K = 0..N-1, run every test
exactly once between them.

The slice is cut from the suite unittest itself loads, by counting, not from
a list of class names somebody has to keep up to date: a class or a test added
tomorrow lands in one of the slices without anyone doing anything, and cannot
be left out of all of them. The last line printed says how many tests this
slice ran out of how many the file holds; run.sh adds those up and refuses a
total that does not come out, which is what a slice that died without a word
would otherwise look like — fewer tests, all of them passing.

Tests are dealt out one at a time rather than a class at a time because the
slow ones sit together: thirteen restarts in one class, eighteen in another.

A slice is always the same tests in the same order, whoever runs it and
whatever else is running: what failed in a full run fails again on its own.
And each is a process of its own, as each file always was. `helper.load()`
rewrites os.environ, and a test has only ever had to live with what the tests
of its own file left there.
"""
import importlib
import os
import sys
import time
import unittest


def tests_in(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for test in tests_in(item):
                yield test
        else:
            yield item


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(__doc__)
        return 2
    k, n, name = int(argv[0]), int(argv[1]), argv[2]
    if n < 1 or not 0 <= k < n:
        sys.stderr.write("shard.py: there is no slice %d of %d\n" % (k, n))
        return 2
    module = importlib.import_module(os.path.splitext(os.path.basename(name))[0])
    # By id, so the order is the same in every slice whatever order the loader
    # happened to walk the module in: the slices only add up to the whole file
    # if all N of them are counting down the same list.
    every = sorted(tests_in(unittest.defaultTestLoader.loadTestsFromModule(module)),
                   key=lambda test: test.id())
    mine = [test for i, test in enumerate(every) if i % n == k]
    started = time.time()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(mine))
    sys.stderr.flush()
    # result.testsRun, not len(mine): what is reported is what happened.
    print("shard: ran %d of %d in %.1fs"
          % (result.testsRun, len(every), time.time() - started))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
