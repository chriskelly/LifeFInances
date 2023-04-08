"""Model of social security.

Required installations are detailed in requirements.txt.

This file contains the following functions:

    * est_Max_Earning() - return estimated social security max earnings
    * est_Index() - return estimated social security index
    * Calculator.make_list() - Returns list with social security payments
    * Calculator.get_payment() - Get a payment for a specific date
    * taxes() - Generate list of taxes paid for social security

"""

from __future__ import annotations
import math
from typing import TYPE_CHECKING
import numpy as np
from data import constants as const
from models import model
import simulator
if TYPE_CHECKING:
    from simulator import Simulator
    from models import income
    from social_security import Calculator as SSCalc

EARLY_AGE = 62
MID_AGE = 66
LATE_AGE = 70

# SS https://www.ssa.gov/oact/cola/Benefits.html
# Effect of Early or Delayed Retirement on Retirement Benefits:
# https://www.ssa.gov/oact/ProgData/ar_drc.html
# Index factors: https://www.ssa.gov/oact/cola/awifactors.html
# Earnings limit:
# https://www.ssa.gov/benefits/retirement/planner/whileworking.html#:~:text=your%20excess%20earnings.-,How%20We%20Deduct%20Earnings%20From%20Benefits,full%20retirement%20age%20is%20%2451%2C960.
# Bend points: https://www.ssa.gov/oact/cola/piaformula.html
# PIA: https://www.ssa.gov/oact/cola/piaformula.html

# Using historical data to predict future Social Security Administration Max Earnings and Indicies
# https://rowannicholls.github.io/python/curve_fitting/exponential.html
SS_MAX_EARNINGS = np.transpose(np.array(const.SS_MAX_EARNINGS))
x_M_E, y_M_E = SS_MAX_EARNINGS[0], SS_MAX_EARNINGS[1] # prep data for np.polyfit
fit_M_E = np.polyfit(x_M_E, np.log(y_M_E), 1)
a_M_E, b_M_E = np.exp(fit_M_E[1]), fit_M_E[0]
def est_max_earning(year:float) -> float:
    """return estimated social security max earnings for specific year

    Args:
        year (float)

    Returns:
        float: max earnings in $1000s format
    """
    return a_M_E * np.exp(b_M_E * year)
# repeat for indicies
SS_INDEXES = np.transpose(np.array(const.SS_INDEXES))
x_I, y_I = SS_INDEXES[0], SS_INDEXES[1]
fit_I = np.polyfit(x_I, np.log(y_I), 1)
a_I, b_I = np.exp(fit_I[1]), fit_I[0]
def est_index(year:float) -> float:
    """return estimated social security index for specific year

    Args:
        year (float)

    Returns:
        float: max earnings in $1000s format
    """
    return a_I * np.exp(b_I * year)

