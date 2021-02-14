"""
Definition of minimizers, wrappers etc.

"""

#  Copyright (c) 2021 zfit
import collections
import copy
import functools
import inspect
import math
import os
import warnings
from collections import OrderedDict
from typing import List, Union, Iterable, Mapping, Callable, Tuple, Optional

import numpy as np
from ordered_set import OrderedSet

from .evaluation import LossEval
from .fitresult import FitResult
from .interface import ZfitMinimizer, ZfitResult
from .strategy import FailMinimizeNaN, ZfitStrategy, PushbackStrategy
from .termination import EDM
from ..core.interfaces import ZfitLoss, ZfitParameter
from ..core.parameter import set_values, convert_to_parameter
from ..settings import run
from ..util import ztyping
from ..util.container import convert_to_container
from ..util.exception import MinimizeNotImplementedError, MinimizeStepNotImplementedError, MinimizerSubclassingError, \
    FromResultNotImplemented, ParameterNotIndependentError

DefaultStrategy = PushbackStrategy


def minimize_supports(*, from_result: Union[bool] = False) -> Callable:
    """Decorator: Add (mandatory for some methods) on a method to control what it can handle.

    If any of the flags is set to False, it will check the arguments and, in case they match a flag
    (say if a *norm_range* is passed while the *norm_range* flag is set to `False`), it will
    raise a corresponding exception (in this example a `NormRangeNotImplementedError`) that will
    be catched by an earlier function that knows how to handle things.

    Args:
        from_result: Specify whether the minimize method can handle a FitResult instead of a loss as a loss. There are
            three options:
            - False: This is the default and means that _no FitResult will ever come true_. The minimizer handles the
              initial parameter values himselves.
            - 'same': If 'same' is set, a `FitResult` will only come through if it was created with the *exact* same
              type as
        multiple_limits: If False, only simple limits are to be expected and no iteration is
            therefore required.
    """

    def wrapper(func):

        parameters = inspect.signature(func).parameters
        keys = list(parameters.keys())
        if from_result is True or 'loss' not in keys:  # no loss as parameters -> no problem
            new_func = func
        else:
            loss_index = keys.index('loss')

            @functools.wraps(func)
            def new_func(*args, **kwargs):
                self_minimizer = args[0]
                can_handle = True
                loss_is_arg = len(args) > loss_index
                if loss_is_arg:
                    loss = args[loss_index]
                else:
                    loss = kwargs['loss']

                if isinstance(loss, FitResult):
                    if from_result == 'same':
                        if not type(self_minimizer) == type(loss.minimizer):
                            can_handle = False
                    elif not from_result:
                        can_handle = False
                    else:
                        raise ValueError("from_result has to be True, False or 'same'")
                if not can_handle:
                    raise FromResultNotImplemented
                return func(*args, **kwargs)

        new_func.__wrapped__ = minimize_supports
        return new_func

    return wrapper


_Minimizer_CHECK_HAS_SUPPORT = {}


def _Minimizer_register_check_support(has_support: bool):
    """Marks a method that the subclass either *has* to or *can't* use the `@supports` decorator.

    Args:
        has_support: If True, flags that it **requires** the `@supports` decorator. If False,
            flags that the `@supports` decorator is **not allowed**.

    """
    if not isinstance(has_support, bool):
        raise TypeError("Has to be boolean.")

    def register(func):
        """Register a method to be checked to (if True) *has* `support` or (if False) has *no* `support`.

        Args:
            func:

        Returns:
            Function:
        """
        name = func.__name__
        _Minimizer_CHECK_HAS_SUPPORT[name] = has_support
        func.__wrapped__ = _Minimizer_register_check_support
        return func

    return register


