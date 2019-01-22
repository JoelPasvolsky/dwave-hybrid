# Copyright 2018 D-Wave Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import collections
import random
from itertools import cycle

import networkx as nx

from hybrid.core import Runnable, State
from hybrid.exceptions import EndOfStream
from hybrid import traits
from hybrid.utils import (
    bqm_induced_by, select_localsearch_adversaries, select_random_subgraph,
    chimera_tiles)

__all__ = [
    'IdentityDecomposer', 'EnergyImpactDecomposer', 'RandomSubproblemDecomposer',
    'TilingChimeraDecomposer', 'RandomConstraintDecomposer',
]

logger = logging.getLogger(__name__)


class IdentityDecomposer(Runnable, traits.ProblemDecomposer):
    """Selects a subproblem that is a full copy of the problem."""

    def next(self, state):
        return state.updated(subproblem=state.problem)


class EnergyImpactDecomposer(Runnable, traits.ProblemDecomposer):
    """Selects a subproblem of variables maximally contributing to the problem energy.

    The selection currently implemented does not ensure that the variables are connected
    in the problem graph.

    Args:
        max_size (int):
            Maximum number of variables in the subproblem.

        min_gain (int, optional, default=-inf):
            Minimum reduction required to BQM energy, given the current sample. A variable
            is included in the subproblem only if inverting its sample value reduces energy
            by at least this amount.

        rolling (bool, optional, default=True):
            Should successive calls for the same problem (but maybe different samples)
            produce subproblems on different variables rolling down the list of all variables
            sorted by decreasing impact?

        rolling_history (float, optional, default=0.1):
            Size of unrolled variables pool, as a fraction of the problem size (0.0 to 1.0).
            Determines the reset condition for subproblem unrolling.

        silent_reset (bool, optional, default=True):
            On unrolling reset condition, should `StopIteration` be raised together
            with resetting the subproblem generator?

    Examples:
        See examples on https://docs.ocean.dwavesys.com/projects/hybrid/en/latest/reference/decomposers.html#examples.
    """

    def __init__(self, max_size, min_gain=None,
                 rolling=True, rolling_history=0.1, silent_reset=True):

        super(EnergyImpactDecomposer, self).__init__()

        if rolling and rolling_history < 0.0 or rolling_history > 1.0:
            raise ValueError("rolling_history must be a float in range [0.0, 1.0]")

        self.max_size = max_size
        self.min_gain = min_gain
        self.rolling = rolling
        self.rolling_history = rolling_history
        self.silent_reset = silent_reset

        # variables unrolled so far
        self._unrolled_vars = set()
        self._rolling_bqm = None

    def __repr__(self):
        return (
            "{self}(max_size={self.max_size!r}, min_gain={self.min_gain!r}, "
            "rolling={self.rolling!r}, rolling_history={self.rolling_history!r})"
        ).format(self=self)

    def _reset_rolling(self, state):
        self._unrolled_vars.clear()
        self._rolling_bqm = state.problem

    def next(self, state):
        bqm = state.problem

        if bqm != self._rolling_bqm:
            self._reset_rolling(state)

        if self.max_size > len(bqm):
            raise ValueError("subproblem size cannot be greater than the problem size")

        sample = state.samples.change_vartype(bqm.vartype).first.sample
        variables = select_localsearch_adversaries(
            bqm, sample, min_gain=self.min_gain)

        if self.rolling and len(self._unrolled_vars) + self.max_size > self.rolling_history * len(bqm):
            logger.debug("rolling reset at unrolled history size = %d",
                         len(self._unrolled_vars))
            # reset before exception, to be ready on a subsequent call
            self._reset_rolling(state)
            if not self.silent_reset:
                raise EndOfStream

        novel_vars = [v for v in variables if v not in self._unrolled_vars]
        next_vars = novel_vars[:self.max_size]

        logger.debug("Selected %d subproblem variables: %r",
                     len(next_vars), next_vars)

        if self.rolling:
            self._unrolled_vars.update(next_vars)

        # induce sub-bqm based on selected variables and global sample
        subbqm = bqm_induced_by(bqm, next_vars, sample)
        return state.updated(subproblem=subbqm)


class RandomSubproblemDecomposer(Runnable, traits.ProblemDecomposer):
    """Select a subproblem of `size` random variables.

    The selection currently implemented does not ensure that the variables are connected
    in the problem graph.

    Args:
        size (int):
            Number of variables in the subproblem.

    Examples:
        See examples on https://docs.ocean.dwavesys.com/projects/hybrid/en/latest/reference/decomposers.html#examples.
    """

    def __init__(self, size):
        super(RandomSubproblemDecomposer, self).__init__()

        self.size = size

    def __repr__(self):
        return "{self}(size={self.size!r})".format(self=self)

    def next(self, state):
        bqm = state.problem

        if self.size > len(bqm):
            raise ValueError("subproblem size cannot be greater than the problem size")

        variables = select_random_subgraph(bqm, self.size)
        sample = state.samples.change_vartype(bqm.vartype).first.sample
        subbqm = bqm_induced_by(bqm, variables, sample)
        return state.updated(subproblem=subbqm)


