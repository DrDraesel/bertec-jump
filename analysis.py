"""Bounded, dependency-free streaming vertical force / jump estimates.

Methodology: McMahon, Suchomel, Lake & Comfort (2018), Understanding the
Key Phases of the Countermovement Jump Force-Time Curve, Strength and
Conditioning Journal (author manuscript):
https://eprints.chi.ac.uk/3266/1/UNDERSTANDINGTHEKEYPHASESOFTHECOUNTERMOVEMENTJUMPFORCE-TIMECURVE.pdf
Use BW = mean quiet-standing Fz, m = BW/g, a = (Fz-BW)/m; trapezoidal
integration from initial v=0. External vertical COM power is Fz*v, NOT
net-force power, joint power or metabolic power. Flight height g*T*T/8
assumes identical COM height at takeoff and landing; leg tuck, stepping off,
hand support, load changes, calibration/zero error and threshold selection
can invalidate estimates. This is not a validated clinical instrument.

Implementation heuristics (not manufacturer approval): >=2 s standing,
100..2500 N, SD <= max(5 N, 1% BW); movement > max(20 N, 5% BW,
5 standing SD); unloaded <=20 N with 10 ms continuous debounce each edge.
Event times are FIRST threshold samples (no debounce timing bias, no
subsample interpolation). Positive takeoff velocity is required. Flight
must be 50 ms..2 s. Re-arm requires another 2 s quiet standing. Feed EVERY
hardware sample with hardware time seconds, never UI-decimated frames.
Gaps >20 ms, nonmonotonic timestamps, and nonfinite channels invalidate
all current derived results and require a fresh baseline. Counts of already
completed jumps survive faults, but explicit reset clears everything.

Default jump_enabled=False is a safety gate: no velocity/power/jump metrics.
Only enable externally after verifying the plate is impact-rated and the
entire installation is approved. This module never discovers or approves
hardware. Left/right are passed through only if actually supplied.
Caller owns synchronization. Each returned dict is a defensive copy.
Peak force spans the current valid session; peak power is positive external
COM power during the most recent movement/flight (landing excluded).
Memory is bounded to 20,001 quiet-standing samples; excessive sampling
rates fail to establish a 2 s baseline rather than grow memory unbounded.

Body weight is the mean vertical force of the most recent >=2 s still-standing
window on a zeroed plate: weight_n = Fz_mean, weight_kg = weight_n/g,
weight_lb = weight_kg*2.2046226218487757, weight_sd_n = the window SD. It is a
measured force divided by g, not a certified scale reading. A different person
or any added/removed mass needs a fresh stable window; every invalidation
clears the weight. With jumps disabled the window is re-verified continuously
while standing; with jumps enabled it is captured during each baseline/landing
phase. The first window after an abrupt load change can include a small tail of
the previous load inside the stability gate; the next full 2 s window converges
on the new stable force (within about four seconds of the change).
"""
from collections import deque
import math

G = 9.80665
LB_PER_KG = 2.2046226218487757


