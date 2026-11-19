#!/usr/bin/env python3
# Run it with: python3 scripts/coverage.py [--min PERCENT]
"""Line coverage of src/er by the unit tests, using only the standard library.

Runs the same tests as `python3 -m unittest discover -s tests` with a trace
function installed, and prints for each file in src/er how many of its lines
ran and which did not. Exits non-zero if a test fails or if the total is
below --min. Writes nothing: no data file, no report, no bytecode.

A line counts once any test runs it, whether or not a test would notice it
being wrong, so read a high number as "nothing is unvisited" and no more.
"""
from __future__ import annotations

import argparse
import ast
import dis
import io
import os
import sys
import threading
import unittest

sys.dont_write_bytecode = True

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def executable_lines(path):
    """The lines of a file that can run: the first line of each statement the
    compiler made code for. Docstrings are left out; they are not behaviour."""
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    compiled, pending = set(), [compile(source, path, "exec")]
    while pending:
        code = pending.pop()
        compiled.update(line for _, line in dis.findlinestarts(code) if line)
        pending.extend(const for const in code.co_consts if hasattr(const, "co_code"))
    statements = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.stmt, ast.ExceptHandler)):
            statements.add(node.lineno)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                compiled.difference_update(range(first.lineno, first.end_lineno + 1))
    return compiled & statements


def ranges(numbers):
    """[3, 4, 5, 9] as ["3-5", "9"]."""
    runs = []
    for number in sorted(numbers):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return ["%d" % low if low == high else "%d-%d" % (low, high) for low, high in runs]


def run_tests(targets, pattern):
    """Run the tests under a trace function. Returns the unittest result, what
    the tests printed, and {path: lines that ran} for each target."""
    hits = {path: set() for path in targets}
    real = {}

    def resolve(filename):
        if filename not in real:
            real[filename] = os.path.realpath(filename)
        return real[filename]

    def on_line(frame, event, arg):
        if event == "line":
            hits[resolve(frame.f_code.co_filename)].add(frame.f_lineno)
        return on_line

    def on_call(frame, event, arg):
        # Returning nothing for other files keeps their lines untraced, which
        # is most of the speed.
        path = resolve(frame.f_code.co_filename)
        if path in hits:
            hits[path].add(frame.f_lineno)
            return on_line
        return None

    tests = os.path.join(REPO, "tests")
    stream = io.StringIO()
    threading.settrace(on_call)
    sys.settrace(on_call)
    try:
        suite = unittest.defaultTestLoader.discover(tests, pattern=pattern, top_level_dir=tests)
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    finally:
        sys.settrace(None)
        threading.settrace(None)
    return result, stream.getvalue(), hits


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min", type=float, default=0.0, metavar="PERCENT",
                        help="fail if the total for src/er is below this (default: 0)")
    parser.add_argument("--pattern", default="test*.py",
                        help="which test files to run (default: %(default)s)")
    args = parser.parse_args(argv)

    package = os.path.join(REPO, "src", "er")
    targets = sorted(os.path.join(package, name)
                     for name in os.listdir(package) if name.endswith(".py"))
    result, output, hits = run_tests(targets, args.pattern)

    print("python %s: %d tests, %d failed, %d skipped" % (
        sys.version.split()[0], result.testsRun,
        len(result.failures) + len(result.errors), len(result.skipped)))
    print("\n%-28s %6s %6s %7s  %s" % ("file", "lines", "missed", "cover", "not run"))
    total = ran = 0
    for path in targets:
        lines = executable_lines(path)
        missed = lines - hits[path]
        total += len(lines)
        ran += len(lines) - len(missed)
        percent = 100.0 * (len(lines) - len(missed)) / len(lines) if lines else 100.0
        print("%-28s %6d %6d %6.1f%%  %s" % (
            os.path.relpath(path, REPO), len(lines), len(missed), percent,
            ", ".join(ranges(missed))))
    percent = 100.0 * ran / total if total else 100.0
    print("%-28s %6d %6d %6.1f%%" % ("TOTAL", total, total - ran, percent))

    if not result.wasSuccessful():
        print("\n" + output)
        print("the tests did not pass, so the numbers above are not to be trusted")
        return 1
    if percent < args.min:
        print("\ncoverage %.1f%% is below the minimum of %.1f%%" % (percent, args.min))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
