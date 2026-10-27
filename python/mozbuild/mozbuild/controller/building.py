# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import errno
import getpass
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter, namedtuple
from itertools import dropwhile, islice, takewhile
from pathlib import Path
from textwrap import TextWrapper

from mach.logging import BUILD_ERROR, SUPPRESSED_WARNING, THIRD_PARTY_WARNING
from mach.site import CommandSiteManager

try:
    import psutil
except Exception:
    psutil = None

import mozfile
import mozpack.path as mozpath
from mach.mixin.logging import LoggingMixin
from mach.util import get_state_dir, get_virtualenv_base_dir
from mozsystemmonitor.resourcemonitor import SystemResourceMonitor
from rich.console import Console
from rich.live import Live
from rich.text import Text

from ..backend import get_backend_class
from ..base import MozbuildObject
from ..compilation.warnings import (
    RE_STRIP_COLORS,
    WarningsCollector,
    WarningsDatabase,
)
from ..dirutils import mkdir
from ..serialized_logging import read_serialized_record
from ..telemetry import get_cpu_brand
from ..testing import install_test_files
from ..util import (
    FileAvoidWrite,
    construct_log_filename,
    is_running_under_coding_agent,
    resolve_target_to_make,
)
from ..rust_commands import cargo_unit_graph_from_spec, cargo_unit_identity
from .clobber import Clobberer

CARGO_UNIT_GRAPH_TOOLCHAIN = "nightly-2026-10-25"

RE_BUILD_OUTPUT = re.compile(
    r"""
    (?P<info_masm>Microsoft\ \(R\)\ Macro\ Assembler|Copyright\ \(C\)\ Microsoft\ Corporation)
    |(?P<info_cargo>^\s{3,}(?:Compiling|Downloading|Building|Finished|Fresh|Running|Documenting)\s)
    |(?P<warning_summary>^\d+\s+(?:compiler\s+)?warnings?\s+(?:generated|present)\.)
    |(?P<error_summary>^\d+\s+errors?\s+generated\.)
    |(?P<python_traceback>^Traceback\ \(most\ recent\ call\ last\):)
    |(?P<make_error>make(?:\[\d+\])?\s*:\s*\*\*\*)
    |(?P<nsis_warning_block>^\d+\s+warnings?:)
    |(?P<error_block>^error(?:\[e\d+\])?:\s?)
    |(?P<warning_standalone>^warning:\s+mkdir\s)
    |(?P<warning_num>^warning\s+\d+:)
    |(?P<warning_block>^warning:\s?)
    |(?P<include_from>^In\ file\ included\ from\ (?P<include_path>.+?):\d+)
    |(?P<gcc_include_continuation>^\s+from\s+(?P<gcc_from_path>.+?):\d+)
    |(?P<gcc_context>^(?P<gcc_context_path>.+?):\s*(?:In\s+.+|At\s+global\s+scope):)
    |(?P<inline_diagnostic>^(?P<diag_path>.+?)(?:\(\d+,\d+\)|:\d+:\d+)?:\s*(?P<diag_type>warning|error|note):)
    |(?P<continuation>^\s+(?:\d+\s+)?\||\s+[\^~])
    """,
    re.VERBOSE | re.IGNORECASE,
)

FINDER_SLOW_MESSAGE = """
===================
PERFORMANCE WARNING

The OS X Finder application (file indexing used by Spotlight) used a lot of CPU
during the build - an average of %f%% (100%% is 1 core). This made your build
slower.

Consider adding ".noindex" to the end of your object directory name to have
Finder ignore it. Or, add an indexing exclusion through the Spotlight System
Preferences.
===================
""".strip()


INSTALL_TESTS_CLOBBER = "".join([
    TextWrapper().fill(line) + "\n"
    for line in """
The build system was unable to install tests because the CLOBBER file has \
been updated. This means if you edited any test files, your changes may not \
be picked up until a full/clobber build is performed.

The easiest and fastest way to perform a clobber build is to run:

 $ mach clobber
 $ mach build

If you did not modify any test files, it is safe to ignore this message \
and proceed with running tests. To do this run:

 $ touch {clobber_file}
""".splitlines()
])

CLOBBER_REQUESTED_MESSAGE = """
===================
The CLOBBER file was updated prior to this build. A clobber build may be
required to succeed, but we weren't expecting it to.

Please consider filing a bug for this failure if you have reason to believe
this is a clobber bug and not due to local changes.
===================
""".strip()


BuildOutputResult = namedtuple(
    "BuildOutputResult", ("warning", "state_changed", "message")
)


class TierStatus:
    """Represents the state and progress of tier traversal.

    The build system is organized into linear phases called tiers. Each tier
    executes in the order it was defined, 1 at a time.
    """

    def __init__(self, resources, metrics):
        """Accepts a SystemResourceMonitor to record results against."""
        self.tiers = {}
        self.tier_status = {}
        self.resources = resources
        self.metrics = metrics

    def set_tiers(self, tiers):
        """Record the set of known tiers."""
        for tier in tiers:
            self.tiers[tier] = dict(
                begin_time=None,
                finish_time=None,
                duration=None,
            )
            self.tier_status[tier] = None

    def begin_tier(self, tier):
        """Record that execution of a tier has begun."""
        self.tier_status[tier] = "active"
        t = self.tiers[tier]
        t["begin_time"] = time.monotonic()
        self.resources.begin_phase(tier)
        metrics_tier_name = "tier_" + tier.replace("-", "_") + "_duration"
        metrics_attribute = getattr(self.metrics.mozbuild, metrics_tier_name, None)
        if metrics_attribute:
            metrics_attribute.start()

    def finish_tier(self, tier):
        """Record that execution of a tier has finished."""
        self.tier_status[tier] = "finished"
        t = self.tiers[tier]
        t["finish_time"] = time.monotonic()
        t["duration"] = self.resources.finish_phase(tier)
        metrics_tier_name = "tier_" + tier.replace("-", "_") + "_duration"
        metrics_attribute = getattr(self.metrics.mozbuild, metrics_tier_name, None)
        if metrics_attribute:
            metrics_attribute.stop()


def record_cargo_timings(resource_monitor, timings_path):
    cargo_start = 0
    try:
        with open(timings_path, encoding="utf-8") as fh:
            # Extrace the UNIT_DATA list from the cargo timing HTML file.
            unit_data = dropwhile(lambda l: l.rstrip() != "const UNIT_DATA = [", fh)
            unit_data = islice(unit_data, 1, None)
            lines = takewhile(lambda l: l.rstrip() != "];", unit_data)
            entries = json.loads("[" + "".join(lines) + "]")
            # Normalize the entries so that any change in data format would
            # trigger the exception handler that skips this (we don't want the
            # build to fail in that case)
            data = [
                (
                    "{} v{}{}".format(
                        entry["name"], entry["version"], entry.get("target", "")
                    ),
                    entry["start"] or 0,
                    entry["duration"] or 0,
                )
                for entry in entries
            ]
        starts = [
            start
            for marker, start in resource_monitor._active_markers.items()
            if marker.startswith("Rust:")
        ]
        # The build system is not supposed to be running more than one cargo
        # at the same time, which thankfully makes it easier to find the start
        # of the one we got the timings for.
        if len(starts) != 1:
            return
        cargo_start = starts[0]
    except Exception:
        return

    if not cargo_start:
        return

    for name, start, duration in data:
        resource_monitor.record_marker(
            "RustCrate", cargo_start + start, cargo_start + start + duration, name
        )


