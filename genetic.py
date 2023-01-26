"""Optimization Algorithm for Retirement Planning Simulator

This script allows the user to try to improve the success rate of a retirement plan
by trying different combinations of parameters.

Required installations are detailed in requirements.txt.

This file can also be imported as a module and contains the following function:

    * Algorithm.main() - An infinite loop that uses a genetic algoritm to search
                            for better parameter combinations

"""

import copy
import json
import time
import os
import numpy as np # pylint: disable=unused-import # used in eval() of parameter ranges
import scipy.stats as ss
from simulator import Simulator
import simulator
import models.model
from models.model import Model
import data.constants as const

if not os.path.exists(const.PARAMS_SUCCESS_LOC):
    with open(const.PARAMS_SUCCESS_LOC, 'w', encoding="utf-8"):
        pass

DEBUG_LVL = 2 # Lvl 0 shows only local and final max param sets
RESET_SUCCESS = False # Set to true to reset all the counts in param_success.json
SUCCESS_THRESH = 0.5 # Initial threshold before switching from random to step mutations
OFFSPRING_QTY = 10
TARGET_SUCCESS_RATE = 0.95
INITIAL_MONTE_RUNS = 100
MAX_MONTE_RUNS = 5000
ITER_LIMIT = 5 # Max number of times to run if parent is better than all children
SEED = True # Use current params to start with
RNG = np.random.default_rng()

