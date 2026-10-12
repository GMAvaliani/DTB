"""Слой наблюдений: политика видит не истину, а оценку Ŷ.

Наблюдение снимается периодически, становится доступным с задержкой и с
относительной погрешностью. Оценка строится одним из способов: последнее
доступное значение, экспоненциальное сглаживание, предсказание по тренду двух
последних наблюдений.

Режимы:

* ``twin`` — политика читает оценку Ŷ (задержка и шум учитываются);
* ``pure_simulation`` — политика читает истинное Y.

Разность итогов двух прогонов, различающихся только режимом, и есть цена
неполноты наблюдений.

Воспроизводимость: шум берётся из SHA-256-субпотока (seed, "observation", id),
новых независимых генераторов не заводится.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from dtb.common.seeding import substream
from dtb.domain.control import Estimator, Observation, ObservationMode

_EPS = 1e-9


@dataclass
class _Sample:
    t_sample: float
    value: float  # зашумлённое значение
    true_value: float
    available_at: float


@dataclass
class _ObsState:
    observation: Observation
    samples: list[_Sample] = field(default_factory=list)
    next_sample_time: float = 0.0
    smoothed: float | None = None
    error_abs_sum: float = 0.0
    error_rel_sum: float = 0.0
    error_max_abs: float = 0.0
    error_count: int = 0


class ObservationLayer:
    """Хранит снимки наблюдений и выдаёт оценки Ŷ в момент принятия решений."""

    def __init__(
        self,
        observations: dict[str, Observation],
        seed: int,
        mode_override: ObservationMode | None = None,
    ):
        self.mode_override = mode_override
        self._obs: dict[str, _ObsState] = {}
        self._rng = {}
        #: индекс (вид цели, ссылка, метрика) → id наблюдения
        self._index: dict[tuple[str, str, str], str] = {}
        for obs_id, obs in sorted(observations.items()):
            if not obs.enabled:
                continue
            self._obs[obs_id] = _ObsState(observation=obs, next_sample_time=0.0)
            self._rng[obs_id] = substream(seed, "observation", obs_id)
            self._index[(obs.target_kind.value, obs.target_ref, obs.effective_metric())] = obs_id

    # ------------------------------------------------------------------ режим --

    def mode_of(self, obs: Observation) -> ObservationMode:
        return self.mode_override or obs.mode

    @property
    def active(self) -> bool:
        return bool(self._obs)

    # --------------------------------------------------------------- снятие --

    def next_sample_time(self, now: float) -> float | None:
        """Ближайший момент снятия наблюдения, не раньше ``now``."""
        times = [
            st.next_sample_time
            for st in self._obs.values()
            if st.next_sample_time >= now - _EPS
        ]
        return min(times) if times else None

    def due_at(self, t: float) -> list[str]:
        return [
            obs_id
            for obs_id, st in sorted(self._obs.items())
            if st.next_sample_time <= t + _EPS
        ]

    def take_samples(self, t: float, resolver: Callable[[Observation], float]) -> None:
        """Снять все наблюдения, срок которых наступил к моменту ``t``."""
        for obs_id in self.due_at(t):
            st = self._obs[obs_id]
            obs = st.observation
            true_value = float(resolver(obs))
            if obs.relative_error > 0:
                noise = self._rng[obs_id].normalvariate(0.0, obs.relative_error * abs(true_value))
                value = true_value + noise
            else:
                value = true_value
            # начальное наблюдение доступно сразу: начальное состояние известно точно
            available_at = t if st.next_sample_time <= _EPS and t <= _EPS else t + obs.delay
            st.samples.append(
                _Sample(t_sample=t, value=value, true_value=true_value, available_at=available_at)
            )
            if obs.estimator == Estimator.SMOOTH:
                st.smoothed = (
                    value
                    if st.smoothed is None
                    else obs.alpha * value + (1.0 - obs.alpha) * st.smoothed
                )
            st.next_sample_time = t + obs.period

    # --------------------------------------------------------------- оценка --

    def estimate(self, obs_id: str, t: float) -> float | None:
        """Оценка Ŷ по доступным к моменту ``t`` наблюдениям."""
        st = self._obs.get(obs_id)
        if st is None:
            return None
        obs = st.observation
        available = [s for s in st.samples if s.available_at <= t + _EPS]
        if not available:
            return None
        if obs.estimator == Estimator.LAST:
            return available[-1].value
        if obs.estimator == Estimator.SMOOTH:
            # сглаживание только по доступным наблюдениям
            value = available[0].value
            for s in available[1:]:
                value = obs.alpha * s.value + (1.0 - obs.alpha) * value
            return value
        # extrapolate: тренд по двум последним доступным, с коррекцией на время
        if len(available) == 1:
            return available[0].value
        s1, s2 = available[-2], available[-1]
        dt = s2.t_sample - s1.t_sample
        if dt <= _EPS:
            return s2.value
        slope = (s2.value - s1.value) / dt
        return s2.value + slope * (t - s2.t_sample)

    def observed_value(
        self, kind: str, ref: str, metric: str, t: float, true_value: float
    ) -> float:
        """Что увидит политика: оценка (режим twin) либо истина (pure)."""
        obs_id = self._index.get((kind, ref, metric))
        if obs_id is None:
            return true_value
        st = self._obs[obs_id]
        if self.mode_of(st.observation) == ObservationMode.PURE_SIMULATION:
            return true_value
        estimate = self.estimate(obs_id, t)
        if estimate is None:
            return true_value
        # накапливаем ошибку наблюдения относительно истины
        err = abs(estimate - true_value)
        st.error_abs_sum += err
        st.error_max_abs = max(st.error_max_abs, err)
        if abs(true_value) > _EPS:
            st.error_rel_sum += err / abs(true_value)
        st.error_count += 1
        return estimate

    def covers(self, kind: str, ref: str, metric: str) -> bool:
        return (kind, ref, metric) in self._index

    # ---------------------------------------------------------------- отчёт --

    def error_report(self) -> dict[str, Any]:
        """Накопленная разность оценки и истины по каждому наблюдению."""
        out: dict[str, Any] = {}
        for obs_id, st in sorted(self._obs.items()):
            if st.error_count == 0:
                out[obs_id] = {
                    "reads": 0,
                    "mean_abs_error": 0.0,
                    "mean_rel_error": 0.0,
                    "max_abs_error": 0.0,
                    "mode": self.mode_of(st.observation).value,
                    "samples": len(st.samples),
                }
                continue
            out[obs_id] = {
                "reads": st.error_count,
                "mean_abs_error": round(st.error_abs_sum / st.error_count, 6),
                "mean_rel_error": round(st.error_rel_sum / st.error_count, 6),
                "max_abs_error": round(st.error_max_abs, 6),
                "mode": self.mode_of(st.observation).value,
                "samples": len(st.samples),
            }
        return out