class Calculator:
    """Calculates social security payments

    Attributes
        sim (simulator.Simulator)

        usr (str): Either 'User' or 'Partner'

        date_ls (list)

        income_calc (income.Calculator): income calculator for specific user

        spouse_calc (social_security.Calculator): optional. social security calculator of spouse
                                                    (parter for user, user for partner)

    Methods
        make_list(): Returns list with social security payments starting from today till final date

        get_payment(): Get a payment for a specific date
    """
    def __init__(self, sim:Simulator, usr:str, date_ls:list, income_calc:income.Calculator,
                 spouse_calc:SSCalc = None):
        self.sim, self.date_ls, self.spouse_calc= sim, date_ls, spouse_calc
        self.usr, self.income_calc = usr, income_calc
        if usr == 'User':
            self.age = sim.user.age
            self.method = sim.user.social_security_method
            self.pension = sim.user.pension
            self.earnings_record = {record.year:record.earnings for record in sim.user.earnings
                                    if not record.is_partner_earnings}
        elif usr == 'Partner':
            self.age = sim.user.partner_age
            self.method = sim.user.partner_social_security_method
            self.pension = sim.user.partner_pension
            self.earnings_record = {record.year:record.earnings for record in sim.user.earnings
                                    if record.is_partner_earnings}
        else:
            raise ValueError("usr must be either 'User' or 'Partner'")
        if self.spouse_calc:
            spouse_calc.spouse_calc = self # if a spouse is added, make spouse's spouse this calc
        self.triggered = False # Has SS been triggered
        eligible_income_ls = _eligible_income(date_ls, income_calc)
        self._add_to_earnings_record(date_ls, eligible_income_ls)
        # -------- CALCULATE PIA (BASE SOCIAL SECURITY PAYMENT) -------- #
        # index and limit the earnings, then sort them from high to low
        ss_earnings = [min(est_max_earning(year), earning) * est_index(year)
                       for year, earning in self.earnings_record.items()]
        ss_earnings.sort(reverse=True)
        # Find Average Indexed Monthly Earnings (AIME), only top 35 years (420 months) count
        aime = sum(ss_earnings[:35])/420
        # Calculate monthly Primary Insurance Amounts (PIA) using bend points.
        # Add AIME and sort to see where the AIME ranks in the bend points
        bend_points =const.SS_BEND_POINTS+[aime]
        bend_points.sort()
        bend_points = bend_points[:bend_points.index(aime)+1] # cut off bend points at inserted AIME
        # For the first bracket, just the bend times the rate.
        # After that, find the marginal income to multiple by the rate
            # PIA rates are lower if you have certain pension incomes
        if self.pension:
            pia_rates = const.PIA_RATES_PENSION
        else:
            pia_rates = const.PIA_RATES
        self.full_pia = sum((bend_points[i]-bend_points[i-1])*rate if i!=0 else bend*rate
                             for (i, bend), rate in zip(enumerate(bend_points), pia_rates))
        # -------- FIND SS AGE AND DATE -------- #
        if self.method == 'early':
            self.ss_age = EARLY_AGE
        elif self.method == 'mid':
            self.ss_age = MID_AGE
        else:
            # method == 'late' or 'portfolio'
            # List will be overwritten if portfolio triggers before late age
            self.ss_age = LATE_AGE
        # not quarterly precise, could be improved by changing from age to birth quarter
        self.ss_date = self.ss_age - self.age + model.TODAY_YR + 1
        # for later use
        self.inflation_ls = None
        self.benefit_rate = None
        self.payments = None
        self.ss_row = None

    def make_list(self, inflation_ls:list):
        """Returns list with social security payments starting from today till final date"""
        self.inflation_ls = inflation_ls
        self.benefit_rate = const.BENEFIT_RATES[str(self.ss_age)]
        # find adjusted PIA based on benefit rates for selected age
        adjusted_pia = self.full_pia * self.benefit_rate
        # PIA is in that today's dollars and needs to be adjusted
        self.payments = list(3 * adjusted_pia * np.array(self.inflation_ls))
        self.ss_row = self.date_ls.index(self.ss_date)
        # then trim the early years and replace with 0s
        self.payments = [0]*self.ss_row + self.payments[self.ss_row:]

    def get_payment(self,row:int, net_worth:float, equity_target:float) -> float:
        """Get a payment for a specific date

        Args:
            row (int)
            net_worth (float)
            equity_target (float)

        Returns:
            float: Payment for specific date
        """
        res =  max(self._get_worker_payment(row, net_worth, equity_target),
                   self._get_spousal_payment(row, net_worth, equity_target))
        if self.sim.admin and self.usr == 'partner':
            res += self._admin_pension_payment(row, equity_target, net_worth)
        return res

    def _get_worker_payment(self,row,net_worth,equity_target):
        if self.method == 'net worth' and net_worth < equity_target * self.inflation_ls[row]\
            and not self.triggered:
        # Have to generate new list if using 'net worth' method
            current_date = self.date_ls[row]
            current_age = math.trunc(current_date) + self.age - model.TODAY_YR
            if EARLY_AGE <= current_age <= LATE_AGE: # confirm worker is of age to retire
                self.ss_date, self.ss_age = current_date, current_age
                self.triggered = True
                self.make_list(self.inflation_ls)
        return self.payments[row]

    def _get_spousal_payment(self, row, net_worth, equity_target):
        """Returns the eligible portion of a spouse's income the worker could receive.
        https://www.ssa.gov/benefits/retirement/planner/applying7.html"""
        if not self.spouse_calc\
            or self.spouse_calc._get_worker_payment(row, net_worth, equity_target) == 0\
                or row < self.ss_row: # pylint: disable=protected-access # N/A
                # if not married or spouse not receiving payments yet or worker's social security
                # hasn't been triggered yet (worker's SS strategy should not be overridden
                # by spouse's strategy)
            return 0
        spouse_payment = self.spouse_calc._get_worker_payment(row, net_worth, equity_target) # pylint: disable=protected-access # N/A
        # reverse the adjustment used to calculate spouse's PIA
        inverted_spouse_payment = spouse_payment / self.spouse_calc.benefit_rate
        # Worker's can earn up to half of their spouse's PIA, adjusted by the worker's benefit rate
        spousal_benefit = 0.5 * inverted_spouse_payment * self.benefit_rate
        # if this worker has a pension and that pension has started paying yet:
        if self.pension and hasattr(self.sim, 'pension_ls'):
            # worker's with a pension have to cut spousal benefit by 2/3 of pension payment
            # https://www.ssa.gov/benefits/retirement/planner/gpo-calc.html
            spousal_benefit = max(0,spousal_benefit - (2/3) * self.sim.pension_ls[row])
        return spousal_benefit

    def _add_to_earnings_record(self, date_ls, income_ls):
        """Add input date_ls and income_ls to the earnings record"""
        # need to deepcopy to avoid editing the lists outside of this method
        date_record = date_ls.copy()
        income_record = income_ls.copy()
        # First, if the first dates are fractional,
        # convert value to annual and delete the fractional first dates
        if date_record[0] % 1 != 0:
            year = math.trunc(date_record[0])
            self.earnings_record[year] = income_record[0] * 4
            while date_record[0] % 1 != 0:
                del date_record[0]
                del income_record[0]
        # Then add the remaining dates to the earnings_record
        for date, income in zip(date_record, income_record):
            year = math.trunc(date)
            if income != 0:
                if year in self.earnings_record:
                    self.earnings_record[year] += income
                else: self.earnings_record[year] = income

    def _admin_pension_payment(self, row:int, equity_target:float, net_worth:float) -> float:
        """Calculates the pension for Admin's partner if admin is selected.
        
        Args:
            row (int): date_ls index
            net_worth (float): net worth at this date
            equity_target (float): equity target at this date

        Returns:
            float: pension payout for given date
        """
            # if pension list has already been calculated, just return the value for the current row
        if hasattr(self, 'pension_ls'):
            if row == 0:
                del self.pension_ls # reset for each loop
            else:
                return self.pension_ls[row] # pylint: disable=access-member-before-definition
                    #only accesses if it's been defined
            # set variables
        early_pension_year, mid_pension_year, late_pension_year = 2043, 2049, 2055
        method = self.sim.user.admin_pension_method
        pension_income = self.income_calc.profiles[0]
        # Corrects for 9% taken from salary for pension
        current_pension_salary_qt = pension_income.income_qt/(1-const.PENSION_COST)
        working_qts = pension_income.last_date_idx - pension_income.start_date_idx()
        max_pension_salary_qt = current_pension_salary_qt * pension_income.yearly_raise\
                                ** (working_qts/4)
        if method == 'cash-out':
            # Need to correct for out-dated info, first estimate salary at date of last update,
            # then project forward
            # Find age of data
            data_age_qt = int((model.TODAY_YR_QT - const.PENSION_ACCOUNT_BAL_UP_DATE)/.25)
            est_prev_pension_salary_qt = current_pension_salary_qt\
                / (pension_income.yearly_raise ** (data_age_qt/4)) # Estimate salary at date of data
            # Rough estimate of historical earnings
            # + projected future earnings (corrected for pension cost)
            projected_income = np.concatenate(
                                (simulator.step_quarterize(self.date_ls, est_prev_pension_salary_qt,
                                                           pension_income.yearly_raise,
                                                           start_date_idx=0,
                                                           end_date_idx=data_age_qt),
                                 np.array(pension_income.income_ls)/(1-const.PENSION_COST)))
            pension_bal = const.PENSION_ACCOUNT_BAL
            pension_int_rate_qt = const.PENSION_INTEREST_YIELD ** (1/4) - 1
            for each_income in projected_income:
                pension_bal += each_income * const.PENSION_COST + pension_bal * pension_int_rate_qt
            self.pension_ls = [0]*working_qts + [pension_bal] + [0]*(len(self.date_ls)
                                                                     - working_qts - 1)
            return self.pension_ls[row]
        if method == 'net worth':
            if (net_worth > equity_target * self.inflation_ls[row]
                and self.date_ls[row]<late_pension_year) or self.date_ls[row]<early_pension_year:
                return 0 # haven't triggered yet or passed the late pension year
            pension_start_yr = min(math.trunc(self.date_ls[row]),late_pension_year)
        elif method == 'early':
            pension_start_yr = early_pension_year
        elif method == 'mid':
            pension_start_yr = mid_pension_year
        elif method == 'late':
            pension_start_yr = late_pension_year
            # find initial pension amount (in last working year's dollars)
        pension_job_first_year = 2016
        pension_job_last_year = math.trunc(self.date_ls[pension_income.last_date_idx])
        years_worked = pension_job_last_year - pension_job_first_year
        pension_multiplier = const.ADMIN_PENSION_RATES[str(pension_start_yr)]
        starting_pension_qt = max_pension_salary_qt * years_worked * pension_multiplier
        # convert to est. value at pension_start_yr
        starting_pension_qt *= pension_income.yearly_raise**(pension_start_yr-pension_job_last_year)
            # build out list with the correct number of zeros to the beginning
        start_date_idx = self.date_ls.index(pension_start_yr)
        self.pension_ls = [0]*(start_date_idx) \
                        + simulator.step_quarterize(self.date_ls, starting_pension_qt,
                                                    pension_income.yearly_raise, start_date_idx,
                                                    end_date_idx=self.sim.rows-1)
        return self.pension_ls[row]