class BaseMinimizer(ZfitMinimizer):
    """Minimizer for loss functions.

    Additional `minimizer_options` (given as **kwargs) can be accessed and changed via the
    attribute (dict) `minimizer.minimizer_options`.
    """
    _DEFAULT_TOLERANCE = 1e-3

    def __init__(self, name, tolerance, verbosity, minimizer_options, criterion=None, strategy=None, maxiter=None,
                 **kwargs):
        super().__init__(**kwargs)
        self._n_iter_per_param = 1000
        if name is None:
            name = repr(self.__class__)[:-2].split(".")[-1]
        if strategy is None:
            strategy = DefaultStrategy()
        if not isinstance(strategy, ZfitStrategy):
            raise TypeError(f"strategy {strategy} is not an instance of ZfitStrategy.")
        self.strategy = strategy
        self.name = name
        if tolerance is None:
            tolerance = self._DEFAULT_TOLERANCE
        self.tolerance = tolerance
        self.verbosity = 5 if verbosity is None else verbosity
        if minimizer_options is None:
            minimizer_options = {}
        self.minimizer_options = minimizer_options
        self.maxiter = maxiter
        self.criterion = EDM if criterion is None else criterion

    @classmethod
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # check if subclass has decorator if required
        cls._subclass_check_support(methods_to_check=_Minimizer_CHECK_HAS_SUPPORT,
                                    wrapper_not_overwritten=_Minimizer_register_check_support)

    @classmethod
    def _subclass_check_support(cls, methods_to_check, wrapper_not_overwritten):
        for method_name, has_support in methods_to_check.items():
            if not hasattr(cls, method_name):
                continue  # skip if only subclass requires it
            method = getattr(cls, method_name)
            if hasattr(method, "__wrapped__"):
                if method.__wrapped__ == wrapper_not_overwritten:
                    continue  # not overwritten, fine

            # here means: overwritten
            if hasattr(method, "__wrapped__"):
                if method.__wrapped__ == minimize_supports:
                    if has_support:
                        continue  # needs support, has been wrapped
                    else:
                        raise MinimizerSubclassingError("Method {} has been wrapped with minimize_supports "
                                                        "but is not allowed to. Has to handle all "
                                                        "arguments.".format(method_name))
                elif has_support:
                    raise MinimizerSubclassingError(f"Method {method_name} has been overwritten and *has to* be "
                                                    "wrapped by `@minimize_supports` decorator (don't forget () )"
                                                    "to call the decorator as it takes arguments")
                elif not has_support:
                    continue  # no support, has not been wrapped with
            else:
                if not has_support:
                    continue  # not wrapped, no support, need no

            # if we reach this points, somethings was implemented wrongly
            raise MinimizerSubclassingError(f"Method {method_name} has not been correctly wrapped with "
                                            f"@minimize_supports ")

    def _check_convert_input(self, loss: ZfitLoss, params, init=None, only_floating=True
                             ) -> Tuple[ZfitLoss, Iterable[ZfitParameter], Union[None, FitResult]]:
        if not isinstance(loss, ZfitLoss):
            if not callable(loss):
                raise TypeError("Given Loss has to  be a ZfitLoss or a callable.")
            elif params is None:
                raise ValueError("If the loss is a callable, the params cannot be None.")

            if not isinstance(params, Mapping):
                values = convert_to_container(params)
                names = [None] * len(params)
            else:
                values = list(params.values())
                names = list(params.keys())
            params = [convert_to_parameter(value=val, name=name, prefer_constant=False)
                      for val, name in zip(values, names)]

            from zfit.core.loss import SimpleLoss
            if hasattr(loss, 'errordef'):
                errordef = loss.errordef
            else:
                errordef = 0.5
            loss = SimpleLoss(func=loss, deps=params, errordef=errordef)

        to_set_param_values = {}
        if params is None:
            params = loss.get_params(only_floating=only_floating)
        else:
            if isinstance(params, collections.Mapping):
                param_values = params
                to_set_param_values = {p: val for p, val in param_values.items() if val is not None}
                try:
                    set_values(list(to_set_param_values), list(to_set_param_values.values()))
                except ParameterNotIndependentError as error:
                    not_indep_and_set = {p for p, val in param_values.items() if val is not None and not p.independent}
                    raise ParameterNotIndependentError(f"Cannot set parameter {not_indep_and_set} to a value as they"
                                                       f" are not independent. The following `param` argument was"
                                                       f" given: {params}."
                                                       f""
                                                       f"Original error"
                                                       f"--------------"
                                                       f"{error}") from error
            else:
                params = convert_to_container(params, container=OrderedSet)

            # now extract all the independent parameters
            params = list(OrderedSet.union(*(p.get_params(only_floating=only_floating) for p in params)))

        # set the parameter values from the init
        if init is not None:
            # don't set the user set
            params_to_set = OrderedSet(params).intersection(OrderedSet(init.params)) - OrderedSet(to_set_param_values)
            set_values(params_to_set, init)
        if only_floating:
            params = self._filter_floating_params(params)
        if not params:
            raise RuntimeError("No parameter for minimization given/found. Cannot minimize.")
        params = list(params)
        return loss, params, init

    @staticmethod
    def _filter_floating_params(params):
        non_floating = [param for param in params if not param.floating]
        if non_floating:  # legacy warning
            warnings.warn(f"CHANGED BEHAVIOR! Non-floating parameters {non_floating} will not be used in the "
                          f"minimization.")
        return [param for param in params if param.floating]

    @staticmethod
    def _extract_load_method(params):
        return [param.load for param in params]

    @staticmethod
    def _extract_param_names(params):
        return [param.name for param in params]

    def _check_gradients(self, params, gradients):
        non_dependents = [param for param, grad in zip(params, gradients) if grad is None]
        if non_dependents:
            raise ValueError("Invalid gradients for the following parameters: {}"
                             "The function does not depend on them. Probably a Tensor"
                             "instead of a `CompositeParameter` was created implicitly.".format(non_dependents))

    @property
    def tolerance(self):
        return self._tolerance

    @tolerance.setter
    def tolerance(self, tolerance):
        self._tolerance = tolerance

    @staticmethod
    def _extract_start_values(params):
        """Extract the current value if defined, otherwise random.

        Arguments:
            params:

        Return:
            list(const): the current value of parameters
        """
        values = [p for p in params]
        return values

    @staticmethod
    def _update_params(params: Union[Iterable[ZfitParameter]], values: Union[Iterable[float], np.ndarray]) -> List[
        ZfitParameter]:
        """Update `params` with `values`. Returns the assign op (if `use_op`, otherwise use a session to load the value.

        Args:
            params: The parameters to be updated
            values: New values for the parameters.

        Returns:
            List of assign operations if `use_op`, otherwise empty. The output
                can therefore be directly used as argument to :py:func:`~tf.control_dependencies`.
        """
        if len(params) == 1 and len(values) > 1:
            values = (values,)  # iteration will be correctly
        for param, value in zip(params, values):
            param.set_value(value)

        return params

    def minimize(self, loss: ZfitLoss, params: ztyping.ParamsTypeOpt = None, init: FitResult = None) -> FitResult:
        """Fully minimize the `loss` with respect to `params`.

        Args:
            loss: Loss to be minimized.
            params: The parameters with respect to which to
                minimize the `loss`. If `None`, the parameters will be taken from the `loss`.
            init: A result of a previous minimization that provides auxiliary information such as the starting point for
                the parameters

        Returns:
            The fit result.
        """
        if isinstance(loss, ZfitResult):
            init = loss  # make the names correct
            loss = init.loss

        loss, params, init = self._check_convert_input(loss=loss, params=params, init=init, only_floating=True)

        return self._call_minimize(loss=loss, params=params, init=init)

    def _call_minimize(self, loss, params, init):

        try:
            try:
                return self._minimize(loss=loss, params=params, init=init)
            except TypeError as error:
                if "got an unexpected keyword argument 'init'" in error.args[0]:
                    warnings.warn(
                        '_minimize has to take an `init` argument. This will be mandatory in the future, please'
                        ' change the signature accordingly.', category=FutureWarning, stacklevel=2)
                    return self._minimize(loss=loss, params=params)
                else:
                    raise
        except (FailMinimizeNaN, RuntimeError):  # iminuit raises RuntimeError if user raises Error
            fail_result = self.strategy.fit_result
            if fail_result is not None:
                return fail_result
            else:
                raise

    def copy(self):
        return copy.copy(self)

    @_Minimizer_register_check_support(True)
    def _minimize(self, loss, params, init):
        raise MinimizeNotImplementedError

    def __str__(self) -> str:
        string = f'<{self.name} strategy={self.strategy} tolerance={self.tolerance}>'
        return string

    def get_maxiter(self, n):
        maxiter = self.maxiter
        if callable(maxiter):
            maxiter = maxiter(n)
        elif maxiter == 'auto':
            maxiter = self._n_iter_per_param * n
        return maxiter

    def create_evaluator(self, loss: ZfitLoss, params: ztyping.ParametersType) -> LossEval:
        evaluator = LossEval(loss=loss,
                             params=params,
                             strategy=self.strategy,
                             do_print=self.verbosity > 8,
                             maxiter=self.get_maxiter(len(params)),
                             minimizer=self)
        return evaluator

    def _update_tol_inplace(self, criterion_value, internal_tol):
        tol_factor = min([max([self.tolerance / criterion_value * 0.3, 1e-2]), 0.2])
        for tol in internal_tol:
            if tol in ('gtol', 'xtol'):
                internal_tol[tol] *= math.sqrt(tol_factor)
            else:
                internal_tol[tol] *= tol_factor

    def create_criterion(self, loss, params):
        criterion = self.criterion(tolerance=self.tolerance, loss=loss, params=params)
        return criterion