class Algorithm:
    def __init__(self,model:Model):
        self.reset_success = RESET_SUCCESS
        self.model = model
        simulator.DEBUG_LVL = 0
    
    def main(self, next_loop=(False,[])):
    # ---------------------- First parameter set ---------------------- #
        #self.model = Model()
        self.mutable_param_ranges:dict[str,list] = {param:list(eval(str(obj['range']))) for param,obj in self.model.param_details.items() if 'range' in obj}
        mute_param_vals = {param:val for param,val in self.model.param_vals.items() if 'range' in self.model.param_details[param]}
        full_param_vals = copy.deepcopy(self.model.param_vals) # make a copy rather than point to the same dict # https://stackoverflow.com/a/22341377/13627745
        self.prev_used_params = [] # used to track and prevent reusing the same param sets during step mutation
        success_rate, parent_is_best_qty = 0.0 , 0
        if next_loop[0]: # check to see if this is the first loop or if the previous one was successful and we're auto-advancing
            full_param_vals = next_loop[1]
            parent_mute_param_vals = mute_param_vals
        elif SEED:
            parent_mute_param_vals = mute_param_vals
        else: # if not, keep random mutating till we hit SUCCESS_THRESH
            while success_rate <  SUCCESS_THRESH:
                success_rate, parent_mute_param_vals = self._make_child(full_param_vals,success_rate,'random')
                if type(success_rate) != float:
                    return None # if cancelled
                if DEBUG_LVL >= 1:
                    print(f"Success Rate: {success_rate*100:.2f}%")
        self._update_param_count(parent_mute_param_vals)
    # ---------------------- Improvement loop ---------------------- #
        while True:
            # Confirm if other cores have succeeded yet or not
            self._check_if_beaten(full_param_vals)
            # Make children
            children = []
            for idx in range(OFFSPRING_QTY):
                self.model.log_to_optimize_page(f"Generating trial {idx+1}/{OFFSPRING_QTY}")
                children.append(self._make_child(full_param_vals, success_rate, 'step',
                                                parent_mute_param_vals,
                                                max_step=max(1, parent_is_best_qty), idx=idx))
                if not children[-1][0]:
                    return None # if cancelled
            # Find best child (or use parent if all children worse)
                # Lambda func needed to avoid sorting by params if success rates are equal
            children.sort(key=lambda child: child[0], reverse=True) 
            # ------ Children not improving ------ #
            if success_rate >= children[0][0]: # Parent better than child
                self.model.log_to_optimize_page('No improvement')
                parent_is_best_qty += 1
                if DEBUG_LVL>=1:
                    print(f"No better children {parent_is_best_qty}/{ITER_LIMIT}")
                if parent_is_best_qty >= ITER_LIMIT: # if children not improving, start over with random child
                    self.model.log_to_optimize_page(f"Best found: {success_rate*100:.2f}%\n {parent_mute_param_vals}")
                    print(f"Local max: {success_rate*100:.2f}%\n {parent_mute_param_vals}")
                    parent_is_best_qty = 0
                    success_rate = 0.0
                    while success_rate <  SUCCESS_THRESH:
                        success_rate, parent_mute_param_vals = self._make_child(full_param_vals,success_rate,'random')
                        if not success_rate: 
                            return None # if cancelled
            # ------ Child is better ------ #
            else: # If child better than parent, update success rate and params
                parent_is_best_qty = 0
                success_rate, parent_mute_param_vals = children[0] 
                self._update_param_count(parent_mute_param_vals)
                self.model.log_to_optimize_page(f'Found a better combination!')
                if DEBUG_LVL >= 1:
                    print(f"Success Rate: {success_rate*100:.2f}%")
            # ------ Child beats target, proceed to test child ------ #
            if success_rate >= TARGET_SUCCESS_RATE * 1.005: # Add a slight buffer to prevent osccilating between barely beating it and failing upon retest 
                self.model.log_to_optimize_page('Confirming combination works well...')
                current_monte_carlo_runs = simulator.MONTE_CARLO_RUNS # save previous value
                simulator.MONTE_CARLO_RUNS = MAX_MONTE_RUNS
                success_rate, _ = self._make_child(full_param_vals,success_rate,'identical',parent_mute_param_vals) # test at higher monte carlo runs
                if not success_rate:
                    return None # if cancelled
                simulator.MONTE_CARLO_RUNS = current_monte_carlo_runs
                if success_rate < TARGET_SUCCESS_RATE:
                    self.model.log_to_optimize_page("Wasn't actually better")
                    if DEBUG_LVL>=1:
                        print(f"Couldn't stand the pressure...{success_rate*100:.2f}%")
                else: # Print results, overwrite params, start again with more ambitious FI target date
                    self._check_if_beaten(full_param_vals)
                    self.model.log_to_optimize_page(f"Found a good combination! {success_rate*100:.2f}%\n {parent_mute_param_vals}")
                    print(f"Final max: {success_rate*100:.2f}%\n {parent_mute_param_vals}")
                    full_param_vals.update(parent_mute_param_vals)
                    self.model.save_from_genetic(parent_mute_param_vals,reduce_dates = next_loop[0])
                    full_param_vals = copy.deepcopy(self.model.param_vals)
                    self.main(next_loop=(True,full_param_vals))
                    
    # ---------------------- Mutation ---------------------- #
    def _random_mutate(self) -> dict:
        """Return mutable params_vals with shuffled values"""
        return {param:np.random.choice(param_range) for (param,param_range) in self.mutable_param_ranges.items()} # random.choice doesn't always work on np.arrays, so np.random.choice is used https://github.com/python/cpython/issues/100805
    
    def _step_mutate(self,mutable_param_values:dict,max_step=1) -> dict:
        """Return mutable param_vals with values shifted in a normal distribution around 
        provided mutable_param_values with a max deviation of max_step"""
        res = {}
        for param,param_range in self.mutable_param_ranges.items():
            old_idx = param_range.index(mutable_param_values[param])
            new_idx = min(len(param_range)-1,max(0,self._gaussian_int(center=old_idx,max_deviation=max_step)))
            res[param] = param_range[new_idx]
        if res in self.prev_used_params:
            if DEBUG_LVL>=1: 
                print(f'Tried params: {len(self.prev_used_params)}')
            res = self._step_mutate(mutable_param_values,max_step)
        return res
    
    # -------------------------------- HELPER FUNCTIONS -------------------------------- #
       
    # def _load_param_success(self):
    #     """Load from json file and update self.param_cnt.
    #     If it fails, dumps an empty json file.
    #     """
    #     with open(const.PARAMS_SUCCESS_LOC, 'r+') as json_file:
    #         try:
    #             self.param_cnt = json.load(json_file)
    #         except: 
    #             self.param_cnt = {}
    #             self.reset_success = True
    #             json.dump(self.param_cnt, json_file, indent=4)

    def _check_if_beaten(self,full_param_vals):
        for usr in ['user','partner']:
            for i, income in enumerate(full_param_vals[f'{usr}_jobs']):
                if income["last_date"] > models.model.load_params()[0][f'{usr}_jobs'][i]["last_date"]:
                    print('got beat')
                    self.main() # start over if another instance found working parameters

    def _gaussian_int(self,center:int,max_deviation:int) -> int:
        """Returns an int from a random gaussian distribution
        https://stackoverflow.com/questions/37411633/how-to-generate-a-random-normal-distribution-of-integers
        """
        scale= max_deviation/1.5 # decreasing the demonimator results in a flater distribution
        x = np.arange(-max_deviation, max_deviation+1) +center
        xU, xL = x + 0.5, x - 0.5
        prob = ss.norm.cdf(xU,loc=center, scale = scale) - ss.norm.cdf(xL,loc=center, scale = scale)
        prob = prob / prob.sum() # normalize the probabilities so their sum is 1
        return np.random.choice(x, p = prob)

    def _update_param_count(self,param_vals:dict):
        """Edit the param_success.json file to add another tally for each of the
        successful mutable_param values. If first time and RESET_SUCCESS,
        overwrite previous file and set count to 0"""
        with open(const.PARAMS_SUCCESS_LOC, 'r+', encoding="utf-8") as json_file:
            try:
                self.param_cnt = json.load(json_file)
            except: 
                self.param_cnt = {}
                self.reset_success = True
                json.dump(self.param_cnt, json_file, indent=4)
        if self.reset_success:
            self.reset_success = False
            self.param_cnt = {param:[0]*len(param_range) for param,param_range in self.mutable_param_ranges.items()}
        for param,param_range in self.mutable_param_ranges.items():
            self.param_cnt[param][param_range.index(param_vals[param])] += 1
        with open(const.PARAMS_SUCCESS_LOC, 'w', encoding="utf-8") as outfile:
            json.dump(self.param_cnt, outfile, indent=4)

    def _make_child(self, full_param_vals:dict, success_rate:float, mutate:str,
                    parent_mute_param_vals:dict = None, max_step:int = 1,idx:int = 0) \
                        -> tuple[float,dict]:
        """Returns a tuple (success rate, mutable_param_vals).\n
        Mutate can be 'step', 'random', or 'identical'"""
        if(DEBUG_LVL>=2):
            child_Start_Time = time.time()
        if mutate == 'step':
            child_mute_param_vals = self._step_mutate(parent_mute_param_vals,max_step=max_step) 
            self.prev_used_params.append(child_mute_param_vals)
        elif mutate == 'random':
            self.prev_used_params = []
            child_mute_param_vals = self._random_mutate()
            self.prev_used_params.append(child_mute_param_vals)
        elif mutate == 'identical':
            child_mute_param_vals = parent_mute_param_vals
        else: raise Exception('no valid mutation chosen')
        full_param_vals.update(child_mute_param_vals)
        # monte carlo runs are exponentially related to success rate. 
        # Increasing the exponent makes the curve more severe. 
        # At the TARGET_SUCCESS_RATE, you'll get the MAX_MONTE_RUNS
        override_dict = {'monte_carlo_runs' : int(max(INITIAL_MONTE_RUNS,(min(MAX_MONTE_RUNS,
                            (MAX_MONTE_RUNS * (success_rate + (1-TARGET_SUCCESS_RATE)) ** 70))))) }
        # if we're on the first child of a set, the simulator will generate returns and feed them back. 
        # For the next children, that same set of returns will be reused
        if idx != 0:
            override_dict['returns']  = self.returns # pylint: disable=access-member-before-definition
        print(f"monte runs: {override_dict['monte_carlo_runs']}")
        new_simulator = Simulator(full_param_vals,override_dict)
        sim_results = new_simulator.main()
        if not sim_results: # Simulator returns empty dict when quit commmand given
            return (None,None)
        self.returns = sim_results['returns']
        if(DEBUG_LVL>=2):
            child_End_Time = time.time()
            print(f"child generation time: {round(child_End_Time-child_Start_Time,2)}")
        return sim_results['s_rate'],child_mute_param_vals


if __name__ == '__main__':
    algorithm = Algorithm(Model())
    algorithm.main()