class TilingChimeraDecomposer(Runnable, traits.ProblemDecomposer, traits.EmbeddingProducing):
    """Returns sequential Chimera lattices that tile the initial problem.

    A Chimera lattice is an m-by-n grid of Chimera tiles, where each tile is a bipartite graph
    with shores of size t. The problem is decomposed into a sequence of subproblems with variables
    belonging to the Chimera lattices that tile the problem Chimera lattice. For example,
    a 2x2 Chimera lattice could be tiled 64 times (8x8) on a fully-yielded D-Wave 2000Q system (16x16).

    Args:
        size (int, optional, default=(4,4,4)):
            Size of the Chimera lattice as (m, n, t), where m is the number of rows,
            n the columns, and t the size of shore in the Chimera lattice.
        loop (Bool, optional, default=True):
            Cycle continually through the tiles.

    Examples:
        See examples on https://docs.ocean.dwavesys.com/projects/hybrid/en/latest/reference/decomposers.html#examples.
    """

    def __init__(self, size=(4,4,4), loop=True):
        """Size C(n,m,t) defines a Chimera subgraph returned with each call."""
        super(TilingChimeraDecomposer, self).__init__()
        self.size = size
        self.loop = loop
        self.blocks = None

    def __repr__(self):
        return "{self}(size={self.size!r}, loop={self.loop!r})".format(self=self)

    def init(self, state):
        self.blocks = iter(chimera_tiles(state.problem, *self.size).items())
        if self.loop:
            self.blocks = cycle(self.blocks)

    def next(self, state):
        """Each call returns a subsequent block of size `self.size` Chimera cells."""
        bqm = state.problem
        pos, embedding = next(self.blocks)
        variables = embedding.keys()
        sample = state.samples.change_vartype(bqm.vartype).first.sample
        subbqm = bqm_induced_by(bqm, variables, sample)
        return state.updated(subproblem=subbqm, embedding=embedding)


class RandomConstraintDecomposer(Runnable, traits.ProblemDecomposer):
    """Selects variables randomly as constrained by groupings.

    By grouping related variables, the problem's structure can guide the random selection
    of variables so subproblems are related to the problem's constraints.

    Args:
        size (int):
            Number of variables in the subproblem.
        constraints (list[set]):
            Groups of variables in the BQM, as a list of sets, where each set is associated
            with a constraint.

    Examples:
        See examples on https://docs.ocean.dwavesys.com/projects/hybrid/en/latest/reference/decomposers.html#examples.
    """

    def __init__(self, size, constraints):
        super(RandomConstraintDecomposer, self).__init__()

        self.size = size

        if not isinstance(constraints, collections.Sequence):
            raise TypeError("constraints should be a list of containers")
        if any(len(const) > size for const in constraints):
            raise ValueError("size must be able to contain the largest constraint")
        self.constraints = constraints

    def __repr__(self):
        return "{self}(size={self.size!r}, constraints={self.constraints!r})".format(self=self)

    def init(self, state):
        if self.size > len(state.problem):
            raise ValueError("subproblem size cannot be greater than the problem size")

        # get the connectivity between the constraint components
        self.constraint_graph = CG = nx.Graph()
        for ci, const in enumerate(self.constraints):
            for i in range(ci+1, len(self.constraints)):
                if any(v in const for v in self.constraints[i]):
                    CG.add_edge(i, ci)

    def next(self, state):
        CG = self.constraint_graph
        size = self.size
        constraints = self.constraints
        bqm = state.problem

        # get a random constraint to start with.
        # for some reason random.choice(CG.nodes) does not work, so we rely on the fact that our
        # graph is index-labeled
        n = random.choice(range(len(CG)))

        if len(constraints[n]) > size:
            raise NotImplementedError

        # starting from our constraint, do a breadth-first search adding constraints until our max
        # size is reached
        variables = set(constraints[n])
        for _, ci in nx.bfs_edges(CG, n):
            proposed = [v for v in constraints[ci] if v not in variables]
            if len(proposed) + len(variables) <= size:
                variables.add(proposed)
            if len(variables) == size:
                # can exit early
                break

        sample = state.samples.change_vartype(bqm.vartype).first.sample
        subbqm = bqm_induced_by(bqm, variables, sample)
        return state.updated(subproblem=subbqm)
