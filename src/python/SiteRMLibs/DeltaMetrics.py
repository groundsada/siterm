#!/usr/bin/env python3
"""Delta lifecycle signals, derived from the append-only state history.

SiteRM already keeps what most of this needs. `stateChangerDelta` is the single
choke point for every delta transition and inserts one row per transition into
`states`; `hoststateshistory` does the same per host. Nothing ever read them
back out. So flow, dwell and time-to-provision are an exporter, not
instrumentation.

Counters are advanced INCREMENTALLY from a row-id watermark rather than
recomputed as `SELECT count(*)`. Retention is 7 days and DBCleaner sweeps every
6 minutes, so a recomputed total would start falling once rows aged out, and a
Prometheus counter that falls reads as a reset -- turning ordinary retention
into a rate() spike. Advancing by "rows I have not seen yet" is monotonic, and
a process restart is a real counter reset that Prometheus already handles.

Cardinality is capped twice over. `siterm_delta_stuck_seconds` carries a
deltaid, which is unbounded over the retention window, so only the oldest few
are emitted -- and it is an OBSERVABLE gauge, because a synchronous one keeps
reporting every attribute set it has ever been given, which would cap what is
written per cycle without capping what is exported.
"""

from SiteRMLibs.OtelMetrics import (
    getCounter,
    getGauge,
    getHistogram,
    getObservableGauge,
    observation,
)

# Nothing leaves these, so a delta sitting in one is not stuck.
TERMINAL_STATES = {"activated", "failed", "removed", "cancelled", "deactivated"}

ERROR_STATES = {"failed", "activate-error", "deactivate-error"}

# Upper bound on siterm_delta_stuck_seconds series per cycle.
MAX_STUCK_SERIES = 20


class DeltaMetrics:  # pylint: disable=too-many-instance-attributes
    """Reads the state history and records the lifecycle signals.

    One instance per exporter process; it keeps the watermarks that make the
    counters monotonic.
    """

    # pylint: disable=too-few-public-methods

    def __init__(self, dbI):
        self.dbI = dbI
        self._stateWatermark = 0
        self._hostWatermark = 0
        # deltaid -> (state, insertdate) of the last row already folded in, so a
        # dwell can be closed by a transition seen in a later cycle.
        self._open = {}
        self._firstSeen = {}
        self._activated = set()
        # (seconds, {deltaid, state}) for the current cycle only. Read by the
        # observable gauge below, which is what keeps deltaid bounded.
        self._stuck = []
        self._stuckRegistered = False
        self._openHosts = {}

    @staticmethod
    def _rows(dbI, table, watermark):
        """New rows in `table` above `watermark`, oldest first."""
        rows = dbI.get(table, search=[["id", ">", watermark]], orderby=["id", "ASC"], limit=5000)
        return rows or []

    def _advance(self, rows, transitions, dwell, timeToActive, errors):
        """Fold new `states` rows into the flow signals."""
        highest = self._stateWatermark
        for row in rows:
            highest = max(highest, row["id"])
            deltaid, state, when = row["deltaid"], row["state"], row["insertdate"]
            previous = self._open.get(deltaid)
            if previous is None:
                self._firstSeen.setdefault(deltaid, when)
                transitions.add(1, {"from": "none", "to": state})
            else:
                prevState, prevWhen = previous
                transitions.add(1, {"from": prevState, "to": state})
                # Dwell is attributed to the state being LEFT, which is the one
                # the time was actually spent in.
                dwell.record(max(0, when - prevWhen), {"state": prevState})
            if state in ERROR_STATES:
                errors.add(1, {"state": state})
            if state == "activated" and deltaid not in self._activated:
                self._activated.add(deltaid)
                started = self._firstSeen.get(deltaid)
                if started is not None:
                    timeToActive.record(max(0, when - started), {})
            self._open[deltaid] = (state, when)
        self._stateWatermark = highest

    def _advanceHosts(self, rows, hostTransitions):
        """Fold new `hoststateshistory` rows into the per-host flow counter."""
        highest = self._hostWatermark
        seen = self._openHosts
        for row in rows:
            highest = max(highest, row["id"])
            key = (row["deltaid"], row["hostname"])
            previous = seen.get(key)
            hostTransitions.add(1, {"from": previous or "none", "to": row["state"]})
            seen[key] = row["state"]
        self._hostWatermark = highest

    def _observeStuck(self, _options):
        """Yield only this cycle's stuck deltas.

        A synchronous gauge would keep reporting every deltaid it had ever been
        given, so MAX_STUCK_SERIES would cap what is written per cycle without
        capping what is exported. An observable gauge reports only what is
        yielded here, so a delta that finishes stops producing a series.
        """
        out = []
        for value, attrs in self._stuck:
            obs = observation(value, attrs)
            if obs is not None:
                out.append(obs)
        return out

    def _occupancy(self, now, inState):
        """Current occupancy, and how long the oldest non-terminal deltas have sat."""
        deltas = self.dbI.get("deltas", limit=5000) or []
        counts = {}
        pending = []
        for delta in deltas:
            counts[delta["state"]] = counts.get(delta["state"], 0) + 1
            if delta["state"] not in TERMINAL_STATES:
                pending.append(delta)
        for state, count in counts.items():
            inState.set(count, {"state": state})
        pending.sort(key=lambda d: d["updatedate"])
        self._stuck = [(max(0, now - d["updatedate"]), {"deltaid": d["uid"], "state": d["state"]}) for d in pending[:MAX_STUCK_SERIES]]

    def collect(self, now):
        """Record every lifecycle signal once. Never raises.

        Called from the SNMPMonitoring cycle, so it inherits that cadence and
        its freshness lag -- which siterm_metrics_generated_timestamp_seconds
        now makes visible.
        """
        try:
            transitions = getCounter("siterm_delta_transitions_total", "Delta state transitions")
            hostTransitions = getCounter("siterm_host_delta_transitions_total", "Per-host delta state transitions")
            errors = getCounter("siterm_delta_errors_total", "Delta transitions into an error state")
            dwell = getHistogram("siterm_delta_state_dwell_seconds", "How long a delta spent in a state before leaving it")
            timeToActive = getHistogram("siterm_delta_time_to_activated_seconds", "First seen to activated")
            inState = getGauge("siterm_deltas_in_state", "Deltas currently in each state")
            if not self._stuckRegistered:
                getObservableGauge("siterm_delta_stuck_seconds", self._observeStuck, "Age of the oldest non-terminal deltas")
                self._stuckRegistered = True

            self._advance(self._rows(self.dbI, "states", self._stateWatermark), transitions, dwell, timeToActive, errors)
            self._advanceHosts(self._rows(self.dbI, "hoststateshistory", self._hostWatermark), hostTransitions)
            self._occupancy(now, inState)
        except Exception as ex:  # pylint: disable=broad-except
            print(f"Delta lifecycle metrics skipped this cycle. Error: {ex}")