class Analyzer:
    def __init__(self, jump_enabled=False):
        self.jump_enabled = bool(jump_enabled)
        self.reset()

    def reset(self):
        """Clear session state without changing the construction safety gate."""
        self._state = dict(phase='baseline', mass_kg=None, fz_n=None,
                           left_n=None, right_n=None, velocity_m_s=None,
                           power_w=None, peak_power_w=None, peak_force_n=None,
                           jump_height_cm=None, flight_time_ms=None,
                           jump_count=0, quality='awaiting_quiet_standing',
                           weight_n=None, weight_kg=None, weight_lb=None,
                           weight_sd_n=None)
        self._last_t = self._last_f = None
        self._window = deque()
        self._sum = self._sum_sq = 0.0
        self._bw = self._sd = None
        self._velocity = 0.0
        self._movement_t = self._unloaded_t = self._landing_t = None
        self._flight_t = self._takeoff_v = None

    def snapshot(self):
        return self._state.copy()

    def _invalidate(self, reason):
        count = self._state['jump_count']
        self.reset()
        self._state.update(jump_count=count, quality='invalid_' + reason)
        return self.snapshot()

    def _clear_window(self):
        self._window.clear()
        self._sum = self._sum_sq = 0.0

    def _baseline(self, t, force):
        if not 100.0 <= force <= 2500.0:
            self._clear_window()
            return
        self._window.append((t, force))
        self._sum += force
        self._sum_sq += force * force
        while (len(self._window) > 20001 or
               (len(self._window) > 1 and
                t - self._window[1][0] >= 2.0 - 1e-9)):
            _, old = self._window.popleft()
            self._sum -= old
            self._sum_sq -= old * old
        if t - self._window[0][0] < 2.0 - 1e-9:
            return
        mean = self._sum / len(self._window)
        sd = math.sqrt(max(0.0, self._sum_sq / len(self._window) - mean * mean))
        if sd > max(5.0, .01 * mean):
            return
        self._bw, self._sd = mean, sd
        self._velocity = 0.0
        self._state.update(mass_kg=mean / G, phase='standing',
                           weight_n=mean, weight_kg=mean / G,
                           weight_lb=mean / G * LB_PER_KG, weight_sd_n=sd)
        if self.jump_enabled:
            self._state.update(velocity_m_s=0.0, power_w=0.0,
                               quality='estimate_ready')
        else:
            self._state['quality'] = 'jump_disabled_force_only'
        self._clear_window()

    def feed(self, t: float, fz_n: float, left_n=None, right_n=None) -> dict:
        """Consume an upward-positive Newton sample and hardware time seconds."""
        try:
            valid = math.isfinite(t) and math.isfinite(fz_n)
            valid = valid and all(x is None or math.isfinite(x)
                                  for x in (left_n, right_n))
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            return self._invalidate('nonfinite_input')
        dt = None if self._last_t is None else t - self._last_t
        if dt is not None and (dt <= 0.0 or dt > .020 + 1e-12):
            return self._invalidate('timestamp')
        previous_force = self._last_f
        self._last_t, self._last_f = t, fz_n
        s = self._state
        s.update(fz_n=fz_n, left_n=left_n, right_n=right_n,
                 peak_force_n=max(fz_n, s['peak_force_n'])
                 if s['peak_force_n'] is not None else fz_n)
        if s['phase'] in ('baseline', 'landing'):
            self._baseline(t, fz_n)
            return self.snapshot()
        if not self.jump_enabled:
            # Jumps disabled: keep re-verifying body weight from still standing.
            # The 2 s window only establishes on a stable, loaded, in-range plate.
            self._baseline(t, fz_n)
            if s['phase'] == 'standing':
                s['quality'] = 'jump_disabled_force_only'
            return self.snapshot()
        if s['phase'] == 'standing':
            if abs(fz_n - self._bw) <= max(20.0, .05 * self._bw, 5 * self._sd):
                return self.snapshot()
            s.update(phase='movement', jump_height_cm=None, flight_time_ms=None,
                     peak_power_w=0.0, quality='estimate_integration')
            self._movement_t = t
            self._unloaded_t = self._landing_t = self._flight_t = None
            self._velocity = 0.0
        # The previous sample is quiet standing at the first movement edge.
        self._velocity += ((previous_force - self._bw) + (fz_n - self._bw)) / (
            2 * s['mass_kg']) * dt
        power = fz_n * self._velocity
        if not math.isfinite(self._velocity) or not math.isfinite(power):
            return self._invalidate('numeric_overflow')
        s.update(velocity_m_s=self._velocity, power_w=power)
        if s['phase'] == 'movement':
            s['peak_power_w'] = max(s['peak_power_w'], power)
            if fz_n <= 20.0:
                if self._unloaded_t is None:
                    self._unloaded_t, self._takeoff_v = t, self._velocity
                if t - self._unloaded_t >= .010 - 1e-9:
                    if self._takeoff_v <= .05:
                        return self._invalidate('no_upward_takeoff')
                    self._flight_t = self._unloaded_t
                    s.update(phase='flight', quality='estimate_flight')
            else:
                self._unloaded_t = None
            if t - self._movement_t > 5.0:
                return self._invalidate('movement_timeout')
        elif s['phase'] == 'flight':
            if fz_n > 20.0:
                if self._landing_t is None:
                    self._landing_t = t
                if t - self._landing_t >= .010 - 1e-9:
                    flight = self._landing_t - self._flight_t
                    if not .050 <= flight <= 2.0:
                        return self._invalidate('flight_duration')
                    s.update(phase='landing', flight_time_ms=flight * 1000,
                             jump_height_cm=G * flight * flight / 8 * 100,
                             jump_count=s['jump_count'] + 1,
                             velocity_m_s=None, power_w=None,
                             quality='estimate_equal_com_height_required')
                    self._clear_window()
            else:
                self._landing_t = None
            if s['phase'] == 'flight' and t - self._flight_t > 2.0:
                return self._invalidate('flight_timeout')
        return self.snapshot()
