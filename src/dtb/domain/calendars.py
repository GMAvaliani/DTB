"""Календари работы: смены, выходные и исключения.

Модельное время измеряется в часах от начала прогона. День недели выводится из
начального дня недели предприятия (``Enterprise.calendar_start_weekday``,
0 — понедельник): день = ⌊t/24⌋, день недели = (начальный + день) mod 7,
час в сутках = t mod 24.

Календарь задаёт **доступную долю ёмкости** ресурса в каждый момент: вне смены
доля равна нулю, и ресурс недоступен. Моменты начала и конца смен становятся
моментами переключения, поэтому движок замечает их точно, а не с округлением.

Исключения (``exceptions``) перекрывают недельный график: плановая остановка
задаётся интервалом с ``working=False``, дополнительная смена — с
``working=True``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dtb.domain.base import EntityBase

#: Русские сокращения дней недели для отчётов и импорта.
WEEKDAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
HOURS_PER_DAY = 24.0
DAYS_PER_WEEK = 7


class Shift(BaseModel):
    """Смена: день недели (или все дни), интервал часов в сутках, доля ёмкости."""

    model_config = ConfigDict(extra="forbid")

    day_of_week: int | None = Field(default=None, ge=0, le=6)  # None — все дни
    start_hour: float = Field(default=0.0, ge=0, le=24)
    end_hour: float = Field(default=24.0, ge=0, le=24)
    capacity_factor: float = Field(default=1.0, ge=0)
    label: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Shift":
        if self.end_hour <= self.start_hour:
            raise ValueError(
                f"смена {self.label or ''}: end_hour ({self.end_hour}) должен быть "
                f"больше start_hour ({self.start_hour}); "
                f"ночная смена задаётся двумя интервалами"
            )
        return self


class CalendarException(BaseModel):
    """Исключение из недельного графика на интервале модельного времени."""

    model_config = ConfigDict(extra="forbid")

    from_hour: float = Field(ge=0)
    to_hour: float = Field(gt=0)
    working: bool = False  # False — остановка, True — дополнительная смена
    capacity_factor: float = Field(default=1.0, ge=0)
    label: str = ""

    @model_validator(mode="after")
    def _check(self) -> "CalendarException":
        if self.to_hour <= self.from_hour:
            raise ValueError(
                f"исключение {self.label or ''}: to_hour должен быть больше from_hour"
            )
        return self


class Calendar(EntityBase):
    """График работы: смены недельного цикла плюс исключения."""

    shifts: list[Shift] = Field(default_factory=list)
    exceptions: list[CalendarException] = Field(default_factory=list)
    #: доля ёмкости вне смен (0 — ресурс полностью недоступен)
    off_shift_factor: float = Field(default=0.0, ge=0)
    enabled: bool = True

    def factor_at(self, t: float, weekday_offset: int = 0) -> float:
        """Доступная доля ёмкости в момент t.

        ``weekday_offset`` — день недели, которому соответствует t = 0
        (0 — понедельник); его передаёт движок из настроек предприятия.
        """
        if not self.enabled:
            return 1.0
        for exc in self.exceptions:
            if exc.from_hour - 1e-9 <= t < exc.to_hour - 1e-9:
                return exc.capacity_factor if exc.working else 0.0
        if not self.shifts:
            return 1.0
        weekday, hour = self.position(t, weekday_offset)
        for shift in self.shifts:
            if shift.day_of_week is not None and shift.day_of_week != weekday:
                continue
            if shift.start_hour - 1e-9 <= hour < shift.end_hour - 1e-9:
                return shift.capacity_factor
        return self.off_shift_factor

    def position(self, t: float, weekday_offset: int = 0) -> tuple[int, float]:
        """(день недели, час в сутках) для момента t."""
        day = int(t // HOURS_PER_DAY)
        return (weekday_offset + day) % DAYS_PER_WEEK, t - day * HOURS_PER_DAY

    def boundaries_in_day(self) -> list[float]:
        """Часы в сутках, в которых доля ёмкости может измениться."""
        points = {0.0}  # полночь: на ней меняется день недели
        for shift in self.shifts:
            points.add(shift.start_hour)
            points.add(shift.end_hour)
        return sorted(p for p in points if 0.0 <= p < HOURS_PER_DAY)

    def next_change(
        self, t: float, horizon: float, weekday_offset: int = 0
    ) -> float | None:
        """Ближайший момент после t, в котором доля ёмкости действительно меняется.

        Границы смен и исключений — лишь кандидаты: в полночь при дневной смене
        доля не меняется, и такой момент не нужен движку. Поэтому кандидаты
        проверяются по существу — сравнением доли до и после.
        """
        current = self.factor_at(t, weekday_offset)
        exception_points = sorted(
            {
                point
                for exc in self.exceptions
                for point in (exc.from_hour, exc.to_hour)
                if t + 1e-9 < point <= horizon + 1e-9
            }
        )
        boundaries = self.boundaries_in_day() if self.shifts else []
        # просмотр идёт по суткам до первого реального изменения. Исключение
        # может быть длиннее недели (новогодние праздники): тогда граница смен
        # после его окончания лежит дальше недели от t, и её нужно найти.
        # Недельный график периодичен, поэтому восемь суток без изменений
        # после последнего исключения означают, что изменений больше не будет.
        day = int(t // HOURS_PER_DAY)
        last_day = int(horizon // HOURS_PER_DAY) + 1
        next_exception = 0
        quiet_days = 0
        while day <= last_day:
            day_end = (day + 1) * HOURS_PER_DAY
            points = [
                day * HOURS_PER_DAY + hour
                for hour in boundaries
                if t + 1e-9 < day * HOURS_PER_DAY + hour <= horizon + 1e-9
            ]
            had_exception = False
            while (
                next_exception < len(exception_points)
                and exception_points[next_exception] < day_end
            ):
                points.append(exception_points[next_exception])
                next_exception += 1
                had_exception = True
            for point in sorted(points):
                if abs(self.factor_at(point, weekday_offset) - current) > 1e-9:
                    return point
            quiet_days = 0 if had_exception else quiet_days + 1
            if next_exception >= len(exception_points):
                if not boundaries or quiet_days > DAYS_PER_WEEK:
                    break
            elif not boundaries:
                # без смен доля меняется только на границах исключений
                day = max(day + 1, int(exception_points[next_exception] // HOURS_PER_DAY))
                continue
            day += 1
        return None