class BaseStepMinimizer(BaseMinimizer):

    @minimize_supports()
    def _minimize(self, loss, params, init):
        n_old_vals = 10
        changes = collections.deque(np.ones(n_old_vals))
        last_val = -10
        n_steps = 0
        criterion = self.criterion(tolerance=self.tolerance, loss=loss, params=params)
        edm = self.tolerance * 1000
        sum_changes = np.sum(changes)
        inv_hesse = None
        while (sum_changes > self.tolerance or edm > self.tolerance) and n_steps < self.get_maxiter(len(params)):
            cur_val = run(self.step(loss=loss, params=params))
            if sum_changes < self.tolerance and n_steps % 5:
                xvalues = np.array(run(params))
                hesse = run(loss.hessian(params))
                inv_hesse = np.linalg.inv(hesse)
                edm = criterion.calculateV1(value=cur_val, xvalues=xvalues, grad=run(loss.gradients(params)),
                                            inv_hesse=inv_hesse)
            changes.popleft()
            changes.append(abs(cur_val - last_val))
            sum_changes = np.sum(changes)
            last_val = cur_val
            n_steps += 1
        fmin = cur_val

        # compose fit result
        message = "successful finished"
        are_unique = len(set([run(change) for change in changes])) > 1  # values didn't change...
        if not are_unique:
            message = "Loss unchanged for last {} steps".format(n_old_vals)

        success = are_unique
        status = 0 if success else 10
        xvalues = np.array(run(params))
        info = {'success': success, 'message': message, 'n_eval': n_steps, 'inv_hesse': inv_hesse}

        params = OrderedDict((p, val) for p, val in zip(params, xvalues))

        return FitResult(params=params, edm=edm, fmin=fmin, info=info,
                         converged=success, status=status,
                         loss=loss, minimizer=self.copy())

    def step(self, loss, params: ztyping.ParamsOrNameType = None, init: FitResult = None):
        """Perform a single step in the minimization (if implemented).

        Args:
            params:

        Returns:

        Raises:
            MinimizeStepNotImplementedError: if the `step` method is not implemented in the minimizer.
        """
        loss, params, init = self._check_convert_input(loss, params, init=init)

        return self._step(loss, params=params, init=init)

    def _step(self, loss, params, init):
        raise MinimizeStepNotImplementedError


class NOT_SUPPORTED:
    def __new__(cls, *args, **kwargs):
        raise RuntimeError("Should never be instantated.")


def print_minimization_status(converged, criterion, evaluator, i, fmin,
                              internal_tol: Optional[Mapping[str, float]] = None):
    internal_tol = {} if internal_tol is None else internal_tol
    tolerances_str = ', '.join(f'{tol}={val:.3g}' for tol, val in internal_tol.items())
    print(f"{f'CONVERGED{os.linesep}' if converged else ''}"
          f"Finished iteration {i}, niter={evaluator.niter}, fmin={fmin:.7g},"
          f" {criterion.name}={criterion.last_value:.3g} {tolerances_str}")
