"""Model of Admin's Pension

For now, this model is not intended for general use and applies
the specific rules of the admin's pension.
"""

from abc import ABC, abstractmethod
import math
from app.data import constants
from app.data.constants import INTERVALS_PER_YEAR
from app.models.config import IncomeProfile, NetWorthStrategyConfig, User
from app.models.financial.state import State
from app.util import interval_yield


BENEFIT_RATES = {
    2043: 0.0116,
    2044: 0.0128,
    2045: 0.0140,
    2046: 0.0152,
    2047: 0.0164,
    2048: 0.0176,
    2049: 0.0188,
    2050: 0.0200,
    2051: 0.0213,
    2052: 0.0227,
    2053: 0.0240,
}
"""Pension details for admin. Format: {year:rate}"""
PENSION_CONTRIBUTION = 0.09  # 9% of income
"""Last date of update"""
INTEREST_YIELD = 1.02  # varies from 1.2-3% based on Progress Reports
EARLY_YEAR = 2043
MID_YEAR = 2048
LATE_YEAR = 2053
JOB_START_DATE = 2016


class _Strategy(ABC):
    """Abstract allocation strategy class.

    Required methods:
        calc_payment(self, state: State) -> float:
    """

    @abstractmethod
    def calc_payment(self, state: State) -> float:
        """Calculate pension payment based on current state

        Args:
            state (State): current state

        Returns:
            float: pension payment for interval
        """


class _AgeStrategy(_Strategy):
    def __init__(self, trigger_year: int, base: float):
        self._trigger_date = trigger_year
        benefit_rate = BENEFIT_RATES[trigger_year]
        self._payment = base * benefit_rate

    def calc_payment(self, state: State) -> float:
        if state.date >= self._trigger_date:
            return self._payment * state.inflation
        return 0


class _NetWorthStrategy(_Strategy):
    def __init__(self, config: NetWorthStrategyConfig, base: float):
        self._equity_target = config.equity_target
        self._base = base
        self._payment = None
        self._benefit_rate = None

    def calc_payment(self, state: State) -> float:
        if self._payment:
            return self._payment * state.inflation
        if (
            state.date >= EARLY_YEAR
            and state.net_worth < self._equity_target * state.inflation
        ) or state.date == LATE_YEAR:
            self._benefit_rate = BENEFIT_RATES[math.trunc(state.date)]
            self._payment = self._base * self._benefit_rate
            return self._payment * state.inflation
        return 0


class _CashOutStrategy(_Strategy):
    """
    Args:
        user (User)
    """

    def __init__(self, user: User):
        self._pension = user.admin.pension
        self._income_profile = user.partner.income_profiles[0]
        self._interval_raise = interval_yield(1 + self._income_profile.yearly_raise)
        self._est_prev_interval_income = self._calc_est_prev_interval_income()
        self.cash_out_date = self._income_profile.last_date
        self.pension_balance = self._calc_pension_balance()

    def _calc_est_prev_interval_income(self) -> float:
        """Estimate the interval income at the time when account balance was last updated"""
        age_of_data = self._intervals_between(
            self._pension.balance_update, constants.TODAY_YR_QT
        )
        # Estimate interval income at the time of last update
        interval_income = self._income_profile.starting_income / INTERVALS_PER_YEAR
        return interval_income / (self._interval_raise**age_of_data)

    def _calc_pension_balance(self) -> float:
        """Estimate pension balance at cash out date"""
        working_intervals = self._intervals_between(
            self.cash_out_date, self._pension.balance_update
        )
        pension_balance = self._pension.account_balance
        income = self._est_prev_interval_income
        interval_interest = interval_yield(INTEREST_YIELD)
        for _ in range(working_intervals):
            pension_balance *= interval_interest
            pension_balance += income * PENSION_CONTRIBUTION
            income *= self._interval_raise
        return pension_balance

    def _intervals_between(self, one_date: float, another_date: float) -> int:
        """Calculate the qty of intervals between dates. Input order doesn't matter"""
        if another_date >= one_date:
            return round((another_date - one_date) * INTERVALS_PER_YEAR)
        return self._intervals_between(  # pylint: disable=arguments-out-of-order
            another_date, one_date
        )

    def calc_payment(self, state: State) -> float:
        if math.isclose(state.date, self.cash_out_date):
            return self.pension_balance
        return 0


class Controller:
    """Manages pension strategy and payment generation.

    The Defined Benefit Program provides a monthly benefit based on a formula:
    `service credit x age factor x final compensation = your retirement benefit`

    Attributes:
        strategy (_Strategy): pension payment strategy

    """

    def __init__(self, user: User):
        self._user = user
        if user.admin:
            base = self._calc_base(user.partner.income_profiles[0])
            self.strategy = self._gen_strategy(base)
        else:
            self.strategy = None

    def _calc_base(self, job_profile: IncomeProfile) -> float:
        """Calculate the interval value to multiply against the benefit rates

        This is the `service credit x final compensation` portion of the pension formula
        """
        years_worked = job_profile.last_date - JOB_START_DATE
        # This base should technically be the present value of income level
        # at the last year of work, but seeing as I don't have access to inflation
        # when this is initialized and we're not too far from retirement
        # anyway, I'll just say that the present value of the future income
        # is roughly equal to the current income
        return job_profile.starting_income * years_worked / INTERVALS_PER_YEAR

    def _gen_strategy(self, base: float) -> _Strategy:
        (
            strategy_str,
            strategy_obj,
        ) = self._user.admin.pension.strategy.chosen_strategy
        match strategy_str:
            case "early":
                return _AgeStrategy(trigger_year=EARLY_YEAR, base=base)
            case "mid":
                return _AgeStrategy(trigger_year=MID_YEAR, base=base)
            case "late":
                return _AgeStrategy(trigger_year=LATE_YEAR, base=base)
            case "net_worth":
                return _NetWorthStrategy(config=strategy_obj, base=base)
            case "cash_out":
                return _CashOutStrategy(self._user)

    def calc_payment(self, state: State) -> float:
        """Calculate pension payment for interval

        Args:
            state (State): current state

        Returns:
            float: Interval payment
        """
        if not self.strategy:
            return 0
        return self.strategy.calc_payment(state)