class BuildMonitor(MozbuildObject):
    """Monitors the output of the build."""

    def init(self, warnings_path, terminal, metrics):
        """Create a new monitor.

        warnings_path is a path of a warnings database to use.
        """
        self._warnings_path = warnings_path
        self.resources = SystemResourceMonitor(
            poll_interval=0.1,
            metadata={"CPUName": get_cpu_brand()},
        )
        self._resources_started = False

        self.tiers = TierStatus(self.resources, metrics)

        self.warnings_database = WarningsDatabase()
        if os.path.exists(warnings_path):
            try:
                self.warnings_database.load_from_file(warnings_path)
            except ValueError:
                os.remove(warnings_path)

        # Contains warnings unique to this invocation. Not populated with old
        # warnings.
        self.instance_warnings = WarningsDatabase()

        self._terminal = terminal

        def on_warning(warning):
            # Skip `errors`
            if warning["type"] == "error":
                return

            filename = warning["filename"]

            if not os.path.exists(filename):
                raise Exception("Could not find file containing warning: %s" % filename)

            self.warnings_database.insert(warning)
            # Make a copy so mutations don't impact other database.
            self.instance_warnings.insert(warning.copy())

        self._warnings_collector = WarningsCollector(on_warning, objdir=self.topobjdir)

        self.build_objects = []
        self.build_dirs = set()

        self.planned_build_events = set()
        self.planned_build_events_by_tier = {}
        self.planned_tiers = set()
        self.completed_build_events = set()
        self.planned_native_compile_units = set()
        self.completed_native_compile_units = set()
        self.use_backend_native_compile_plan = False
        self._planning_tier = None
        self._planning_build_dir = "."
        self._active_tier = None
        self.dry_run_in_progress = False
        self.planned_cargo_specs = {}
        self.planned_cargo_units = {}
        self.planned_cargo_units_by_edge = {}
        self.completed_cargo_units = set()
        self._planning_cargo_edge = None
        self._active_cargo_edge = None
        self.build_completed = False

        """
        # debug configuration
        self.cwd_path = Path(self.topsrcdir)
        self.log_dir_path = self.cwd_path.joinpath("tmp")
        self.log_dir_path.mkdir(exist_ok=True)
        self.planned_make_log_path = self.log_dir_path.joinpath("planned_make_log.txt")
        self.build_make_log_path = self.log_dir_path.joinpath("build_make_log.txt")
        """

    def start(self):
        """Record the start of the build."""
        self.start_time = time.monotonic()
        self._finder_start_cpu = self._get_finder_cpu_usage()

    def start_resource_recording(self):
        # This should be merged into start() once bug 892342 lands.
        self.resources.start()
        self._resources_started = True

        if "MOZ_AUTOMATION" in os.environ and "UPLOAD_PATH" in os.environ:
            self._build_resources_profile_path = mozpath.join(
                os.environ["UPLOAD_PATH"], "profile_build_resources.json"
            )
        else:
            self._ensure_build_log_dir_exists()
            self._build_resources_profile_path = self._get_build_log_filename(
                construct_log_filename("profile")
            )
        self.resources.start_streaming(self._build_resources_profile_path)
        print(
            f"Streaming resource usage profile to: {self._build_resources_profile_path}"
        )

    @staticmethod
    def _parse_build_status(line, terminal=None, dry_run=False):
        plain_line = terminal.strip(line) if terminal else line.strip()

        if dry_run and not plain_line.startswith("BUILDSTATUS"):
            try:
                command = shlex.split(plain_line)
            except ValueError:
                return None
            if not command or command[0] not in ("echo", "@echo"):
                return None
            plain_line = " ".join(command[1:])

        if not plain_line.startswith("BUILDSTATUS"):
            return None

        args = plain_line.split()
        _, _, disambiguator = args.pop(0).partition("@")
        action = args.pop(0)
        timestamp = None
        if re.fullmatch(r"\d{10}(?:\.\d{1,9})?", action):
            timestamp = float(action)
            action = args.pop(0)

        return disambiguator, action, args, timestamp

    """
    # debug configuration
    @staticmethod
    def _format_build_status(build_status):
        disambiguator, action, args, timestamp = build_status
        parts = [f"BUILDSTATUS@{disambiguator}"]
        if timestamp is not None:
            parts.append(str(timestamp))
        parts.extend([action, *args])
        return " ".join(parts)
    """

    @staticmethod
    def _build_event_key(disambiguator, action, args):
        return disambiguator, action, tuple(args)

    @staticmethod
    def _cargo_edge_key(disambiguator, args):
        return disambiguator, tuple(args)

    def load_native_compile_units(self, manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                units = json.load(fh)
            self.planned_native_compile_units = {
                (unit["directory"], unit["object"]) for unit in units
            }
        except (OSError, ValueError, KeyError, TypeError):
            return False

        self.use_backend_native_compile_plan = bool(self.planned_native_compile_units)
        return self.use_backend_native_compile_plan

    def _add_planned_build_event(self, event):
        if event in self.planned_build_events:
            return False
        self.planned_build_events.add(event)
        if self._active_tier is not None:
            self.planned_build_events_by_tier.setdefault(self._active_tier, set()).add(
                event
            )
        return True

    def _record_native_compile_start(self, directory, object_name):
        unit = (mozpath.normsep(directory) or ".", object_name)
        if unit not in self.planned_native_compile_units:
            self.planned_native_compile_units.add(unit)
        return unit

    def _record_native_compile_end(self, directory, object_name):
        unit = self._record_native_compile_start(directory, object_name)
        if unit in self.completed_native_compile_units:
            return False
        self.completed_native_compile_units.add(unit)
        return True

    def _collect_cargo_spec_from_dry_line(self, line):
        # dry-runで得られる出力のうちcargo関連の予定イベントを取得
        if self._planning_cargo_edge is None:
            return

        plain_line = self._terminal.strip(line) if self._terminal else line.strip()
        try:
            command = shlex.split(plain_line)
        except ValueError:
            return

        if "mozbuild.action.run_cargo" not in command and not any(
            Path(token).name == "run_cargo.py" for token in command
        ):
            return

        try:
            spec_index = command.index("--spec") + 1
            spec = Path(command[spec_index])
        except (ValueError, IndexError):
            return

        if not spec.is_absolute():
            object_dir = Path(self.topobjdir)
            candidates = [
                object_dir / self._planning_cargo_edge[0] / spec,
                object_dir / self._planning_build_dir / spec,
                object_dir / spec,
            ]
            spec = next(
                (candidate for candidate in candidates if candidate.is_file()), spec
            )

        if spec.is_file():
            self.planned_cargo_specs[self._planning_cargo_edge] = spec

    def collect_planned_cargo_units(self, environ):
        graph_cache = {}
        logged_errors = set()

        for edge, spec_path in self.planned_cargo_specs.items():
            try:
                graph = graph_cache.get(spec_path)
                if graph is None:
                    graph = cargo_unit_graph_from_spec(
                        spec_path, CARGO_UNIT_GRAPH_TOOLCHAIN, environ
                    )
                    graph_cache[spec_path] = graph
            except Exception as exc:
                if str(exc) not in logged_errors:
                    self.log(
                        logging.WARNING,
                        "cargo_unit_graph",
                        {"path": str(spec_path), "msg": str(exc)},
                        "Unable to query Cargo unit graph for {path}: {msg}",
                    )
                    logged_errors.add(str(exc))
                continue

            units = {
                cargo_unit_identity(unit): unit
                for unit in graph["units"]
                if unit.get("mode") in ("build", "check", "test")
            }
            if not units:
                continue

            self.planned_cargo_units_by_edge[edge] = units
            self.planned_cargo_units.update(units)

            rust_end = self._build_event_key(edge[0], "END_Rust", edge[1])
            self.planned_build_events.discard(rust_end)
            for events in self.planned_build_events_by_tier.values():
                events.discard(rust_end)

        return bool(self.planned_cargo_units)

    @staticmethod
    def _cargo_unit_matches_artifact(unit, artifact):
        if unit["pkg_id"] != artifact.get("package_id"):
            return False

        planned_target = unit["target"]
        actual_target = artifact.get("target", {})
        for field in ("name", "kind", "crate_types"):
            planned_value = planned_target.get(field)
            actual_value = actual_target.get(field)
            if isinstance(planned_value, list) and isinstance(actual_value, list):
                if sorted(planned_value) != sorted(actual_value):
                    return False
            elif planned_value != actual_value:
                return False

        if sorted(unit.get("features", [])) != sorted(artifact.get("features", [])):
            return False

        planned_profile = unit.get("profile", {})
        actual_profile = artifact.get("profile", {})
        for field in (
            "opt_level",
            "debuginfo",
            "debug_assertions",
            "overflow_checks",
            "codegen_units",
            "incremental",
        ):
            expected = planned_profile.get(field)
            if field in actual_profile and expected is not None:
                if expected != actual_profile[field]:
                    return False

        if "test" in actual_profile:
            expected_test = unit.get("mode") == "test"
            if expected_test != actual_profile["test"]:
                return False

        return True

    def _record_cargo_artifact(self, artifact):
        units = self.planned_cargo_units_by_edge.get(self._active_cargo_edge)
        if not units:
            return False

        candidates = [
            unit_id
            for unit_id, unit in units.items()
            if unit_id not in self.completed_cargo_units
            and self._cargo_unit_matches_artifact(unit, artifact)
        ]
        if not candidates:
            return False

        filenames = artifact.get("filenames", [])
        candidate_platforms = {
            units[unit_id].get("platform")
            for unit_id in candidates
            if units[unit_id].get("platform")
        }
        matching_platforms = {
            platform
            for platform in candidate_platforms
            if any(f"/{platform}/" in filename for filename in filenames)
        }
        if matching_platforms:
            candidates = [
                unit_id
                for unit_id in candidates
                if units[unit_id].get("platform") in matching_platforms
            ]

        self.completed_cargo_units.add(sorted(candidates)[0])
        return True

    def _finish_cargo_edge(self, edge):
        # Rust終了イベント発生時にRustのコンパイル予定イベントの中で実行されなかったものを実行済にカウントし辻褄をあわせる
        units = self.planned_cargo_units_by_edge.get(edge)
        if not units:
            return False
        incomplete = set(units) - self.completed_cargo_units
        self.completed_cargo_units.update(incomplete)
        return bool(incomplete)

    def _on_cargo_json_line(self, line):
        # BuildMonitor.on_line()のうちcargo関連の出力の処理を行う
        plain_line = self._terminal.strip(line) if self._terminal else line.strip()
        try:
            message = json.loads(plain_line)
        except (TypeError, ValueError):
            return None

        if not isinstance(message, dict):
            return None

        reason = message.get("reason")
        if reason == "compiler-artifact":
            return BuildOutputResult(None, self._record_cargo_artifact(message), None)

        if reason == "compiler-message":
            return BuildOutputResult(None, False, None)

        if reason == "build-script-executed":
            warnings = []
            for output_line in message.get("stdout", "").splitlines():
                for prefix in ("cargo:warning=", "cargo::warning="):
                    if output_line.startswith(prefix):
                        warnings.append("warning: " + output_line[len(prefix) :])
                        break
            if warnings:
                return BuildOutputResult(None, False, "\n".join(warnings))

        if reason in (
            "build-script-executed",
            "build-finished",
            "future-incompat-report",
        ):
            return BuildOutputResult(None, False, None)

        return None

    def _record_completed_build_event(self, disambiguator, action, args):
        event = self._build_event_key(disambiguator, action, args)
        self._add_planned_build_event(event)
        if event in self.completed_build_events:
            return False
        self.completed_build_events.add(event)
        return True

    def _complete_build_tier(self, tier):
        # 各TIER終了時にそのTIERで予定されていたが実行されなかったイベントを実行済みにカウントしTIERごとの辻褄をあわせる
        planned = self.planned_build_events_by_tier.get(tier, set())
        new_events = planned - self.completed_build_events
        self.completed_build_events.update(new_events)
        if tier == "compile":
            self.completed_native_compile_units.update(
                self.planned_native_compile_units
            )
        return bool(new_events)

    def on_line(self, line):
        """Consume a line of output from the build system.

        This will parse the line for state and determine whether more action is
        needed.

        Returns a BuildOutputResult instance.

        In this named tuple, warning will be an object describing a new parsed
        warning. Otherwise it will be None.

        state_changed indicates whether the build system changed state with
        this line. If the build system changed state, the caller may want to
        query this instance for the current state in order to update UI, etc.

        message is either None, or the content of a message to be
        displayed to the user (as a str or a logging.LogRecord).
        """
        message = None

        cargo_result = self._on_cargo_json_line(line)
        if cargo_result is not None:
            return cargo_result

        # If the previous line was colored (eg. for a compiler warning), our
        # line will start with the ansi reset sequence. Strip it to ensure it
        # does not interfere with our parsing of the line.
        plain_line = self._terminal.strip(line) if self._terminal else line.strip()

        build_status = self._parse_build_status(line, terminal=self._terminal)
        if build_status:
            """
            # debug configuration
            with self.build_make_log_path.open("a", encoding="utf-8") as log:
                log.write(self._format_build_status(build_status) + "\n")
            """

            disambiguator, action, args, timestamp = build_status
            update_needed = True

            if action == "TIERS":
                self.tiers.set_tiers(args)
                update_needed = False
            elif action == "TIER_START":
                tier = args[0]
                self._active_tier = tier
                self.tiers.begin_tier(tier)
            elif action == "TIER_FINISH":
                (tier,) = args
                self.tiers.finish_tier(tier)
                self._complete_build_tier(tier)
                self._active_tier = None
            elif action == "OBJECT_FILE":
                self.build_objects.append(args[0])
                self.resources.begin_marker("Object", args[0], disambiguator)
                if self.use_backend_native_compile_plan:
                    self._record_native_compile_start(disambiguator, args[0])
                else:
                    self._add_planned_build_event(
                        self._build_event_key(disambiguator, "END_Object", [args[0]])
                    )
                update_needed = True
            elif action.startswith("START_"):
                if action == "START_Rust":
                    self._active_cargo_edge = self._cargo_edge_key(disambiguator, args)
                cargo_edge = self._cargo_edge_key(disambiguator, args)
                if (
                    action != "START_Rust"
                    or cargo_edge not in self.planned_cargo_units_by_edge
                ):
                    end_action = "END_" + action[len("START_") :]
                    plan_changed = self._add_planned_build_event(
                        self._build_event_key(disambiguator, end_action, args)
                    )
                else:
                    plan_changed = False
                self.resources.begin_marker(
                    action[len("START_") :],
                    " ".join(args),
                    disambiguator,
                    timestamp,
                )
                update_needed = plan_changed
            elif action.startswith("END_"):
                self.resources.end_marker(
                    action[len("END_") :],
                    " ".join(args),
                    disambiguator,
                    timestamp,
                )
                if action == "END_Object" and self.use_backend_native_compile_plan:
                    update_needed = self._record_native_compile_end(
                        disambiguator, args[0]
                    )
                elif action == "END_Rust":
                    edge = self._cargo_edge_key(disambiguator, args)
                    if edge in self.planned_cargo_units_by_edge:
                        update_needed = self._finish_cargo_edge(edge)
                    else:
                        update_needed = self._record_completed_build_event(
                            disambiguator, action, args
                        )
                    if self._active_cargo_edge == edge:
                        self._active_cargo_edge = None
                else:
                    update_needed = self._record_completed_build_event(
                        disambiguator, action, args
                    )
            elif action == "BUILD_VERBOSE":
                build_dir = args[0]
                if build_dir not in self.build_dirs:
                    self.build_dirs.add(build_dir)
                    message = build_dir
                update_needed = False
            else:
                raise Exception("Unknown build status: %s" % action)

            return BuildOutputResult(None, update_needed, message)

        elif plain_line.startswith("Timing report saved to "):
            cargo_timings = plain_line[len("Timing report saved to ") :]
            record_cargo_timings(self.resources, cargo_timings)
            return BuildOutputResult(None, False, None)

        if log_record := read_serialized_record(line):
            return BuildOutputResult(None, False, log_record)

        warning = None
        message = line

        try:
            warning = self._warnings_collector.process_line(line)
        except Exception:
            pass

        return BuildOutputResult(warning, False, message)

    def on_line_for_dry(self, line):
        """Collect the build work units emitted by make's dry run."""

        build_status = self._parse_build_status(
            line, terminal=self._terminal, dry_run=True
        )

        if build_status:
            """
            # debug configuration
            with self.planned_make_log_path.open("a", encoding="utf-8") as log:
                log.write(self._format_build_status(build_status) + "\n")
            """
            disambiguator, action, args, _ = build_status
            if action == "TIER_START":
                self._planning_tier = args[0]
                self.planned_tiers.add(self._planning_tier)
            elif action == "TIER_FINISH":
                self._planning_tier = None
            elif action == "BUILD_VERBOSE":
                self._planning_build_dir = args[0]
            elif action == "START_Rust":
                self._planning_cargo_edge = self._cargo_edge_key(disambiguator, args)
            elif action.startswith("END_"):
                if not (
                    self.use_backend_native_compile_plan and action == "END_Object"
                ):
                    event = self._build_event_key(disambiguator, action, args)
                    self.planned_build_events.add(event)
                    if self._planning_tier is not None:
                        self.planned_build_events_by_tier.setdefault(
                            self._planning_tier, set()
                        ).add(event)
                if action == "END_Rust":
                    self._planning_cargo_edge = None

        self._collect_cargo_spec_from_dry_line(line)

    def stop_resource_recording(self):
        if self._resources_started:
            self.resources.stop()

        self._resources_started = False

    def finish(self):
        """Record the end of the build."""
        self.stop_resource_recording()
        self.end_time = time.monotonic()
        self._finder_end_cpu = self._get_finder_cpu_usage()
        self.elapsed = self.end_time - self.start_time

        self.warnings_database.prune()
        self.warnings_database.save_to_file(self._warnings_path)

    def record_usage(self):
        try:
            with open(
                self._build_resources_profile_path, "w", encoding="utf-8", newline="\n"
            ) as fh:
                to_write = json.dumps(
                    self.resources.as_profile(), separators=(",", ":")
                )
                fh.write(to_write)
        except Exception as e:
            self.log(
                logging.WARNING,
                "build_resources_error",
                {"msg": str(e)},
                "Exception when writing resource usage file: {msg}",
            )

    def _get_finder_cpu_usage(self):
        """Obtain the CPU usage of the Finder app on OS X.

        This is used to detect high CPU usage.
        """
        if not sys.platform.startswith("darwin"):
            return None

        if not psutil:
            return None

        for proc in psutil.process_iter():
            if proc.name != "Finder":
                continue

            if proc.username != getpass.getuser():
                continue

            # Try to isolate system finder as opposed to other "Finder"
            # processes.
            if not proc.exe.endswith("CoreServices/Finder.app/Contents/MacOS/Finder"):
                continue

            return proc.get_cpu_times()

        return None

    def have_high_finder_usage(self):
        """Determine whether there was high Finder CPU usage during the build.

        Returns True if there was high Finder CPU usage, False if there wasn't,
        or None if there is nothing to report.
        """
        if not self._finder_start_cpu:
            return None, None

        # We only measure if the measured range is sufficiently long.
        if self.elapsed < 15:
            return None, None

        if not self._finder_end_cpu:
            return None, None

        start = self._finder_start_cpu
        end = self._finder_end_cpu

        start_total = start.user + start.system
        end_total = end.user + end.system

        cpu_seconds = end_total - start_total

        # If Finder used more than 25% of 1 core during the build, report an
        # error.
        finder_percent = cpu_seconds / self.elapsed * 100

        return finder_percent > 25, finder_percent

    def have_excessive_swapping(self):
        """Determine whether there was excessive swapping during the build.

        Returns a tuple of (excessive, swap_in, swap_out). All values are None
        if no swap information is available.
        """
        if not self.have_resource_usage:
            return None, None, None

        swap_in = sum(m.swap.sin for m in self.resources.measurements)
        swap_out = sum(m.swap.sout for m in self.resources.measurements)

        # The threshold of 1024 MB has been arbitrarily chosen.
        #
        # Choosing a proper value that is ideal for everyone is hard. We will
        # likely iterate on the logic until people are generally satisfied.
        # If a value is too low, the eventual warning produced does not carry
        # much meaning. If the threshold is too high, people may not see the
        # warning and the warning will thus be ineffective.
        excessive = swap_in > 512 * 1048576 or swap_out > 512 * 1048576
        return excessive, swap_in, swap_out

    @property
    def have_resource_usage(self):
        """Whether resource usage is available."""
        return self.resources.start_time is not None

    def get_resource_usage(self):
        """Produce a data structure containing the low-level resource usage information.

        This data structure can e.g. be serialized into JSON and saved for
        subsequent analysis.

        If no resource usage is available, None is returned.
        """
        if not self.have_resource_usage:
            return None

        cpu_percent = self.resources.aggregate_cpu_percent(phase=None, per_cpu=False)
        io = self.resources.aggregate_io(phase=None)

        return dict(
            cpu_percent=cpu_percent,
            io=io,
        )

    def log_resource_usage(self, usage):
        """Summarize the resource usage of this build in a log message."""

        if not usage:
            return

        params = dict(
            duration=self.end_time - self.start_time,
            cpu_percent=usage["cpu_percent"],
            io_read_bytes=usage["io"].read_bytes,
            io_write_bytes=usage["io"].write_bytes,
        )

        message = (
            "Overall system resources - Wall time: {duration:.0f}s; "
            "CPU: {cpu_percent:.0f}%; "
            "Read bytes: {io_read_bytes}; Write bytes: {io_write_bytes}; "
        )

        if hasattr(usage["io"], "read_time") and hasattr(usage["io"], "write_time"):
            params.update(
                io_read_time=usage["io"].read_time,
                io_write_time=usage["io"].write_time,
            )
            message += "Read time: {io_read_time}; Write time: {io_write_time}"

        self.log(logging.WARNING, "resource_usage", params, message)

        excessive, sin, sout = self.have_excessive_swapping()
        if excessive is not None and (sin or sout):
            sin /= 1048576
            sout /= 1048576
            self.log(
                logging.WARNING,
                "swap_activity",
                {"sin": sin, "sout": sout},
                "Swap in/out (MB): {sin}/{sout}",
            )

    def ccache_stats(self, ccache=None):
        ccache_stats = None

        if ccache is None:
            ccache = mozfile.which("ccache")
        if ccache:
            # With CCache v3.7+ we can use --print-stats
            has_machine_format = CCacheStats.check_version_3_7_or_newer(ccache)
            try:
                output = subprocess.check_output(
                    [ccache, "--print-stats" if has_machine_format else "-s"],
                    universal_newlines=True,
                )
                ccache_stats = CCacheStats(output, has_machine_format)
            except ValueError as e:
                self.log(logging.WARNING, "ccache", {"msg": str(e)}, "{msg}")
        return ccache_stats


class TerminalLoggingHandler(logging.Handler):
    """Routes log records to a Rich Console so they appear above the live footer.

    This class should probably live elsewhere, like the mach core. Consider
    this a proving ground for its usefulness.
    """

    def __init__(self, console):
        logging.Handler.__init__(self)
        self.console = console

    def flush(self):
        self.acquire()
        try:
            self.console.file.flush()
        finally:
            self.release()

    def emit(self, record):
        msg = self.format(record)
        self.acquire()
        try:
            self.console.print(Text.from_ansi(msg), soft_wrap=True)
        finally:
            self.release()


class BuildProgressFooter:
    """Handles display of a build progress indicator in a terminal.

    When mach builds inside a terminal, it will render progress information
    collected from a BuildMonitor. This class converts the state of
    BuildMonitor into terminal output.
    """

    def __init__(self, monitor):
        self.monitor = monitor

    def __rich__(self):
        tiers = list(self.monitor.tiers.tier_status.items())
        if not tiers and not self.monitor.dry_run_in_progress:
            return Text("")

        planned = self.monitor.planned_build_events
        completed = self.monitor.completed_build_events.intersection(planned)
        planned_native = self.monitor.planned_native_compile_units
        completed_native = self.monitor.completed_native_compile_units.intersection(
            planned_native
        )
        planned_cargo = self.monitor.planned_cargo_units
        completed_cargo = self.monitor.completed_cargo_units.intersection(planned_cargo)
        total = len(planned) + len(planned_native) + len(planned_cargo) + 1
        done = (
            len(completed)
            + len(completed_native)
            + len(completed_cargo)
            + int(self.monitor.build_completed)
        )

        # The drawn terminal looks something like:
        # TIER: static export libs tools

        t = Text(no_wrap=True, overflow="ellipsis")

        if self.monitor.dry_run_in_progress:
            t.append("Progress: collecting plan\n")
        elif len(tiers) > 1:
            percentage = done / total * 100
            progressBar = Text("[")
            step = 30
            for i in range(step):
                if i * 100 / step <= percentage:
                    progressBar.append("#")
                else:
                    progressBar.append("-")
            progressBar.append("]")
            t.append(progressBar)
            t.append(f" ({percentage:.2f}%)\n")
            
        t.append("TIER:", style="bold")
        for tier, status in tiers:
            t.append(" ")
            if status is None:
                t.append(tier)
            elif status == "finished":
                t.append(tier, style="green")
            else:
                t.append(tier, style="underline yellow")
        return t


class OutputManager(LoggingMixin):
    """Handles writing job output to a terminal or log."""

    def __init__(self, log_manager, footer):
        self.populate_logger()

        self.live = None
        self.console = None
        self._handler = None

        terminal = log_manager.terminal

        # TODO convert terminal footer to config file setting.
        if not terminal:
            return
        if os.environ.get("INSIDE_EMACS", None):
            return

        self.console = Console(
            file=sys.stdout,
            soft_wrap=True,
            highlight=False,
            markup=False,
            emoji=False,
        )

        self._handler = TerminalLoggingHandler(self.console)
        self._handler.setFormatter(log_manager.terminal_formatter)
        old = log_manager.replace_terminal_handler(self._handler)
        self._handler.level = old.level

        if os.environ.get("MACH_NO_TERMINAL_FOOTER", None):
            footer = None

        if footer is not None and self.console.is_terminal:
            self.live = Live(
                footer,
                console=self.console,
                auto_refresh=False,
                transient=True,
            )

    def __enter__(self):
        if self.live is not None:
            self.live.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.live is not None:
            self.live.stop()
            self.live = None

    def write_line(self, line):
        if self.console is not None:
            self.console.print(line, soft_wrap=True)
        else:
            print(line)

    def refresh(self):
        if self.live is not None:
            self.live.refresh()


class BuildOutputManager(OutputManager):
    """Handles writing build output to a terminal, to logs, etc."""

    def __init__(self, log_manager, monitor, footer):
        self.monitor = monitor
        self._active_log_level = None
        self._stdout_warning_lines_remaining = 0
        self._third_party_dirs = self._load_third_party_paths()
        OutputManager.__init__(self, log_manager, footer)

    def _load_third_party_paths(self):
        paths = []
        for filename in ("ThirdPartyPaths.txt", "Generated.txt"):
            filepath = mozpath.join(
                self.monitor.topsrcdir, "tools", "rewriting", filename
            )
            if os.path.exists(filepath):
                with open(filepath, encoding="utf-8", newline="\n") as f:
                    paths.extend(line.rstrip("\n/") for line in f)
        return tuple(paths)

    def _load_suppressed_flags(self):
        substs = self.monitor.substs
        return {
            w.replace("-Wno-error=", "-W")
            for w in substs.get("WARNINGS_CFLAGS", [])
            + substs.get("WARNINGS_CXXFLAGS", [])
            if w.startswith("-Wno-error=")
        }

    @property
    def _suppressed_flags(self):
        if not hasattr(self, "_suppressed_flags_cache"):
            self._suppressed_flags_cache = self._load_suppressed_flags()
        return self._suppressed_flags_cache

    def _is_third_party_path(self, filepath):
        path = mozpath.normsep(filepath)
        # Handle the case where we have relative path from Unified build
        if not os.path.isabs(path):
            stripped = re.sub(r"^([./\\])*[/\\]", "", path)
            if os.path.exists(stripped):
                path = mozpath.abspath(stripped)
        if not path.startswith(self.monitor.topsrcdir):
            return True  # generated file
        if not self._third_party_dirs:
            return False
        path = path[len(self.monitor.topsrcdir) + 1 :]
        return path.startswith(self._third_party_dirs)

    def __exit__(self, exc_type, exc_value, traceback):
        OutputManager.__exit__(self, exc_type, exc_value, traceback)

        # Ensure the resource monitor is stopped because leaving it running
        # could result in the process hanging on exit because the resource
        # collection child process hasn't been told to stop.
        self.monitor.stop_resource_recording()

    def _refresh_with_lock(self):
        self.refresh()

    def on_stdout_line(self, line):
        """Handle stdout output - log as INFO, but catch obvious warnings/errors."""

        warning, state_changed, message = self.monitor.on_line(line)
        if message:
            if isinstance(message, logging.LogRecord):
                self.log_record("build_output", message)
            else:
                log_level = logging.INFO
                if isinstance(message, str):
                    stripped = RE_STRIP_COLORS.sub("", message)
                    match = RE_BUILD_OUTPUT.match(stripped)
                    if match:
                        match_type = match.lastgroup
                        if match_type == "error_block":
                            log_level = BUILD_ERROR
                        elif match_type in ("warning_block", "warning_num"):
                            log_level = logging.WARNING
                        elif match_type == "nsis_warning_block":
                            self._stdout_warning_lines_remaining = int(
                                match.group().split()[0]
                            )
                            log_level = logging.WARNING
                    elif self._stdout_warning_lines_remaining > 0:
                        self._stdout_warning_lines_remaining -= 1
                        log_level = logging.WARNING
                self.log(log_level, "build_output", {"line": message}, "{line}")
        elif state_changed:
            self._refresh_with_lock()

    def on_stderr_line(self, line):
        """Handle stderr output - parse for warnings/errors."""
        warning, state_changed, message = self.monitor.on_line(line)
        if message:
            if isinstance(message, logging.LogRecord):
                self.log_record("build_output", message)
            else:
                log_level = self._active_log_level or logging.WARNING

                if warning:
                    if warning["type"] == "error":
                        self._active_log_level = log_level = BUILD_ERROR
                    elif self._is_third_party_path(warning["filename"]):
                        self._active_log_level = log_level = THIRD_PARTY_WARNING
                    elif warning["flag"] and warning["flag"] in self._suppressed_flags:
                        self._active_log_level = log_level = SUPPRESSED_WARNING
                    else:
                        self._active_log_level = log_level = logging.WARNING
                elif isinstance(message, str):
                    stripped = RE_STRIP_COLORS.sub("", message)
                    match = RE_BUILD_OUTPUT.search(stripped)
                    if match:
                        match_type = match.lastgroup
                        if match_type in ("info_masm", "info_cargo"):
                            log_level = logging.INFO
                        elif match_type == "nsis_warning_block":
                            self._active_log_level = log_level = logging.WARNING
                        elif match_type in ("warning_summary", "warning_standalone"):
                            log_level = logging.WARNING
                            self._active_log_level = None
                        elif match_type in (
                            "error_summary",
                            "python_traceback",
                            "make_error",
                            "error_block",
                        ):
                            self._active_log_level = log_level = BUILD_ERROR
                        elif match_type in ("warning_block", "warning_num"):
                            if self._active_log_level != THIRD_PARTY_WARNING:
                                self._active_log_level = log_level = logging.WARNING
                        elif match_type == "continuation":
                            if self._active_log_level:
                                log_level = self._active_log_level
                        elif match.group("include_from"):
                            path = match.group("include_path")
                            self._active_log_level = log_level = (
                                THIRD_PARTY_WARNING
                                if self._is_third_party_path(path)
                                else logging.WARNING
                            )
                        elif match.group("gcc_include_continuation"):
                            path = match.group("gcc_from_path")
                            normed = mozpath.normsep(path)
                            is_relative = (
                                not normed.startswith(self.monitor.topsrcdir)
                                and not os.path.isabs(normed)
                                and not normed.startswith("/")
                            )
                            if is_relative:
                                # Relative paths are generated files, preserve current level
                                log_level = self._active_log_level or logging.WARNING
                            elif self._is_third_party_path(path):
                                self._active_log_level = log_level = THIRD_PARTY_WARNING
                            elif self._active_log_level == THIRD_PARTY_WARNING:
                                log_level = THIRD_PARTY_WARNING
                            else:
                                self._active_log_level = log_level = logging.WARNING
                        elif match.group("gcc_context"):
                            path = match.group("gcc_context_path")
                            self._active_log_level = log_level = (
                                THIRD_PARTY_WARNING
                                if self._is_third_party_path(path)
                                else logging.WARNING
                            )
                        elif match.group("inline_diagnostic"):
                            path = match.group("diag_path")
                            diag_type = match.group("diag_type").lower()
                            if diag_type == "error":
                                self._active_log_level = log_level = BUILD_ERROR
                            elif diag_type == "note":
                                log_level = self._active_log_level or logging.WARNING
                            elif self._is_third_party_path(path):
                                self._active_log_level = log_level = THIRD_PARTY_WARNING
                            else:
                                self._active_log_level = log_level = logging.WARNING

                self.log(log_level, "build_output", {"line": message}, "{line}")
        elif state_changed:
            self._refresh_with_lock()


class StaticAnalysisFooter:
    """Handles display of a static analysis progress indicator in a terminal."""

    def __init__(self, monitor):
        self.monitor = monitor

    def __rich__(self):
        monitor = self.monitor
        total = monitor.num_files
        processed = monitor.num_files_processed
        if total:
            percent = "(%.2f%%)" % (processed * 100.0 / total)
        else:
            percent = "(100%)"

        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("Processing ", style="bright_black")
        t.append(str(processed), style="yellow")
        t.append(" of ", style="bright_black")
        t.append(str(total), style="yellow")
        t.append(" files ", style="bright_black")
        t.append(percent, style="green")
        if monitor.current_file:
            t.append(" ")
            t.append(monitor.current_file, style="bold")
        return t


class StaticAnalysisOutputManager(OutputManager):
    """Handles writing static analysis output to a terminal or file."""

    def __init__(self, log_manager, monitor, footer):
        self.monitor = monitor
        self.raw = ""
        OutputManager.__init__(self, log_manager, footer)

    def on_line(self, line):
        warning, relevant = self.monitor.on_line(line)
        if relevant:
            self.raw += line + "\n"

        if warning:
            self.log(
                logging.INFO,
                "compiler_warning",
                warning,
                "Warning: {flag} in {filename}: {message}",
            )

        if relevant:
            self.log(logging.INFO, "build_output", {"line": line}, "{line}")
        else:
            self.refresh()

    def write(self, path, output_format):
        assert output_format in (
            "text",
            "json",
        ), f"Invalid output format {output_format}"
        path = mozpath.realpath(path)

        if output_format == "json":
            self.monitor._warnings_database.save_to_file(path)

        else:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(self.raw)

        self.log(
            logging.INFO,
            "write_output",
            {"path": path, "format": output_format},
            "Wrote {format} output in {path}",
        )


class CCacheStats:
    """Holds statistics from ccache.

    Instances can be subtracted from each other to obtain differences.
    print() or str() the object to show a ``ccache -s`` like output
    of the captured stats.

    """

    STATS_KEYS = [
        # (key, description)
        # Refer to stats.c in ccache project for all the descriptions.
        ("stats_zeroed", ("stats zeroed", "stats zero time")),
        ("stats_updated", "stats updated"),
        ("cache_hit_direct", "cache hit (direct)"),
        ("cache_hit_preprocessed", "cache hit (preprocessed)"),
        ("cache_hit_rate", "cache hit rate"),
        ("cache_miss", "cache miss"),
        ("link", "called for link"),
        ("preprocessing", "called for preprocessing"),
        ("multiple", "multiple source files"),
        ("stdout", "compiler produced stdout"),
        ("no_output", "compiler produced no output"),
        ("empty_output", "compiler produced empty output"),
        ("failed", "compile failed"),
        ("error", "ccache internal error"),
        ("preprocessor_error", "preprocessor error"),
        ("cant_use_pch", "can't use precompiled header"),
        ("compiler_missing", "couldn't find the compiler"),
        ("cache_file_missing", "cache file missing"),
        ("bad_args", "bad compiler arguments"),
        ("unsupported_lang", "unsupported source language"),
        ("compiler_check_failed", "compiler check failed"),
        ("autoconf", "autoconf compile/link"),
        ("unsupported_code_directive", "unsupported code directive"),
        ("unsupported_compiler_option", "unsupported compiler option"),
        ("out_stdout", "output to stdout"),
        ("out_device", "output to a non-regular file"),
        ("no_input", "no input file"),
        ("bad_extra_file", "error hashing extra file"),
        ("num_cleanups", "cleanups performed"),
        ("cache_files", "files in cache"),
        ("cache_size", "cache size"),
        ("cache_max_size", "max cache size"),
    ]

    SKIP_LINES = (
        "cache directory",
        "primary config",
        "secondary config",
    )

    STATS_KEYS_3_7_PLUS = {
        "stats_zeroed_timestamp": "stats_zeroed",
        "stats_updated_timestamp": "stats_updated",
        "direct_cache_hit": "cache_hit_direct",
        "preprocessed_cache_hit": "cache_hit_preprocessed",
        # "cache_hit_rate" is not provided
        "cache_miss": "cache_miss",
        "called_for_link": "link",
        "called_for_preprocessing": "preprocessing",
        "multiple_source_files": "multiple",
        "compiler_produced_stdout": "stdout",
        "compiler_produced_no_output": "no_output",
        "compiler_produced_empty_output": "empty_output",
        "compile_failed": "failed",
        "internal_error": "error",
        "preprocessor_error": "preprocessor_error",
        "could_not_use_precompiled_header": "cant_use_pch",
        "could_not_find_compiler": "compiler_missing",
        "missing_cache_file": "cache_file_missing",
        "bad_compiler_arguments": "bad_args",
        "unsupported_source_language": "unsupported_lang",
        "compiler_check_failed": "compiler_check_failed",
        "autoconf_test": "autoconf",
        "unsupported_code_directive": "unsupported_code_directive",
        "unsupported_compiler_option": "unsupported_compiler_option",
        "output_to_stdout": "out_stdout",
        "output_to_a_non_file": "out_device",
        "no_input_file": "no_input",
        "error_hashing_extra_file": "bad_extra_file",
        "cleanups_performed": "num_cleanups",
        "files_in_cache": "cache_files",
        "cache_size_kibibyte": "cache_size",
        # "cache_max_size" is obsolete and not printed anymore
    }

    ABSOLUTE_KEYS = {"cache_files", "cache_size", "cache_max_size"}
    FORMAT_KEYS = {"cache_size", "cache_max_size"}

    GiB = 1024**3
    MiB = 1024**2
    KiB = 1024

    def __init__(self, output=None, has_machine_format=False):
        """Construct an instance from the output of ccache -s."""
        self._values = {}

        if not output:
            return

        if has_machine_format:
            self._parse_machine_format(output)
        else:
            self._parse_human_format(output)

    def _parse_machine_format(self, output):
        for line in output.splitlines():
            line = line.strip()
            key, _, value = line.partition("\t")
            stat_key = self.STATS_KEYS_3_7_PLUS.get(key)
            if stat_key:
                value = int(value)
                if key.endswith("_kibibyte"):
                    value *= 1024
                self._values[stat_key] = value

        (direct, preprocessed, miss) = self.hit_rates()
        self._values["cache_hit_rate"] = (direct + preprocessed) * 100

    def _parse_human_format(self, output):
        for line in output.splitlines():
            line = line.strip()
            if line:
                self._parse_line(line)

    def _parse_line(self, line):
        for stat_key, stat_description in self.STATS_KEYS:
            if line.startswith(stat_description):
                raw_value = self._strip_prefix(line, stat_description)
                self._values[stat_key] = self._parse_value(raw_value)
                break
        else:
            if not line.startswith(self.SKIP_LINES):
                raise ValueError("Failed to parse ccache stats output: %s" % line)

    @staticmethod
    def _strip_prefix(line, prefix):
        if isinstance(prefix, tuple):
            for p in prefix:
                line = CCacheStats._strip_prefix(line, p)
            return line
        return line[len(prefix) :].strip() if line.startswith(prefix) else line

    @staticmethod
    def _parse_value(raw_value):
        try:
            # ccache calls strftime with '%c' (src/stats.c)
            ts = time.strptime(raw_value, "%c")
            return int(time.mktime(ts))
        except ValueError:
            if raw_value == "never":
                return 0
            pass

        value = raw_value.split()
        unit = ""
        if len(value) == 1:
            numeric = value[0]
        elif len(value) == 2:
            numeric, unit = value
        else:
            raise ValueError("Failed to parse ccache stats value: %s" % raw_value)

        if "." in numeric:
            numeric = float(numeric)
        else:
            numeric = int(numeric)

        if unit in ("GB", "Gbytes"):
            unit = CCacheStats.GiB
        elif unit in ("MB", "Mbytes"):
            unit = CCacheStats.MiB
        elif unit in ("KB", "Kbytes"):
            unit = CCacheStats.KiB
        else:
            unit = 1

        return int(numeric * unit)

    def hit_rate_message(self):
        return (
            "ccache (direct) hit rate: {:.1%}; (preprocessed) hit rate: {:.1%};"
            " miss rate: {:.1%}".format(*self.hit_rates())
        )

    def hit_rates(self):
        direct = self._values["cache_hit_direct"]
        preprocessed = self._values["cache_hit_preprocessed"]
        miss = self._values["cache_miss"]
        total = float(direct + preprocessed + miss)

        if total > 0:
            direct /= total
            preprocessed /= total
            miss /= total

        return (direct, preprocessed, miss)

    def __sub__(self, other):
        result = CCacheStats()

        for k, prefix in self.STATS_KEYS:
            if k not in self._values and k not in other._values:
                continue

            our_value = self._values.get(k, 0)
            other_value = other._values.get(k, 0)

            if k in self.ABSOLUTE_KEYS:
                result._values[k] = our_value
            else:
                result._values[k] = our_value - other_value

        return result

    def __str__(self):
        LEFT_ALIGN = 34
        lines = []

        for stat_key, stat_description in self.STATS_KEYS:
            if stat_key not in self._values:
                continue

            value = self._values[stat_key]

            if stat_key in self.FORMAT_KEYS:
                value = "%15s" % self._format_value(value)
            else:
                value = "%8u" % value

            if isinstance(stat_description, tuple):
                stat_description = stat_description[0]

            lines.append("%s%s" % (stat_description.ljust(LEFT_ALIGN), value))

        return "\n".join(lines)

    def __nonzero__(self):
        relative_values = [
            v for k, v in self._values.items() if k not in self.ABSOLUTE_KEYS
        ]
        return all(v >= 0 for v in relative_values) and any(
            v > 0 for v in relative_values
        )

    def __bool__(self):
        return self.__nonzero__()

    @staticmethod
    def _format_value(v):
        if v > CCacheStats.GiB:
            return "%.1f Gbytes" % (float(v) / CCacheStats.GiB)
        elif v > CCacheStats.MiB:
            return "%.1f Mbytes" % (float(v) / CCacheStats.MiB)
        else:
            return "%.1f Kbytes" % (float(v) / CCacheStats.KiB)

    @staticmethod
    def check_version_3_7_or_newer(ccache):
        output_version = subprocess.check_output(
            [ccache, "--version"], universal_newlines=True
        )
        return CCacheStats._is_version_3_7_or_newer(output_version)

    @staticmethod
    def _is_version_3_7_or_newer(output):
        if "ccache version" not in output:
            return False

        major = 0
        minor = 0

        for line in output.splitlines():
            version = re.search(r"ccache version (\d+).(\d+).*", line)
            if version:
                major = int(version.group(1))
                minor = int(version.group(2))
                break

        return ((major << 8) + minor) >= ((3 << 8) + 7)


class BuildDriver(MozbuildObject):
    """Provides a high-level API for build actions."""

    def __init__(self, *args, **kwargs):
        MozbuildObject.__init__(self, *args, virtualenv_name="build", **kwargs)
        self.metrics = None
        self.mach_context = None

    def build(
        self,
        metrics,
        what=None,
        jobs=0,
        job_size=0,
        directory=None,
        verbose=False,
        keep_going=False,
        mach_context=None,
        append_env=None,
        allow_subdirectory_build=False,
        no_completion_messages=False,
    ):
        self._ensure_build_log_dir_exists()
        warnings_path = self._get_build_log_filename(construct_log_filename("warnings"))
        monitor = self._spawn(BuildMonitor)
        monitor.init(warnings_path, self.log_manager.terminal, metrics)
        status = self._build(
            monitor,
            metrics,
            what,
            jobs,
            job_size,
            directory,
            verbose,
            keep_going,
            mach_context,
            append_env,
            allow_subdirectory_build,
            no_completion_messages,
        )

        record_usage = True

        # On automation, only record usage for plain `mach build`
        if "MOZ_AUTOMATION" in os.environ and what:
            record_usage = False

        if record_usage:
            monitor.record_usage()

        return status

    def _build(
        self,
        monitor,
        metrics,
        what=None,
        jobs=0,
        job_size=0,
        directory=None,
        verbose=False,
        keep_going=False,
        mach_context=None,
        append_env=None,
        allow_subdirectory_build=False,
        no_completion_messages=False,
    ):
        """Invoke the build backend.

        ``what`` defines the thing to build. If not defined, the default
        target is used.
        """
        self.metrics = metrics
        self.mach_context = mach_context
        footer = BuildProgressFooter(monitor)

        # Disable indexing in objdir because it is not necessary and can slow
        # down builds.
        mkdir(self.topobjdir, not_indexed=True)

        with BuildOutputManager(self.log_manager, monitor, footer) as output:
            monitor.start()

            if directory is not None and not what:
                self.log(
                    logging.ERROR,
                    "build_error",
                    {},
                    "Can only use -C/--directory with an explicit target name.",
                )
                return 1

            if directory is not None:
                directory = mozpath.normsep(directory)
                if directory.startswith("/"):
                    directory = directory[1:]

            monitor.start_resource_recording()

            dry_run_refresh = {"last": 0.0, "state": None}

            def on_dry_run_line(line):
                result = monitor.on_line_for_dry(line)
                state = (
                    len(monitor.planned_build_events),
                    len(monitor.planned_cargo_specs),
                    len(monitor.planned_tiers),
                )
                now = time.monotonic()
                if (
                    state != dry_run_refresh["state"]
                    and now - dry_run_refresh["last"] >= 0.2
                ):
                    output.refresh()
                    dry_run_refresh["state"] = state
                    dry_run_refresh["last"] = now
                return result

            if self._check_clobber(self.mozconfig, os.environ):
                return 1

            self.mach_context.command_attrs["clobber"] = False
            self.metrics.mozbuild.clobber.set(False)
            config = None
            try:
                config = self.config_environment
            except Exception:
                # If we don't already have a config environment this is either
                # a fresh objdir or $OBJDIR/config.status has been removed for
                # some reason, which indicates a clobber of sorts.
                self.mach_context.command_attrs["clobber"] = True
                self.metrics.mozbuild.clobber.set(True)

            # Record whether a clobber was requested so we can print
            # a special message later if the build fails.
            clobber_requested = False

            # Write out any changes to the current mozconfig in case
            # they should invalidate configure.
            self._write_mozconfig_json()

            previous_backend = None
            if config is not None:
                previous_backend = config.substs.get("BUILD_BACKENDS", [None])[0]

            config_rc = None
            # Even if we have a config object, it may be out of date
            # if something that influences its result has changed.
            if config is None or self.build_out_of_date(
                mozpath.join(self.topobjdir, "config.status"),
                mozpath.join(self.topobjdir, "config_status_deps.in"),
            ):
                if previous_backend and "Make" not in previous_backend:
                    clobber_requested = self._clobber_configure()

                if config is None:
                    self.log(
                        logging.INFO,
                        "build_output",
                        {},
                        "Config object not found by mach.",
                    )

                config_rc = self.configure(
                    metrics,
                    buildstatus_messages=True,
                    line_handler=output.on_stdout_line,
                    append_env=append_env,
                )

                if config_rc != 0:
                    return config_rc

                config = self.reload_config_environment()

            if config.substs.get("MOZ_USING_CCACHE"):
                ccache = config.substs.get("CCACHE")
                ccache_start = monitor.ccache_stats(ccache)
            else:
                ccache_start = None

            # Collect glean metrics
            substs = config.substs
            mozbuild_metrics = metrics.mozbuild
            mozbuild_metrics.compiler.set(substs.get("CC_TYPE", None))

            def get_substs_flag(name):
                return bool(substs.get(name, None))

            host = substs.get("host")
            monitor.resources.metadata["oscpu"] = host
            target = substs.get("target")
            if host != target:
                monitor.resources.metadata["abi"] = target

            product_name = substs.get("MOZ_BUILD_APP")
            app_displayname = substs.get("MOZ_APP_DISPLAYNAME")
            if app_displayname:
                product_name = app_displayname
                app_version = substs.get("MOZ_APP_VERSION")
                if app_version:
                    product_name += " " + app_version
            monitor.resources.metadata["product"] = product_name

            mozbuild_metrics.artifact.set(get_substs_flag("MOZ_ARTIFACT_BUILDS"))
            mozbuild_metrics.debug.set(get_substs_flag("MOZ_DEBUG"))
            mozbuild_metrics.opt.set(get_substs_flag("MOZ_OPTIMIZE"))
            mozbuild_metrics.ccache.set(get_substs_flag("CCACHE"))
            using_sccache = get_substs_flag("MOZ_USING_SCCACHE")
            mozbuild_metrics.sccache.set(using_sccache)
            mozbuild_metrics.project.set(substs.get("MOZ_BUILD_APP", ""))
            mozbuild_metrics.target.set(target)

            all_backends = config.substs.get("BUILD_BACKENDS", [None])
            active_backend = all_backends[0]

            status = None

            if not config_rc:
                self.ensure_backend_current()

            if (
                not what
                and "Make" in active_backend
                and not substs.get("MOZ_ARTIFACT_BUILDS")
                and (
                    clobber_requested
                    or self.mach_context.command_attrs.get("clobber", False)
                )
            ):
                monitor.load_native_compile_units(
                    mozpath.join(
                        self.topobjdir,
                        ".mozbuild",
                        "planned_native_compile_units.json",
                    )
                )

            jobs = self.resolve_num_jobs(jobs, job_size)

            if "Make" not in active_backend:
                backend_cls = get_backend_class(active_backend)(config)
                status = backend_cls.build(
                    self, output, jobs, verbose, what, append_env=append_env
                )

                if status and clobber_requested:
                    for line in CLOBBER_REQUESTED_MESSAGE.splitlines():
                        self.log(
                            logging.WARNING, "clobber", {"msg": line.rstrip()}, "{msg}"
                        )

            if what and status is None:
                # Collect target pairs.
                target_pairs = []
                for target in what:
                    path_arg = self._wrap_path_argument(target)

                    if directory is not None:
                        make_dir = mozpath.join(self.topobjdir, directory)
                        make_target = target
                    else:
                        make_dir, make_target = resolve_target_to_make(
                            self.topobjdir, path_arg.relpath()
                        )
                        if (
                            make_dir is not None
                            and not allow_subdirectory_build
                            and (Path(self.topsrcdir) / target).is_dir()
                        ):
                            message = "Build argument '{target}' is a subdirectory and was ignored."
                            # Don't tell agents how to override, because they do
                            # override
                            if not is_running_under_coding_agent():
                                message += (
                                    "\nUse --allow-subdirectory-build to override."
                                )

                            self.log(
                                logging.WARNING,
                                "build",
                                {"target": target},
                                message,
                            )
                            continue

                    if make_dir is None and make_target is None:
                        return 1

                    # See bug 886162 - we don't want to "accidentally" build
                    # the entire tree (if that's really the intent, it's
                    # unlikely they would have specified a directory.)
                    if not make_dir and not make_target:
                        self.log(
                            logging.ERROR,
                            "build_error",
                            {},
                            "The specified directory doesn't contain a "
                            "Makefile and the first parent with one is the "
                            "root of the tree. Please specify a directory "
                            "with a Makefile or run |mach build| if you "
                            "want to build the entire tree.",
                        )
                        return 1

                    target_pairs.append((make_dir, make_target))

                # Count target pairs
                monitor.dry_run_in_progress = True
                output.refresh()
                try:
                    for make_dir, make_target in target_pairs:
                        # We don't display build status messages during partial
                        # tree builds because they aren't reliable there. This
                        # could potentially be fixed if the build monitor were more
                        # intelligent about encountering undefined state.
                        no_build_status = "1" if make_dir is not None else ""
                        tgt_env = dict(append_env or {})
                        tgt_env["NO_BUILDSTATUS_MESSAGES"] = no_build_status
                        status = self._dry_run_make(
                            directory=make_dir,
                            target=make_target,
                            line_handler=on_dry_run_line,
                            stderr_line_handler=None,
                            log=False,
                            print_directory=False,
                            ensure_exit_code=False,
                            num_jobs=jobs,
                            job_size=job_size,
                            silent=not verbose,
                            append_env=tgt_env,
                            keep_going=keep_going,
                        )
                finally:
                    monitor.dry_run_in_progress = False
                    output.refresh()

                # Build target pairs.
                for make_dir, make_target in target_pairs:
                    # We don't display build status messages during partial
                    # tree builds because they aren't reliable there. This
                    # could potentially be fixed if the build monitor were more
                    # intelligent about encountering undefined state.
                    no_build_status = "1" if make_dir is not None else ""
                    tgt_env = dict(append_env or {})
                    tgt_env["NO_BUILDSTATUS_MESSAGES"] = no_build_status
                    status = self._run_make(
                        directory=make_dir,
                        target=make_target,
                        line_handler=output.on_stdout_line,
                        stderr_line_handler=output.on_stderr_line,
                        log=False,
                        print_directory=False,
                        ensure_exit_code=False,
                        num_jobs=jobs,
                        job_size=job_size,
                        silent=not verbose,
                        append_env=tgt_env,
                        keep_going=keep_going,
                    )

                    if status != 0:
                        break

            elif status is None:
                # If the backend doesn't specify a build() method, then just
                # call client.mk directly.
                # In automation, client.mk starts the sccache daemon, which
                # only reads its configuration from the environment when it
                # starts, so it needs the sccache variables in its environment.
                # config.mk covers the sub-makes.
                client_mk_env = dict(append_env or {})
                client_mk_env.update(
                    (name, value)
                    for name, value in config.substs.items()
                    if name.startswith("SCCACHE_")
                )
                client_mk_env["MOZBUILD_CARGO_JSON_OUTPUT"] = "1"
                client_mk_env["MOZBUILD_CARGO_JSON_RENDER_DIAGNOSTICS"] = "1"
                monitor.dry_run_in_progress = True
                output.refresh()
                try:
                    self._dry_run_client_mk(
                        line_handler=on_dry_run_line,
                        stderr_line_handler=on_dry_run_line,
                        jobs=jobs,
                        job_size=job_size,
                        verbose=verbose,
                        keep_going=keep_going,
                        append_env=client_mk_env,
                    )
                    if "compile" not in monitor.planned_tiers:
                        monitor._planning_tier = "compile"
                        self._dry_run_make(
                            target="compile",
                            line_handler=on_dry_run_line,
                            stderr_line_handler=on_dry_run_line,
                            log=False,
                            print_directory=False,
                            ensure_exit_code=False,
                            num_jobs=jobs,
                            job_size=job_size,
                            silent=not verbose,
                            append_env=client_mk_env,
                            keep_going=keep_going,
                        )
                    graph_env = dict(os.environ)
                    graph_env.update(client_mk_env)
                    monitor.collect_planned_cargo_units(graph_env)
                    output.refresh()
                finally:
                    monitor.dry_run_in_progress = False
                    output.refresh()
                status = self._run_client_mk(
                    line_handler=output.on_stdout_line,
                    stderr_line_handler=output.on_stderr_line,
                    jobs=jobs,
                    job_size=job_size,
                    verbose=verbose,
                    keep_going=keep_going,
                    append_env=client_mk_env,
                )

            self.log(
                logging.WARNING,
                "warning_summary",
                {"count": len(monitor.warnings_database)},
                "{count} compiler warnings present.",
            )

            # Try to run the active build backend's post-build step, if possible.
            try:
                active_backend = config.substs.get("BUILD_BACKENDS", [None])[0]
                if active_backend:
                    backend_cls = get_backend_class(active_backend)(config)
                    new_status = backend_cls.post_build(
                        self, output, jobs, verbose, status
                    )
                    status = new_status
            except Exception as ex:
                self.log(
                    logging.DEBUG,
                    "post_build",
                    {"ex": str(ex)},
                    "Unable to run active build backend's post-build step; "
                    + "failing the build due to exception: {ex}.",
                )
                if not status:
                    # If the underlying build provided a failing status, pass
                    # it through; otherwise, fail.
                    status = 1

            monitor.build_completed = status == 0
            if monitor.build_completed:
                monitor.completed_build_events.update(monitor.planned_build_events)
                monitor.completed_native_compile_units.update(
                    monitor.planned_native_compile_units
                )
                monitor.completed_cargo_units.update(monitor.planned_cargo_units)
            output.refresh()
            monitor.finish()

        if status == 0:
            usage = monitor.get_resource_usage()
            if usage:
                self.mach_context.command_attrs["usage"] = usage
                monitor.log_resource_usage(usage)

        # Print the collected compiler warnings. This is redundant with
        # inline output from the compiler itself. However, unlike inline
        # output, this list is sorted and grouped by file, making it
        # easier to triage output.
        #
        # Only do this if we had a successful build. If the build failed,
        # there are more important things in the log to look for than
        # whatever code we warned about.
        if not status:
            # Suppress warnings for 3rd party projects in local builds
            # until we suppress them for real.
            # TODO remove entries/feature once we stop generating warnings
            # in these directories.
            pathToThirdparty = mozpath.join(
                self.topsrcdir, "tools", "rewriting", "ThirdPartyPaths.txt"
            )

            pathToGenerated = mozpath.join(
                self.topsrcdir, "tools", "rewriting", "Generated.txt"
            )

            if os.path.exists(pathToThirdparty):
                with open(pathToThirdparty, encoding="utf-8", newline="\n") as f, open(
                    pathToGenerated, encoding="utf-8", newline="\n"
                ) as g:
                    # Normalize the path (no trailing /)
                    LOCAL_SUPPRESS_DIRS = tuple(
                        [line.strip("\n/") for line in f]
                        + [line.strip("\n/") for line in g]
                    )
            else:
                # For application based on gecko like thunderbird
                LOCAL_SUPPRESS_DIRS = ()

            suppressed_by_dir = Counter()

            THIRD_PARTY_CODE = "third-party code"
            suppressed = set(
                w.replace("-Wno-error=", "-W")
                for w in substs.get("WARNINGS_CFLAGS", [])
                + substs.get("WARNINGS_CXXFLAGS", [])
                if w.startswith("-Wno-error=")
            )
            warnings = []
            for warning in sorted(monitor.instance_warnings):
                path = mozpath.normsep(warning["filename"])
                if path.startswith(self.topsrcdir):
                    path = path[len(self.topsrcdir) + 1 :]

                warning["normpath"] = path

                if "MOZ_AUTOMATION" not in os.environ:
                    if path.startswith(LOCAL_SUPPRESS_DIRS):
                        suppressed_by_dir[THIRD_PARTY_CODE] += 1
                        continue

                    if warning["flag"] in suppressed:
                        suppressed_by_dir[mozpath.dirname(path)] += 1
                        continue

                warnings.append(warning)

            if THIRD_PARTY_CODE in suppressed_by_dir:
                suppressed_third_party_code = [
                    (THIRD_PARTY_CODE, suppressed_by_dir.pop(THIRD_PARTY_CODE))
                ]
            else:
                suppressed_third_party_code = []
            for d, count in suppressed_third_party_code + sorted(
                suppressed_by_dir.items()
            ):
                self.log(
                    logging.WARNING,
                    "suppressed_warning",
                    {"dir": d, "count": count},
                    "(suppressed {count} warnings in {dir})",
                )

            for warning in warnings:
                if warning["column"] is not None:
                    self.log(
                        logging.WARNING,
                        "compiler_warning",
                        warning,
                        "warning: {normpath}:{line}:{column} [{flag}] {message}",
                    )
                else:
                    self.log(
                        logging.WARNING,
                        "compiler_warning",
                        warning,
                        "warning: {normpath}:{line} [{flag}] {message}",
                    )

        high_finder, finder_percent = monitor.have_high_finder_usage()
        if high_finder:
            print(FINDER_SLOW_MESSAGE % finder_percent)

        if config.substs.get("MOZ_USING_CCACHE"):
            ccache_end = monitor.ccache_stats(ccache)
        else:
            ccache_end = None

        ccache_diff = None
        if ccache_start and ccache_end:
            ccache_diff = ccache_end - ccache_start
            if ccache_diff:
                self.log(
                    logging.INFO,
                    "ccache",
                    {"msg": ccache_diff.hit_rate_message()},
                    "{msg}",
                )

        notify_minimum_time = 300
        try:
            notify_minimum_time = int(os.environ.get("MACH_NOTIFY_MINTIME", "300"))
        except ValueError:
            # Just stick with the default
            pass

        if not no_completion_messages and monitor.elapsed > notify_minimum_time:
            # Display a notification when the build completes.
            self.notify("Build complete" if not status else "Build failed")

        if status:
            if (
                not no_completion_messages
                and what
                and any([
                    target for target in what if target not in ("faster", "binaries")
                ])
            ):
                print(
                    "Hey! Builds initiated with `mach build "
                    "$A_SPECIFIC_TARGET` may not always work, even if the "
                    "code being built is correct. Consider doing a bare "
                    "`mach build` instead."
                )
            return status

        if not no_completion_messages:
            self._print_build_completion_messages(monitor, what, using_sccache)

        return status

    def _print_build_completion_messages(self, monitor, what, using_sccache):
        if monitor.have_resource_usage:
            excessive, swap_in, swap_out = monitor.have_excessive_swapping()
            # if excessive:
            #    print(EXCESSIVE_SWAP_MESSAGE)

            self.log(
                logging.INFO,
                "build_output",
                {},
                "To view a profile of the build, run |mach resource-usage|.",
            )

        long_build = monitor.elapsed > 1200

        if long_build:
            self.log(
                logging.INFO,
                "build_output",
                {},
                "We know it took a while, but your build finally finished successfully!",
            )
            if not using_sccache:
                self.log(
                    logging.INFO,
                    "build_output",
                    {},
                    "If you are building Firefox often, SCCache can save you a lot "
                    "of time. You can learn more here: "
                    "https://firefox-source-docs.mozilla.org/setup/"
                    "configuring_build_options.html#sccache",
                )
        else:
            self.log(logging.INFO, "build_output", {}, "Your build was successful!")

        # Only for full builds because incremental builders likely don't
        # need to be burdened with this.
        if not what:
            try:
                # Fennec doesn't have useful output from just building. We should
                # arguably make the build action useful for Fennec. Another day...
                if self.substs["MOZ_BUILD_APP"] != "mobile/android":
                    self.log(
                        logging.INFO,
                        "build_output",
                        {},
                        "To take your build for a test drive, run: |mach run|",
                    )
                app = self.substs["MOZ_BUILD_APP"]
                if app in ("browser", "mobile/android"):
                    self.log(
                        logging.INFO,
                        "build_output",
                        {},
                        "For more information on what to do now, see "
                        "https://firefox-source-docs.mozilla.org/setup/contributing_code.html",
                    )
            except Exception:
                # Ignore Exceptions in case we can't find config.status (such
                # as when doing OSX Universal builds)
                pass

    def configure(
        self,
        metrics,
        options=None,
        buildstatus_messages=False,
        line_handler=None,
        append_env=None,
    ):
        # Disable indexing in objdir because it is not necessary and can slow
        # down builds.
        self.metrics = metrics
        mkdir(self.topobjdir, not_indexed=True)
        self._write_mozconfig_json()

        def on_line(line):
            self.log(logging.INFO, "build_output", {"line": line}, "{line}")

        line_handler = line_handler or on_line

        append_env = dict(append_env or {})

        # Back when client.mk was used, `mk_add_options "export ..."` lines
        # from the mozconfig would spill into the configure environment, so
        # add that for backwards compatibility.
        for line in self.mozconfig["make_extra"] or []:
            if line.startswith("export "):
                k, eq, v = line[len("export ") :].partition("=")
                if eq == "=":
                    append_env[k] = v

        build_site = CommandSiteManager.from_environment(
            self.topsrcdir,
            lambda: get_state_dir(specific_to_topsrcdir=True, topsrcdir=self.topsrcdir),
            "build",
            get_virtualenv_base_dir(self.topsrcdir),
        )
        build_site.ensure()

        command = [build_site.python_path, mozpath.join(self.topsrcdir, "configure.py")]
        if options:
            command.extend(options)

        if buildstatus_messages:
            append_env["MOZ_CONFIGURE_BUILDSTATUS"] = "1"
            line_handler("BUILDSTATUS TIERS configure")
            line_handler("BUILDSTATUS TIER_START configure")

        env = os.environ.copy()
        env.update(append_env)

        with subprocess.Popen(
            command,
            cwd=self.topobjdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        ) as process:
            for line in process.stdout:
                line_handler(line.rstrip())
            status = process.wait()
        if buildstatus_messages:
            line_handler("BUILDSTATUS TIER_FINISH configure")
        # config.log is written in the objdir, which automation doesn't upload.
        if upload_path := os.environ.get("UPLOAD_PATH"):
            try:
                mkdir(upload_path)
                shutil.copy2(
                    Path(self.topobjdir) / "config.log",
                    Path(upload_path) / "config.log",
                )
            except OSError:
                pass
        if status:
            self.log(
                BUILD_ERROR,
                "configure_error",
                {},
                '*** Fix above errors and then restart with "./mach build"',
            )
        else:
            self.log(logging.INFO, "configure_complete", {}, "Configure complete!")
            self.log(
                logging.INFO,
                "configure_complete",
                {},
                "Be sure to run |mach build| to pick up any changes",
            )

        return status

    def install_tests(self, force=False):
        """Install test files."""

        if self.is_clobber_needed():
            print(
                INSTALL_TESTS_CLOBBER.format(
                    clobber_file=mozpath.join(self.topobjdir, "CLOBBER")
                )
            )
            sys.exit(1)

        skipped = install_test_files(
            mozpath.normpath(self.topsrcdir),
            self.topobjdir,
            "_tests",
            force=force,
        )
        if skipped:
            print(
                "Skipping test file installation (up to date). "
                "Run with --force to force reinstallation.",
                file=sys.stderr,
            )

    def _clobber_configure(self):
        # This is an optimistic treatment of the CLOBBER file for when we have
        # some trust in the build system: an update to the CLOBBER file is
        # interpreted to mean that configure will fail during an incremental
        # build, which is handled by removing intermediate configure artifacts
        # and subsections of the objdir related to python and testing before
        # proceeding.
        clobberer = Clobberer(self.topsrcdir, self.topobjdir)
        clobber_output = io.StringIO()
        res = clobberer.maybe_do_clobber(os.getcwd(), False, clobber_output)
        required, performed, message = res
        assert not performed
        if not required:
            return False

        def remove_objdir_path(path):
            path = mozpath.join(self.topobjdir, path)
            self.log(
                logging.WARNING,
                "clobber",
                {"path": path},
                "CLOBBER file has been updated, removing {path}.",
            )
            mozfile.remove(path)

        # Remove files we think could cause "configure" clobber bugs.
        for f in ("old-configure.vars", "config.cache", "configure.pkl"):
            remove_objdir_path(f)
            remove_objdir_path(mozpath.join("js", "src", f))

        rm_dirs = [
            # Stale paths in our virtualenv may cause build-backend
            # to fail.
            "_virtualenvs",
            # Some tests may accumulate state in the objdir that may
            # become invalid after srcdir changes.
            "_tests",
        ]

        for d in rm_dirs:
            remove_objdir_path(d)

        os.utime(mozpath.join(self.topobjdir, "CLOBBER"), None)
        return True

    def _write_mozconfig_json(self):
        mozconfig_json = mozpath.join(self.topobjdir, ".mozconfig.json")
        with FileAvoidWrite(mozconfig_json) as fh:
            to_write = json.dumps(
                {
                    "topsrcdir": self.topsrcdir,
                    "topobjdir": self.topobjdir,
                    "mozconfig": self.mozconfig,
                },
                sort_keys=True,
                indent=2,
            )

            # json.dumps in python2 inserts some trailing whitespace while
            # json.dumps in python3 does not, which defeats the FileAvoidWrite
            # mechanism. Strip the trailing whitespace to avoid rewriting this
            # file unnecessarily.
            to_write = "\n".join([line.rstrip() for line in to_write.splitlines()])
            fh.write(to_write)

    def _run_client_mk(
        self,
        target=None,
        line_handler=None,
        stderr_line_handler=None,
        jobs=0,
        job_size=0,
        verbose=None,
        keep_going=False,
        append_env=None,
    ):
        append_env = dict(append_env or {})
        append_env["TOPSRCDIR"] = self.topsrcdir

        append_env["CONFIG_GUESS"] = self.resolve_config_guess()

        mozconfig = self.mozconfig

        mozconfig_make_lines = []
        for arg in mozconfig["make_extra"] or []:
            mozconfig_make_lines.append(arg)

        if mozconfig["make_flags"]:
            mozconfig_make_lines.append(
                "MOZ_MAKE_FLAGS=%s" % " ".join(mozconfig["make_flags"])
            )
        objdir = mozpath.normsep(self.topobjdir)
        mozconfig_make_lines.append("MOZ_OBJDIR=%s" % objdir)
        mozconfig_make_lines.append("OBJDIR=%s" % objdir)

        if mozconfig["path"]:
            mozconfig_make_lines.append(
                "FOUND_MOZCONFIG=%s" % mozpath.normsep(mozconfig["path"])
            )
            mozconfig_make_lines.append("export FOUND_MOZCONFIG")

        # The .mozconfig.mk file only contains exported variables and lines with
        # UPLOAD_EXTRA_FILES.
        mozconfig_filtered_lines = [
            line
            for line in mozconfig_make_lines
            # Bug 1418122 investigate why UPLOAD_EXTRA_FILES is special and
            # remove it.
            if line.startswith("export ") or "UPLOAD_EXTRA_FILES" in line
        ]

        mozconfig_client_mk = mozpath.join(self.topobjdir, ".mozconfig-client-mk")
        with FileAvoidWrite(mozconfig_client_mk) as fh:
            fh.write("\n".join(mozconfig_make_lines))

        mozconfig_mk = mozpath.join(self.topobjdir, ".mozconfig.mk")
        with FileAvoidWrite(mozconfig_mk) as fh:
            fh.write("\n".join(mozconfig_filtered_lines))

        # Copy the original mozconfig to the objdir.
        mozconfig_objdir = mozpath.join(self.topobjdir, ".mozconfig")
        if mozconfig["path"]:
            with open(mozconfig["path"], "rb") as ifh:
                with FileAvoidWrite(mozconfig_objdir) as ofh:
                    ofh.write(ifh.read())
        else:
            try:
                os.unlink(mozconfig_objdir)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise

        if mozconfig_make_lines:
            self.log(
                logging.WARNING,
                "mozconfig_content",
                {
                    "path": mozconfig["path"],
                    "content": "\n    ".join(mozconfig_make_lines),
                },
                "Adding make options from {path}\n    {content}",
            )

        append_env["OBJDIR"] = mozpath.normsep(self.topobjdir)

        return self._run_make(
            srcdir=True,
            filename="client.mk",
            ensure_exit_code=False,
            print_directory=False,
            target=target,
            line_handler=line_handler,
            stderr_line_handler=stderr_line_handler,
            log=False,
            num_jobs=jobs,
            job_size=job_size,
            silent=not verbose,
            keep_going=keep_going,
            append_env=append_env,
        )

    def _dry_run_client_mk(
        self,
        target=None,
        line_handler=None,
        stderr_line_handler=None,
        jobs=0,
        job_size=0,
        verbose=None,
        keep_going=False,
        append_env=None,
    ):
        append_env = dict(append_env or {})
        append_env["TOPSRCDIR"] = self.topsrcdir

        append_env["CONFIG_GUESS"] = self.resolve_config_guess()

        mozconfig = self.mozconfig

        mozconfig_make_lines = []
        for arg in mozconfig["make_extra"] or []:
            mozconfig_make_lines.append(arg)

        if mozconfig["make_flags"]:
            mozconfig_make_lines.append(
                "MOZ_MAKE_FLAGS=%s" % " ".join(mozconfig["make_flags"])
            )
        objdir = mozpath.normsep(self.topobjdir)
        mozconfig_make_lines.append("MOZ_OBJDIR=%s" % objdir)
        mozconfig_make_lines.append("OBJDIR=%s" % objdir)

        if mozconfig["path"]:
            mozconfig_make_lines.append(
                "FOUND_MOZCONFIG=%s" % mozpath.normsep(mozconfig["path"])
            )
            mozconfig_make_lines.append("export FOUND_MOZCONFIG")

        # The .mozconfig.mk file only contains exported variables and lines with
        # UPLOAD_EXTRA_FILES.
        mozconfig_filtered_lines = [
            line
            for line in mozconfig_make_lines
            # Bug 1418122 investigate why UPLOAD_EXTRA_FILES is special and
            # remove it.
            if line.startswith("export ") or "UPLOAD_EXTRA_FILES" in line
        ]

        mozconfig_client_mk = mozpath.join(self.topobjdir, ".mozconfig-client-mk")
        with FileAvoidWrite(mozconfig_client_mk) as fh:
            fh.write("\n".join(mozconfig_make_lines))

        mozconfig_mk = mozpath.join(self.topobjdir, ".mozconfig.mk")
        with FileAvoidWrite(mozconfig_mk) as fh:
            fh.write("\n".join(mozconfig_filtered_lines))

        # Copy the original mozconfig to the objdir.
        mozconfig_objdir = mozpath.join(self.topobjdir, ".mozconfig")
        if mozconfig["path"]:
            with open(mozconfig["path"], "rb") as ifh:
                with FileAvoidWrite(mozconfig_objdir) as ofh:
                    ofh.write(ifh.read())
        else:
            try:
                os.unlink(mozconfig_objdir)
            except OSError as e:
                if e.errno != errno.ENOENT:
                    raise

        if mozconfig_make_lines:
            self.log(
                logging.WARNING,
                "mozconfig_content",
                {
                    "path": mozconfig["path"],
                    "content": "\n    ".join(mozconfig_make_lines),
                },
                "Adding make options from {path}\n    {content}",
            )

        append_env["OBJDIR"] = mozpath.normsep(self.topobjdir)

        return self._dry_run_make(
            srcdir=True,
            filename="client.mk",
            ensure_exit_code=False,
            print_directory=False,
            target=target,
            line_handler=line_handler,
            stderr_line_handler=stderr_line_handler,
            log=False,
            num_jobs=jobs,
            job_size=job_size,
            silent=not verbose,
            keep_going=keep_going,
            append_env=append_env,
        )

    def _check_clobber(self, mozconfig, env):
        """Run `Clobberer.maybe_do_clobber`, log the result and return a status bool.

        Wraps the clobbering logic in `Clobberer.maybe_do_clobber` to provide logging
        and handling of the `AUTOCLOBBER` mozconfig option.

        Return a bool indicating whether the clobber reached an error state. For example,
        return `True` if the clobber was required but not completed, and return `False` if
        the clobber was not required and not completed.
        """
        auto_clobber = any([
            env.get("AUTOCLOBBER", False),
            (mozconfig["env"] or {}).get("added", {}).get("AUTOCLOBBER", False),
            "AUTOCLOBBER=1" in (mozconfig["make_extra"] or []),
        ])
        from mozbuild.base import BuildEnvironmentNotFoundException

        substs = dict()
        try:
            substs = self.substs
        except BuildEnvironmentNotFoundException:
            # We'll just use an empty substs if there is no config.
            pass
        clobberer = Clobberer(self.topsrcdir, self.topobjdir, substs)
        clobber_output = io.StringIO()
        res = clobberer.maybe_do_clobber(os.getcwd(), auto_clobber, clobber_output)
        clobber_output.seek(0)
        for line in clobber_output.readlines():
            msg = line.rstrip()
            # Log "Clobber not needed" at INFO level, actual clobber actions at WARNING
            level = logging.INFO if msg == "Clobber not needed." else logging.WARNING
            self.log(level, "clobber", {"msg": msg}, "{msg}")

        clobber_required, clobber_performed, clobber_message = res
        if clobber_required and not clobber_performed:
            for line in clobber_message.splitlines():
                self.log(logging.WARNING, "clobber", {"msg": line.rstrip()}, "{msg}")
            return True

        if clobber_performed and env.get("TINDERBOX_OUTPUT"):
            self.log(
                logging.WARNING,
                "clobber",
                {"msg": "TinderboxPrint: auto clobber"},
                "{msg}",
            )

        return False
