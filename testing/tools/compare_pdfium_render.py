#!/usr/bin/env python3

"""Compare two pdfium_test builds for exact render fidelity and wall time."""

import argparse
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


PDFIUM_TEST = "pdfium_test"
MD5_RE = re.compile(r"^MD5:(.+):([0-9a-fA-F]{32})$")


class RenderFailure(RuntimeError):
  pass


def _resolve_build_dir(build_dir):
  build_path = pathlib.Path(build_dir).expanduser().resolve()
  pdfium_test = build_path / PDFIUM_TEST
  if not pdfium_test.exists():
    raise RenderFailure(f"missing {PDFIUM_TEST}: {pdfium_test}")
  if not os.access(pdfium_test, os.X_OK):
    raise RenderFailure(f"{pdfium_test} is not executable")
  return pdfium_test


def _build_command(pdfium_test, pdf_path, pages, scale, png, md5):
  command = [str(pdfium_test)]
  if png:
    command.append("--png")
  if md5:
    command.append("--md5")
  if pages:
    command.append(f"--pages={pages}")
  command.append(f"--scale={scale}")
  command.append(str(pdf_path))
  return command


def _parse_md5_output(stdout):
  hashes = {}
  other_lines = []
  for line in stdout.splitlines():
    match = MD5_RE.match(line)
    if not match:
      if line.strip():
        other_lines.append(line)
      continue
    file_name, md5_hash = match.groups()
    hashes[pathlib.Path(file_name).name] = md5_hash.lower()
  if not hashes:
    raise RenderFailure("pdfium_test did not emit any MD5 lines")
  return hashes, other_lines


def _run_once(command, cwd):
  start = time.perf_counter()
  completed = subprocess.run(
      command,
      cwd=cwd,
      capture_output=True,
      text=True,
      check=False)
  elapsed = time.perf_counter() - start
  if completed.returncode != 0:
    raise RenderFailure(
        "command failed with exit code %d\nstdout:\n%s\nstderr:\n%s" %
        (completed.returncode, completed.stdout, completed.stderr))
  return completed, elapsed


def _run_fidelity(pdfium_test, pdf_path, pages, scale, output_dir):
  local_pdf_path = pathlib.Path(output_dir) / pathlib.Path(pdf_path).name
  shutil.copy2(pdf_path, local_pdf_path)
  command = _build_command(
      pdfium_test, local_pdf_path, pages, scale, png=True, md5=True)
  completed, elapsed = _run_once(command, cwd=output_dir)
  hashes, extra_output = _parse_md5_output(completed.stdout)
  return {
      "command": command,
      "elapsed_s": elapsed,
      "hashes": hashes,
      "extra_output": extra_output,
  }


def _run_timing(pdfium_test, pdf_path, pages, scale, warmup, repeats):
  command = _build_command(
      pdfium_test, pdf_path, pages, scale, png=False, md5=False)
  samples = []
  for run_index in range(warmup + repeats):
    _, elapsed = _run_once(command, cwd=pdfium_test.parent)
    if run_index >= warmup:
      samples.append(elapsed)
  return {
      "command": command,
      "samples_s": samples,
      "median_s": statistics.median(samples),
      "min_s": min(samples),
      "max_s": max(samples),
  }


def _compare_hashes(before_hashes, after_hashes):
  mismatches = []
  all_keys = sorted(set(before_hashes) | set(after_hashes))
  for key in all_keys:
    before_hash = before_hashes.get(key)
    after_hash = after_hashes.get(key)
    if before_hash != after_hash:
      mismatches.append((key, before_hash, after_hash))
  return mismatches


