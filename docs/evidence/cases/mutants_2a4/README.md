# Mutant records for the 2a.4 case corpus

Mutation-testing utilities. Each script applies one source edit at a time, runs the
named test files, records KILLED / SURVIVED / INVALID, and restores the file from saved
bytes. A run starts only after an unmutated baseline passes.

- `gate_mut.py`, `gate_mut.log`: 26 mutants of the corpus freeze gates against
  `tests/test_corpus_gates.py`. The log is the second run (26/26). The first run, with the
  tests as first written, printed 24/26 to the console only: G13 (a group family may be
  shared: the length check removed) and G25 (X single-template cap raised by 20 cases)
  survived; `test_red_arm_group_with_a_third_member` and the exact-boundary tests
  were added for them.
- `vmut.py`, `vmut.log`: 45 mutants of the validators, builder, metric-layer search and
  gold SQL check against `tests/test_case_build.py` and `tests/test_case_validators.py`,
  Postgres required. `vmut.py` takes the earlier campaigns' mutant lists
  (`mut_validators_previous.py`, `mut_search_previous.py`) as arguments and retargets two
  of them. 44/45 in the log; `N13_rerun.txt` records the survivor caught after a new test.
