# Cross-platform Makefile for COLOSSEUM (Windows + Unix)
# Sử dụng uv để quản lý Python (uv 0.12.2+)

PY := python
VENV := .venv
# Cross-platform: use Scripts on Windows, bin on Unix
ifeq ($(OS),Windows_NT)
    BIN := $(VENV)\Scripts
    PY_BIN := $(BIN)\python
else
    BIN := $(VENV)/bin
    PY_BIN := $(BIN)/python
endif
BOT ?= rookie
# `AS` is a GNU make BUILT-IN (the assembler, default `as`), so `AS ?= as`
# never fired and a plain `make spar BOT=rookie` ran `spar.py --as as`, which
# argparse rejects. `?=` only assigns when a variable is UNDEFINED, and make had
# already defined this one. Keep the documented `AS=defender` interface working
# by honouring AS only when it really came from the command line.
ROLE ?= all
ifeq ($(origin AS),command line)
ROLE := $(AS)
endif

.PHONY: install spar ui validate validate-bots qualify submit test clean check-no-key doctor check-referee check-world

install:
	uv venv --python 3.12 --seed $(VENV)
	$(PY_BIN) -m pip install -q --upgrade pip
	$(PY_BIN) -m pip install -q pytest
	@echo ready. no api key needed, ever.

spar:
	$(PY_BIN) spar.py --bot $(BOT) --as $(ROLE)

ui:
	$(PY_BIN) -m kit.arena_ui.build_ui
	$(PY_BIN) -m kit.arena_ui.serve --no-open

# Always validate against the REAL exported world. Without --world the validator falls
# back to kit/world/fixture.py's ~40-page synthetic world, where every real anchor fails
# to resolve — 15 spurious failures that look like a broken deck and are not.
WORLD := $(firstword $(wildcard kit/world/*/manifest.json))

validate:
ifeq ($(WORLD),)
	$(error no world exported - run 'make check-world')
else
	$(PY_BIN) validate_deck.py deck/deck.json deck/lineup.json --world $(dir $(WORLD))
endif

validate-bots:
ifeq ($(WORLD),)
	$(error no world exported - run 'make check-world')
else
	$(PY_BIN) validate_deck.py bots/rookie/deck.json bots/rookie/lineup.json --world $(dir $(WORLD))
	$(PY_BIN) validate_deck.py bots/operator/deck.json bots/operator/lineup.json --world $(dir $(WORLD))
	$(PY_BIN) validate_deck.py bots/adversary/deck.json bots/adversary/lineup.json --world $(dir $(WORLD))
endif

# `qualify` used to run a `qualify.py` that was never written, writing a
# `submissions/radar.json` that NOTHING in either repo reads. It is not a
# missing dependency, it is a promise that was never wired up. The student's
# real conformance check is the public suite: `make test`.
qualify:
	@echo make qualify: retired — nothing consumed submissions/radar.json.
	@echo Your conformance check is 'make test' (the public suite).
	@echo Then: make validate && make submit TEAM=<your-team>
	@exit 1

# NOT `validate qualify` — qualify is retired (above), and kit.submit REQUIRES
# --team, which this target never passed, so `make submit` failed twice over.
submit: validate
ifeq ($(TEAM),)
	$(error usage: make submit TEAM=<your-team-name>)
else
	$(PY_BIN) -m kit.submit --team $(TEAM)
endif

test: check-no-key
	$(PY_BIN) -m pytest tests/

# The referee in kit/ is a hash-synced copy of the arena's (CONTRACTS.md 2.4): students
# must be able to run the exact verifier that will judge them, or prosecution is guesswork.
check-referee:
ifeq ($(wildcard kit/referee),)
	$(error kit/referee missing - ask your instructor to run tools.sync_referee)
else
	@$(PY_BIN) -c "from kit.referee.rubric import CLASSES; from kit.referee.adjudicate import LOCAL_ONLY; print(f'referee: {len(CLASSES)} classes, local_only={LOCAL_ONLY}')"
endif

# The world artifact is exported by the instructor; without it nothing can run.
check-world:
	@$(PY_BIN) -c "import glob, json, sys; manifests = sorted(glob.glob('kit/world/*/manifest.json')); sys.exit('no world in kit/world/ - ask your instructor for the world artifact') if not manifests else None; truths = glob.glob('kit/world/*/truth.json'); sys.exit('FAIL: truth.json must never ship to students') if truths else None; m = json.load(open(manifests[-1], encoding='utf-8')); print('world', m.get('world_id'), '-', sum(m.get('counts', {}).values()), 'pages')"

doctor: check-no-key check-world check-referee validate
	@echo ready to spar.

# A shipped gate, not a formality: the student kit must contain no model client and no
# API key. It is a real module with its own tests, not a grep — the grep version fired on
# the sandbox's own network-denial probe and on the injection fixtures that have to NAME
# the key to be realistic. Naming a secret is not leaking one; see kit/gate_no_key.py.
check-no-key:
	@$(PY_BIN) -m kit.gate_no_key

clean:
	@$(PY_BIN) -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(p, ignore_errors=True) for p in (pathlib.Path('.pytest_cache'), pathlib.Path('.venv'))]"