def _eligible_income(date_ls:list, income_calc:income.Calculator) -> list:
    """Determine which income can be used for social security

    Args:
        date_ls (list)
        income_calc (income.Calculator): a single income Calculator

    Returns:
        list: full list of incomes with 0s for non-eligible incomes and non-working years
    """
    eligible_income = []
    for each_income in income_calc.profiles:
        if each_income.ss_eligible:
            eligible_income += each_income.income_ls
        else:
            eligible_income += [0] * len(each_income.income_ls)
    eligible_income += [0]*(len(date_ls)-len(eligible_income))
    return eligible_income

def taxes(date_ls:list, inflation, user_calc:income.Calculator,
          partner_calc:income.Calculator = None) -> list:
    """Generate list of taxes paid for social security
    Dependent on whether an individual income stream is social security eligible

    Args:
        date_ls (list)
        inflation (_type_): used to estimate yearly step quarterized growth of SS Max Earnings
        user_calc (income.Calculator)
        partner_calc (income.Calculator, optional): Defaults to None.

    Returns:
        list
    """
    # sum up social security eligible income from all usrs
    if partner_calc:
        total_eligible_income = list(np.array(_eligible_income(date_ls, user_calc))
                                 +np.array(_eligible_income(date_ls, partner_calc)))
    else:
        total_eligible_income = _eligible_income(date_ls, user_calc)
    # need the SS Max Earnings, but in quarter form instead of the annual form
    ss_max_earnings_qt_ls = simulator.step_quarterize(date_ls, first_val=0.25
                                                      * est_max_earning(model.TODAY_YR),
                                                       increase_yield=inflation, start_date_idx=0,
                                                       end_date_idx=len(date_ls)-1)
    ss_tax = [0.062*min(income, ss_max) for income, ss_max in zip(total_eligible_income,
                                                                ss_max_earnings_qt_ls)]
    return ss_tax