def _print_timing(label, timing_data):
  samples_ms = ", ".join(f"{sample * 1000.0:.1f}" for sample in timing_data["samples_s"])
  print(f"{label} timing:")
  print(f"  command: {' '.join(timing_data['command'])}")
  print(f"  median: {timing_data['median_s'] * 1000.0:.1f} ms")
  print(f"  min/max: {timing_data['min_s'] * 1000.0:.1f} ms / "
        f"{timing_data['max_s'] * 1000.0:.1f} ms")
  print(f"  samples: {samples_ms}")


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("before_build_dir", help="build dir containing baseline pdfium_test")
  parser.add_argument("after_build_dir", help="build dir containing candidate pdfium_test")
  parser.add_argument("pdf_path", help="PDF to render")
  parser.add_argument(
      "--fidelity-pages",
      default=None,
      help="0-based page selection for the exact MD5 comparison, e.g. 56-58")
  parser.add_argument(
      "--timing-pages",
      default=None,
      help="0-based page selection for timing. Defaults to --fidelity-pages.")
  parser.add_argument(
      "--scale",
      default="2.0",
      help="render scale passed to pdfium_test --scale= (default: 2.0)")
  parser.add_argument(
      "--warmup",
      type=int,
      default=1,
      help="number of untimed warmup runs per build (default: 1)")
  parser.add_argument(
      "--repeats",
      type=int,
      default=5,
      help="number of timed runs per build (default: 5)")
  parser.add_argument(
      "--artifacts-dir",
      default=None,
      help="directory to keep before/after PNG and MD5 artifacts")
  args = parser.parse_args()

  if args.warmup < 0:
    raise SystemExit("--warmup must be non-negative")
  if args.repeats <= 0:
    raise SystemExit("--repeats must be positive")

  pdf_path = pathlib.Path(args.pdf_path).expanduser().resolve()
  if not pdf_path.exists():
    raise SystemExit(f"missing pdf: {pdf_path}")

  timing_pages = args.timing_pages if args.timing_pages is not None else args.fidelity_pages

  before_pdfium_test = _resolve_build_dir(args.before_build_dir)
  after_pdfium_test = _resolve_build_dir(args.after_build_dir)

  cleanup_artifacts = False
  artifacts_root = args.artifacts_dir
  if artifacts_root:
    artifacts_path = pathlib.Path(artifacts_root).expanduser().resolve()
    artifacts_path.mkdir(parents=True, exist_ok=True)
  else:
    artifacts_path = pathlib.Path(
        tempfile.mkdtemp(prefix="compare_pdfium_render_")).resolve()
    cleanup_artifacts = True

  before_artifacts = artifacts_path / "before"
  after_artifacts = artifacts_path / "after"
  before_artifacts.mkdir(parents=True, exist_ok=True)
  after_artifacts.mkdir(parents=True, exist_ok=True)

  try:
    before_fidelity = _run_fidelity(
        before_pdfium_test, pdf_path, args.fidelity_pages, args.scale,
        before_artifacts)
    after_fidelity = _run_fidelity(
        after_pdfium_test, pdf_path, args.fidelity_pages, args.scale,
        after_artifacts)
    mismatches = _compare_hashes(before_fidelity["hashes"], after_fidelity["hashes"])

    before_timing = _run_timing(
        before_pdfium_test, pdf_path, timing_pages, args.scale, args.warmup,
        args.repeats)
    after_timing = _run_timing(
        after_pdfium_test, pdf_path, timing_pages, args.scale, args.warmup,
        args.repeats)
  except RenderFailure as exc:
    print(f"Artifacts: {artifacts_path}", file=sys.stderr)
    print(str(exc), file=sys.stderr)
    return 1

  print("Exact render fidelity:")
  print(f"  pages: {args.fidelity_pages or 'all'}")
  print(f"  scale: {args.scale}")
  print(f"  baseline md5 count: {len(before_fidelity['hashes'])}")
  print(f"  candidate md5 count: {len(after_fidelity['hashes'])}")
  if before_fidelity["extra_output"]:
    print("  baseline extra stdout:")
    for line in before_fidelity["extra_output"]:
      print(f"    {line}")
  if after_fidelity["extra_output"]:
    print("  candidate extra stdout:")
    for line in after_fidelity["extra_output"]:
      print(f"    {line}")
  if mismatches:
    print(f"  mismatches: {len(mismatches)}")
    for key, before_hash, after_hash in mismatches[:20]:
      print(f"    {key}: {before_hash} != {after_hash}")
    if len(mismatches) > 20:
      print(f"    ... {len(mismatches) - 20} more")
  else:
    print("  mismatches: 0")

  if mismatches or not cleanup_artifacts:
    print()
    print(f"Artifacts: {artifacts_path}")
    print()
  else:
    print()
    print("Artifacts: temporary outputs removed after successful comparison")
    print()

  _print_timing("Baseline", before_timing)
  print()
  _print_timing("Candidate", after_timing)
  print()

  speedup = before_timing["median_s"] / after_timing["median_s"]
  delta_pct = ((before_timing["median_s"] - after_timing["median_s"]) /
               before_timing["median_s"]) * 100.0
  print("Summary:")
  print(f"  timing pages: {timing_pages or 'all'}")
  print(f"  median speedup: {speedup:.2f}x")
  print(f"  median delta: {delta_pct:.1f}%")

  if cleanup_artifacts and not mismatches:
    shutil.rmtree(artifacts_path)

  if mismatches:
    return 2
  return 0


if __name__ == "__main__":
  sys.exit(main())
