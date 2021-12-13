# -*- coding: utf-8 -*-
# Copyright (C) 2021 by SCICO Developers
# All rights reserved. BSD 3-clause License.
# This file is part of the SCICO package. Details of the copyright and
# user license can be found in the 'LICENSE' file distributed with the
# package.

"""Linearized ADMM solver."""

# Needed to annotate a class method that returns the encapsulating class;
# see https://www.python.org/dev/peps/pep-0563/
from __future__ import annotations

from typing import Callable, List, Optional, Tuple, Union

import scico.numpy as snp
from scico.blockarray import BlockArray
from scico.diagnostics import IterationStats
from scico.functional import Functional
from scico.linop import LinearOperator
from scico.numpy.linalg import norm
from scico.typing import JaxArray
from scico.util import Timer, ensure_on_device

__author__ = """\n""".join(
    ["Luke Pfister <luke.pfister@gmail.com>", "Brendt Wohlberg <brendt@ieee.org>"]
)


class LinearizedADMM:
    r"""Linearized alternating direction method of multipliers algorithm.

    |

    Solve an optimization problem of the form

    .. math::
        \argmin_{\mb{x}} \; f(\mb{x}) + g(C \mb{x}) \;,

    where :math:`f` and :math:`g` are instances of :class:`.Functional`,
    (in most cases :math:`f` will, more specifically be an an instance
    of :class:`.Loss`), and :math:`C` is an instance of
    :class:`.LinearOperator`.

    The optimization problem is solved by introducing the splitting
    :math:`\mb{z} = C \mb{x}` and solving

    .. math::
        \argmin_{\mb{x}, \mb{z}} \; f(\mb{x}) + g(\mb{z}) \;
       \text{such that}\; C \mb{x} = \mb{z} \;,

    via a linearized ADMM algorithm :cite:`yang-2012-linearized`
    :cite:`parikh-2014-proximal` (Sec. 4.4.2) consisting of the
    iterations (see :meth:`step`)

    .. math::
       \begin{aligned}
       \mb{x}^{(k+1)} &= \mathrm{prox}_{\mu f} \left( \mb{x}^{(k)} -
       (\mu / \nu) C^T \left(C \mb{x}^{(k)} - \mb{z}^{(k)} + \mb{u}^{(k)}
       \right) \right) \\
       \mb{z}^{(k+1)} &= \mathrm{prox}_{\nu g} \left(C \mb{x}^{(k+1)} +
       \mb{u}^{(k)} \right) \\
       \mb{u}^{(k+1)} &=  \mb{u}^{(k)} + C \mb{x}^{(k+1)} -
       \mb{z}^{(k+1)}  \;.
       \end{aligned}

    Parameters :math:`\mu` and :math:`\nu` are required to satisfy

    .. math::
       0 < \mu < \nu \| C \|_2^{-2} \;.


    Attributes:
        f (:class:`.Functional`): Functional :math:`f` (usually a
           :class:`.Loss`).
        g (:class:`.Functional`): Functional :math:`g`.
        C (:class:`.LinearOperator`): :math:`C` operator.
        itnum (int): Iteration counter.
        maxiter (int): Number of ADMM outer-loop iterations.
        timer (:class:`.Timer`): Iteration timer.
        mu (scalar): First algorithm parameter.
        nu (scalar): Second algorithm parameter.
        u (array-like): Scaled Lagrange multipliers :math:`\mb{u}` at
           current iteration.
        x (array-like): Solution variable.
        z (array-like): Auxiliary variables :math:`\mb{z}` at current
          iteration.
        z_old (array-like): Auxiliary variables :math:`\mb{z}` at
          previous iteration.
    """

    def __init__(
        self,
        f: Functional,
        g: Functional,
        C: LinearOperator,
        mu: float,
        nu: float,
        x0: Optional[Union[JaxArray, BlockArray]] = None,
        maxiter: int = 100,
        verbose: bool = False,
        itstat: Optional[Tuple[dict, Callable]] = None,
    ):
        r"""Initialize a :class:`LinearizedADMM` object.

        Args:
            f: Functional :math:`f` (usually a loss function).
            g: Functional :math:`g`.
            C: Operator :math:`C`.
            mu: First algorithm parameter.
            nu: Second algorithm parameter.
            x0: Starting point for :math:`\mb{x}`. If None, defaults to
                an array of zeros.
            maxiter: Number of ADMM outer-loop iterations. Default: 100.
            verbose: Flag indicating whether iteration statistics should
                be displayed.
            itstat: A tuple (`fieldspec`, `insertfunc`), where `fieldspec`
                is a dict suitable for passing to the `fields` argument
                of the :class:`.diagnostics.IterationStats` initializer,
                and `insertfunc` is a function with two parameters, an
                integer and a LinearizedADMM object, responsible for
                constructing a tuple ready for insertion into the
                :class:`.diagnostics.IterationStats` object. If None,
                default values are used for the tuple components.
        """
        self.f: Functional = f
        self.g: Functional = g
        self.C: LinearOperator = C
        self.mu: float = mu
        self.nu: float = nu
        self.itnum: int = 0
        self.maxiter: int = maxiter
        self.timer: Timer = Timer()
        self.verbose: bool = verbose

        if itstat:
            itstat_dict = itstat[0]
            itstat_func = itstat[1]
        elif itstat is None:
            if g.has_eval:
                itstat_dict = {
                    "Iter": "%d",
                    "Time": "%8.2e",
                    "Objective": "%8.3e",
                    "Primal Rsdl": "%8.3e",
                    "Dual Rsdl": "%8.3e",
                }

                def itstat_func(obj):
                    return (
                        obj.itnum,
                        obj.timer.elapsed(),
                        obj.objective(),
                        obj.norm_primal_residual(),
                        obj.norm_dual_residual(),
                    )

            else:
                # At least one 'g' can't be evaluated, so drop objective from the default itstat
                itstat_dict = {
                    "Iter": "%d",
                    "Time": "%8.1e",
                    "Primal Rsdl": "%8.3e",
                    "Dual Rsdl": "%8.3e",
                }

                def itstat_func(obj):
                    return (
                        obj.i,
                        obj.timer.elapsed(),
                        obj.norm_primal_residual(),
                        obj.norm_dual_residual(),
                    )

        self.itstat_object = IterationStats(itstat_dict, display=verbose)
        self.itstat_insert_func = itstat_func

        if x0 is None:
            input_shape = C.input_shape
            dtype = C.input_dtype
            x0 = snp.zeros(input_shape, dtype=dtype)
        self.x = ensure_on_device(x0)
        self.z, self.z_old = self.z_init(self.x)
        self.u = self.u_init(self.x)

    def objective(
        self,
        x: Optional[Union[JaxArray, BlockArray]] = None,
        z: Optional[List[Union[JaxArray, BlockArray]]] = None,
    ) -> float:
        r"""Evaluate the objective function.


        Evaluate the objective function

        .. math::
            f(\mb{x}) + g(\mb{z}) \;.


        Args:
            x: Point at which to evaluate objective function. If `None`,
               the objective is evaluated at the current iterate
               :code:`self.x`.
            z: Point at which to evaluate objective function. If `None`,
               the objective is evaluated at the current iterate
               :code:`self.z`.

        Returns:
            scalar: Current value of the objective function.
        """
        if (x is None) != (z is None):
            raise ValueError("Both or neither of x and z must be supplied")
        if x is None:
            x = self.x
            z = self.z
        out = 0.0
        if self.f:
            out += self.f(x)
        out += self.g(z)
        return out

    def norm_primal_residual(self, x: Optional[Union[JaxArray, BlockArray]] = None) -> float:
        r"""Compute the :math:`\ell_2` norm of the primal residual.


        Compute the :math:`\ell_2` norm of the primal residual

        .. math::
            \norm{C \mb{x} - \mb{z}}_2 \;.

        Args:
            x: Point at which to evaluate primal residual. If `None`, the
               primal residual is evaluated at the currentiterate
               :code:`self.x`.

        Returns:
            Current value of primal residual.
        """
        if x is None:
            x = self.x

        return norm(self.C(self.x) - self.z)

    def norm_dual_residual(self) -> float:
        r"""Compute the :math:`\ell_2` norm of the dual residual.

        Compute the :math:`\ell_2` norm of the dual residual

        .. math::
            \norm{\mb{z}^{(k)} - \mb{z}^{(k-1)}}_2 \;.

        Returns:
            Current value of dual residual.
        """
        return norm(self.C.adj(self.z - self.z_old))

    def z_init(self, x0: Union[JaxArray, BlockArray]):
        r"""Initialize auxiliary variable :math:`\mb{z}`.

        Initialized to

        .. math::
            \mb{z} = C \mb{x}^{(0)} \;.

        :code:`z` and :code:`z_old` are initialized to the same value.

        Args:
            x0: Starting point for :math:`\mb{x}`.
        """
        z = self.C(x0)
        z_old = z.copy()
        return z, z_old

    def u_init(self, x0: Union[JaxArray, BlockArray]):
        r"""Initialize scaled Lagrange multiplier :math:`\mb{u}`.

        Initialized to

        .. math::
            \mb{u} = C \mb{x}^{(0)} \;.


        Args:
            x0: Starting point for :math:`\mb{x}`.
        """
        u = snp.zeros(self.C.output_shape, dtype=self.C.output_dtype)
        return u

    def step(self):
        r"""Perform a single linearized ADMM iteration.

        The primary variable :math:`\mb{x}` is updated by computing

        .. math::
            \mb{x}^{(k+1)} = \mathrm{prox}_{\mu f} \left( \mb{x}^{(k)} -
            (\mu / \nu) A^T \left(A \mb{x}^{(k)} - \mb{z}^{(k)} +
            \mb{u}^{(k)} \right) \right) \;.

        The auxiliary variable is updated according to

        .. math::
            \mb{z}^{(k+1)} = \mathrm{prox}_{\nu g} \left(A \mb{x}^{(k+1)}
            + \mb{u}^{(k)} \right) \;,

        and the scaled Lagrange multiplier is updated according to

        .. math::
            \mb{u}^{(k+1)} =  \mb{u}^{(k)} + C \mb{x}^{(k+1)} -
            \mb{z}^{(k+1)} \;.
        """
        proxarg = self.x - (self.mu / self.nu) * self.C.conj().T(self.C(self.x) - self.z + self.u)
        self.x = self.f.prox(proxarg, self.mu, v0=self.x)

        self.z_old = self.z.copy()
        Cx = self.C(self.x)
        self.z = self.g.prox(Cx + self.u, self.nu, v0=self.z)
        self.u = self.u + Cx - self.z

    def solve(
        self,
        callback: Optional[Callable[[LinearizedADMM], None]] = None,
    ) -> Union[JaxArray, BlockArray]:
        r"""Initialize and run the LinearizedADMM algorithm.

        Initialize and run the LinearizedADMM algorithm for a total of
        ``self.maxiter`` iterations.

        Args:
            callback: An optional callback function, taking an a single
              argument of type :class:`LinearizedADMM`, that is called
              at the end of every iteration.

        Returns:
            Computed solution.
        """
        self.timer.start()
        for self.itnum in range(self.itnum, self.itnum + self.maxiter):
            self.step()
            self.itstat_object.insert(self.itstat_insert_func(self))
            if callback:
                self.timer.stop()
                callback(self)
                self.timer.start()
        self.timer.stop()
        self.itnum += 1
        return self.x