# def test_unit():
#     my_simulator = simulator.test_unit()
#     test_date_ls = [2022.75, 2023.0, 2023.25, 2023.5, 2023.75, 2024.0, 2024.25, 2024.5, 2024.75,
# 2025.0, 2025.25, 2025.5, 2025.75, 2026.0, 2026.25, 2026.5, 2026.75, 2027.0, 2027.25, 2027.5,
# 2027.75, 2028.0, 2028.25, 2028.5, 2028.75, 2029.0, 2029.25, 2029.5, 2029.75, 2030.0, 2030.25,
# 2030.5, 2030.75, 2031.0, 2031.25, 2031.5, 2031.75, 2032.0, 2032.25, 2032.5, 2032.75, 2033.0,
# 2033.25, 2033.5, 2033.75, 2034.0, 2034.25, 2034.5, 2034.75, 2035.0, 2035.25, 2035.5, 2035.75,
# 2036.0, 2036.25, 2036.5, 2036.75, 2037.0, 2037.25, 2037.5, 2037.75, 2038.0, 2038.25, 2038.5,
# 2038.75, 2039.0, 2039.25, 2039.5, 2039.75, 2040.0, 2040.25, 2040.5, 2040.75, 2041.0, 2041.25,
# 2041.5, 2041.75, 2042.0, 2042.25, 2042.5, 2042.75, 2043.0, 2043.25, 2043.5, 2043.75, 2044.0,
# 2044.25, 2044.5, 2044.75, 2045.0, 2045.25, 2045.5, 2045.75, 2046.0, 2046.25, 2046.5, 2046.75,
# 2047.0, 2047.25, 2047.5, 2047.75, 2048.0, 2048.25, 2048.5, 2048.75, 2049.0, 2049.25, 2049.5,
# 2049.75, 2050.0, 2050.25, 2050.5, 2050.75, 2051.0, 2051.25, 2051.5, 2051.75, 2052.0, 2052.25,
# 2052.5, 2052.75, 2053.0, 2053.25, 2053.5, 2053.75, 2054.0, 2054.25, 2054.5, 2054.75, 2055.0,
# 2055.25, 2055.5, 2055.75, 2056.0, 2056.25, 2056.5, 2056.75, 2057.0, 2057.25, 2057.5, 2057.75,
# 2058.0, 2058.25, 2058.5, 2058.75, 2059.0, 2059.25, 2059.5, 2059.75, 2060.0, 2060.25, 2060.5,
# 2060.75, 2061.0, 2061.25, 2061.5, 2061.75, 2062.0, 2062.25, 2062.5, 2062.75, 2063.0, 2063.25,
# 2063.5, 2063.75, 2064.0, 2064.25, 2064.5, 2064.75, 2065.0, 2065.25, 2065.5, 2065.75, 2066.0,
# 2066.25, 2066.5, 2066.75, 2067.0, 2067.25, 2067.5, 2067.75, 2068.0, 2068.25, 2068.5, 2068.75,
# 2069.0, 2069.25, 2069.5, 2069.75, 2070.0, 2070.25, 2070.5, 2070.75, 2071.0, 2071.25, 2071.5,
# 2071.75, 2072.0, 2072.25, 2072.5, 2072.75, 2073.0, 2073.25, 2073.5, 2073.75, 2074.0, 2074.25,
# 2074.5, 2074.75, 2075.0, 2075.25, 2075.5, 2075.75, 2076.0, 2076.25, 2076.5, 2076.75, 2077.0,
# 2077.25, 2077.5, 2077.75, 2078.0, 2078.25, 2078.5, 2078.75, 2079.0, 2079.25, 2079.5, 2079.75,
# 2080.0, 2080.25, 2080.5, 2080.75, 2081.0, 2081.25, 2081.5, 2081.75, 2082.0, 2082.25, 2082.5,
# 2082.75, 2083.0, 2083.25, 2083.5, 2083.75, 2084.0, 2084.25, 2084.5, 2084.75, 2085.0, 2085.25,
# 2085.5, 2085.75, 2086.0, 2086.25, 2086.5, 2086.75, 2087.0, 2087.25, 2087.5, 2087.75, 2088.0,
# 2088.25, 2088.5, 2088.75, 2089.0, 2089.25, 2089.5, 2089.75]
#     test_income_ls = [75.48375, 78.5031, 78.5031, 78.5031, 78.5031, 81.643224, 81.643224,
# 81.643224, 81.643224, 84.90895296000001, 84.90895296000001, 84.90895296000001, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
# 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
#     inflation_ls = returnGenerator.main(my_simulator.rows,4,1)[3][0]
#     ss_calc =  Calculator(my_simulator,'User',inflation_ls,
#                      date_ls=test_date_ls,income_ls=test_income_ls)
#     return ss_calc._make_list(ss_date=2061.25)
