#!/usr/bin/env bash
# Runs the whole suite. Needs nothing but python3 and bash.
#
#   ./test/run.sh                 # everything
#   ./test/run.sh test_time.py    # just these
#   ./test/run.sh --docker        # the same suite inside a Linux container
#   ./test/run.sh --live-codex    # the T15 checklist against real codex (asks first)
#
#   CR_TEST_JOBS=1 ./test/run.sh  # one file after another, nothing side by side
set -u
cd "$(dirname "$0")" || exit 1

if [ "${1:-}" = "--docker" ]; then
  shift
  exec ./run-docker.sh "$@"
fi

# Never part of the default run: it spends real quota, so it is its own flag
# and asks before it starts. live_codex.py does its own orphan check at the
# end, whatever happens in between.
if [ "${1:-}" = "--live-codex" ]; then
  shift
  exec "${PYTHON:-python3}" live_codex.py "$@"
fi

PY=${PYTHON:-python3}
fail=0

# Fast and self-contained first, pty-driven last: a failure in the cheap tests is
# usually the cause of the slow ones failing too, and this way it is on screen
# within a second.
files=("$@")
if [ "${#files[@]}" -eq 0 ]; then
  files=(test_patterns.py test_time.py test_transcript.py test_models.py
         test_update.py test_statusline.py test_controller.py test_badge.py
         test_input_grammar.py test_screen.py test_agents.py
         test_check_orphans.py test_live_codex.py
         test_degrade.py test_custom_command.py test_pty.py
         test_codex.py test_fake_agents.py test_two_wrappers.py)
fi

# The files that start real wrappers on real ptys. Nearly all of their time is
# spent waiting — on a clock in the wrapper, on a fake that is pretending to
# think — and a wait costs the same whether one test is sitting in it or six.
# So these are not run one after another: each is cut into $jobs slices (see
# shard.py), and $jobs workers take slices off one queue until it is empty. A
# file that is not on this list is run whole and on its own, as all of them
# used to be; that is always correct, only slower.
#
# Longest first, because that is the order the queue is worked through, and a
# long slice started last is one worker still at it after the rest have gone
# home.
slow="test_pty.py test_codex.py test_custom_command.py test_two_wrappers.py test_degrade.py test_fake_agents.py"

# Four, by measurement, not by the number of cores: the workers are idle nearly
# all the time, so more of them is not what makes this slow — but every one more
# is another few wrappers starting in the same instant, and these tests are
# about timing. Six is a quarter faster again; it was not worth that.
jobs=${CR_TEST_JOBS:-4}
case "$jobs" in
  ''|*[!0-9]*|0) echo "run.sh: CR_TEST_JOBS must be a whole number, 1 or more" >&2; exit 2 ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

whole=()      # run as they always were
sliced=()     # ...and not, in the order they were asked for
for f in "${files[@]}"; do
  case " $slow " in
    *" $f "*) if [ "$jobs" -gt 1 ]; then sliced+=("$f"); else whole+=("$f"); fi ;;
    *) whole+=("$f") ;;
  esac
done
queue=()      # the same files, in the order they are worked through
for f in $slow; do
  case " ${sliced[*]-} " in
    *" $f "*) queue+=("$f") ;;
  esac
done

# ${a[@]+"${a[@]}"}: bash 3.2, which is what macOS still ships, takes an empty
# array under `set -u` for an unset variable.
for f in ${whole[@]+"${whole[@]}"}; do
  echo "=== $f"
  if "$PY" "$f" >"$tmp/out" 2>&1; then
    tail -3 "$tmp/out"
  else
    # A summary is enough when everything passes; when something fails, the whole
    # output is the only thing that matters — especially on a CI runner, where
    # there is no second chance to reproduce it interactively.
    cat "$tmp/out"
    fail=1
  fi
done

# One worker: down the queue, taking every slice nobody has taken yet. `mkdir`
# is the claim — it succeeds for exactly one of however many try at once, on
# every filesystem this runs on, and needs nothing bash 3.2 does not have.
# Which worker ends up with which slice is not decided anywhere and does not
# matter: a slice is the same tests in the same process whoever runs it.
worker() {
  local i=0 f k
  while [ "$i" -lt "$((${#queue[@]} * jobs))" ]; do
    if mkdir "$tmp/taken.$i" 2>/dev/null; then
      f=${queue[$((i / jobs))]}
      k=$((i % jobs))
      "$PY" shard.py "$k" "$jobs" "$f" >"$tmp/$f.$k" 2>&1
      echo "$?" >"$tmp/$f.$k.rc"
    fi
    i=$((i + 1))
  done
}

if [ "${#queue[@]}" -gt 0 ]; then
  n=0
  while [ "$n" -lt "$jobs" ]; do
    worker &
    n=$((n + 1))
  done
  # Every one of them, however the others fared: a worker left running is a
  # wrapper left running, and the orphan check below would be right to say so.
  wait

  # Reported a file at a time and in the order they were asked for, whichever
  # slice finished first, so two runs can be laid side by side.
  for f in "${sliced[@]}"; do
    echo "=== $f"
    ran=0 total= said= bad=0
    k=0
    while [ "$k" -lt "$jobs" ]; do
      out="$tmp/$f.$k"
      rc=$(cat "$out.rc" 2>/dev/null)
      # "shard: ran 17 of 68 in 41.2s" is the last thing shard.py prints.
      set -- $(sed -n 's/^shard: ran \([0-9]*\) of \([0-9]*\) in \([0-9.]*s\)$/\1 \2 \3/p' "$out" 2>/dev/null | tail -1)
      if [ "${rc:-missing}" != 0 ] || [ "$#" -ne 3 ]; then
        echo "--- $PY shard.py $k $jobs $f (exit ${rc:-missing})"
        cat "$out" 2>/dev/null
        echo        # a slice that died mid-line did not end its last one
        bad=1
      fi
      if [ "$#" -eq 3 ]; then
        ran=$((ran + $1))
        # Every slice counted the same file; if two of them disagree about how
        # many tests are in it, neither total means anything.
        [ -z "$total" ] || [ "$total" = "$2" ] || bad=1
        total=$2
        said="$said${said:+, }$1 in $3"
      fi
      k=$((k + 1))
    done
    # The slices have to add up to the file. One that died, or was never heard
    # from, leaves a suite that is green and short — which is the one way this
    # arrangement could quietly prove less than running the file whole.
    if [ "$bad" -eq 0 ] && [ "$ran" = "$total" ]; then
      echo "Ran $ran tests in $jobs slices ($said)"
      echo
      echo "OK"
    else
      echo "Ran $ran of ${total:-?} tests in $jobs slices${said:+ ($said)}"
      echo
      echo "FAILED"
      fail=1
    fi
  done
fi

# Once, and last, after every worker has been reaped. The two seconds are for
# the fakes of the very last tests, which die of their supervisor's death and
# not quite in the same instant; anything still alive after that is not about
# to exit on its own.
echo "=== orphan check"
if ! "$PY" check-orphans.py 2; then
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "ALL PASS"
else
  echo "FAILURES"
fi
exit "$fail"
