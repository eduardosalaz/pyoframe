from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import pandas as pd
import polars as pl
import pyoptinterface as poi

from pyoframe.constants import (
    CONST_TERM,
    SUPPORTED_SOLVER_TYPES,
    SUPPORTED_SOLVERS,
    Config,
    ObjSense,
    ObjSenseValue,
    PyoframeError,
    VType,
)
from pyoframe.core import Constraint, Variable
from pyoframe.model_element import ModelElement, ModelElementWithId
from pyoframe.objective import Objective
from pyoframe.util import Container, NamedVariableMapper, for_solvers, get_obj_repr


class Model:
    """
    The object that holds all the variables, constraints, and the objective.

    Parameters:
        name:
            The name of the model. Currently it is not used for much.
        solver:
            The solver to use. If `None`, `Config.default_solver` will be used.
            If `Config.default_solver` has not been set (`None`), Pyoframe will try to detect whichever solver is already installed.
        solver_env:
            Gurobi only: a dictionary of parameters to set when creating the Gurobi environment.
        use_var_names:
            Whether to pass variable names to the solver. Set to `True` if you'd like outputs from e.g. `Model.write()` to be legible.
            Does not work with HiGHS (see [here](https://github.com/Bravos-Power/pyoframe/issues/102#issuecomment-2727521430)).
        sense:
            Either "min" or "max". Indicates whether it's a minmization or maximization problem.
            Typically, this parameter can be omitted (`None`) as it will automatically be
            set when the objective is set using `.minimize` or `.maximize`.

    Examples:
        >>> m = pf.Model()
        >>> m.X = pf.Variable()
        >>> m.my_constraint = m.X <= 10
        >>> m
        <Model vars=1 constrs=1 objective=False>

        Try setting the Gurobi license:
        >>> m = pf.Model(solver="gurobi", solver_env=dict(ComputeServer="myserver", ServerPassword="mypassword"))
        Traceback (most recent call last):
        ...
        RuntimeError: Could not resolve host: myserver (code 6, command POST http://myserver/api/v1/cluster/jobs)
    """

    _reserved_attributes = [
        "_variables",
        "_constraints",
        "_objective",
        "var_map",
        "io_mappers",
        "name",
        "solver",
        "poi",
        "params",
        "result",
        "attr",
        "sense",
        "objective",
        "_use_var_names",
        "ONE",
        "solver_name",
        "minimize",
        "maximize",
    ]

    def __init__(
        self,
        name=None,
        solver: Optional[SUPPORTED_SOLVER_TYPES] = None,
        solver_env: Optional[Dict[str, str]] = None,
        use_var_names=False,
        sense: Union[ObjSense, ObjSenseValue, None] = None,
    ):
        self.poi, self.solver_name = Model.create_poi_model(solver, solver_env)
        self._variables: List[Variable] = []
        self._constraints: List[Constraint] = []
        self.sense = ObjSense(sense) if sense is not None else None
        self._objective: Optional[Objective] = None
        self.var_map = (
            NamedVariableMapper(Variable) if Config.print_uses_variable_names else None
        )
        self.name = name

        self.params = Container(self._set_param, self._get_param)
        self.attr = Container(self._set_attr, self._get_attr)
        self._use_var_names = use_var_names

    @property
    def use_var_names(self):
        return self._use_var_names

    @classmethod
    def create_poi_model(
        cls, solver: Optional[str], solver_env: Optional[Dict[str, str]]
    ):
        if solver is None:
            if Config.default_solver is None:
                for solver_option in SUPPORTED_SOLVERS:
                    try:
                        return cls.create_poi_model(solver_option, solver_env)
                    except RuntimeError:
                        pass
                raise ValueError(
                    'Could not automatically find a solver. Is one installed? If so, specify which one: e.g. Model(solver="gurobi")'
                )
            else:
                solver = Config.default_solver

        solver = solver.lower()
        if solver == "gurobi":
            from pyoptinterface import gurobi

            if solver_env is None:
                model = gurobi.Model()
            else:
                env = gurobi.Env(empty=True)
                for key, value in solver_env.items():
                    env.set_raw_parameter(key, value)
                env.start()
                model = gurobi.Model(env)
        elif solver == "highs":
            from pyoptinterface import highs

            model = highs.Model()
        elif solver == "ipopt":
            from pyoptinterface import ipopt

            model = ipopt.Model()
        else:
            raise ValueError(
                f"Solver {solver} not recognized or supported."
            )  # pragma: no cover

        constant_var = model.add_variable(lb=1, ub=1, name="ONE")
        if constant_var.index != CONST_TERM:
            raise ValueError(
                "The first variable should have index 0."
            )  # pragma: no cover
        return model, solver

    @property
    def variables(self) -> List[Variable]:
        return self._variables

    @property
    def binary_variables(self) -> Iterable[Variable]:
        """
        Examples:
            >>> m = pf.Model()
            >>> m.X = pf.Variable(vtype=pf.VType.BINARY)
            >>> m.Y = pf.Variable()
            >>> len(list(m.binary_variables))
            1
        """
        return (v for v in self.variables if v.vtype == VType.BINARY)

    @property
    def integer_variables(self) -> Iterable[Variable]:
        """
        Examples:
            >>> m = pf.Model()
            >>> m.X = pf.Variable(vtype=pf.VType.INTEGER)
            >>> m.Y = pf.Variable()
            >>> len(list(m.integer_variables))
            1
        """
        return (v for v in self.variables if v.vtype == VType.INTEGER)

    @property
    def constraints(self):
        return self._constraints

    @property
    def objective(self):
        return self._objective

    @objective.setter
    def objective(self, value):
        if self._objective is not None and (
            not isinstance(value, Objective) or not value._constructive
        ):
            raise ValueError("An objective already exists. Use += or -= to modify it.")
        if not isinstance(value, Objective):
            value = Objective(value)
        self._objective = value
        value.on_add_to_model(self, "objective")

    @property
    def minimize(self):
        if self.sense != ObjSense.MIN:
            raise ValueError("Can't get .minimize in a maximization problem.")
        return self._objective

    @minimize.setter
    def minimize(self, value):
        if self.sense is None:
            self.sense = ObjSense.MIN
        if self.sense != ObjSense.MIN:
            raise ValueError("Can't set .minimize in a maximization problem.")
        self.objective = value

    @property
    def maximize(self):
        if self.sense != ObjSense.MAX:
            raise ValueError("Can't get .maximize in a minimization problem.")
        return self._objective

    @maximize.setter
    def maximize(self, value):
        if self.sense is None:
            self.sense = ObjSense.MAX
        if self.sense != ObjSense.MAX:
            raise ValueError("Can't set .maximize in a minimization problem.")
        self.objective = value

    def __setattr__(self, __name: str, __value: Any) -> None:
        if __name not in Model._reserved_attributes and not isinstance(
            __value, (ModelElement, pl.DataFrame, pd.DataFrame)
        ):
            raise PyoframeError(
                f"Cannot set attribute '{__name}' on the model because it isn't of type ModelElement (e.g. Variable, Constraint, ...)"
            )

        if (
            isinstance(__value, ModelElement)
            and __name not in Model._reserved_attributes
        ):
            if isinstance(__value, ModelElementWithId):
                assert not hasattr(self, __name), (
                    f"Cannot create {__name} since it was already created."
                )

            __value.on_add_to_model(self, __name)

            if isinstance(__value, Variable):
                self._variables.append(__value)
                if self.var_map is not None:
                    self.var_map.add(__value)
            elif isinstance(__value, Constraint):
                self._constraints.append(__value)
        return super().__setattr__(__name, __value)

    def __repr__(self) -> str:
        return get_obj_repr(
            self,
            name=self.name,
            vars=len(self.variables),
            constrs=len(self.constraints),
            objective=bool(self.objective),
        )

    def write(self, file_path: Union[Path, str], pretty: bool = False):
        """
        Output the model to a file.

        Typical usage includes writing the solution to a `.sol` file as well as writing the problem to a `.lp` or `.mps` file.
        Set `use_var_names` in your model constructor to `True` if you'd like the output to contain human-readable names (useful for debugging).

        Parameters:
            file_path:
                The path to the file to write to.
            pretty:
                Only used when writing .sol files in HiGHS. If `True`, will use HiGH's pretty print columnar style which contains more information.
        """
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # If using IPOPT, raise an informative exception
        if self.solver_name == "ipopt":
            extension = file_path.suffix.lower()
            raise NotImplementedError(
                f"IPOPT does not support writing models to {extension} files. "
                "Use another solver like Gurobi or HiGHS if you need file export capabilities."
            )

        # Default handling for other solvers
        kwargs = {}
        if self.solver_name == "highs":
            if self.use_var_names:
                self.params.write_solution_style = 1
            kwargs["pretty"] = pretty
        self.poi.write(str(file_path), **kwargs)

    def optimize(self):
        """
        Optimize the model using your selected solver (e.g. Gurobi, HiGHS).
        """
        self.poi.optimize()

    @for_solvers("gurobi")
    def convert_to_fixed(self) -> None:
        """
        Turns a mixed integer program into a continuous one by fixing
        all the integer and binary variables to their solution values.

        !!! warning "Gurobi only"
            This method only works with the Gurobi solver. Open an issue if you'd like to see support for other solvers.

        Examples:
            >>> m = pf.Model(solver="gurobi")
            >>> m.X = pf.Variable(vtype=pf.VType.BINARY, lb=0)
            >>> m.Y = pf.Variable(vtype=pf.VType.INTEGER, lb=0)
            >>> m.Z = pf.Variable(lb=0)
            >>> m.my_constraint = m.X + m.Y + m.Z <= 10
            >>> m.maximize = 3 * m.X + 2 * m.Y + m.Z
            >>> m.optimize()
            >>> m.X.solution, m.Y.solution, m.Z.solution
            (1, 9, 0.0)
            >>> m.my_constraint.dual
            Traceback (most recent call last):
            ...
            polars.exceptions.ComputeError: RuntimeError: Unable to retrieve attribute 'Pi'
            >>> m.convert_to_fixed()
            >>> m.optimize()
            >>> m.my_constraint.dual
            1.0

            Only works for Gurobi:

            >>> m = pf.Model("max", solver="highs")
            >>> m.convert_to_fixed()
            Traceback (most recent call last):
            ...
            NotImplementedError: Method 'convert_to_fixed' is not implemented for solver 'highs'.
        """
        self.poi._converttofixed()

    @for_solvers("gurobi", "copt")
    def compute_IIS(self):
        """
        Computes the Irreducible Infeasible Set (IIS) of the model.

        !!! warning "Gurobi only"
            This method only works with the Gurobi solver. Open an issue if you'd like to see support for other solvers.

        Examples:
            >>> m = pf.Model(solver="gurobi")
            >>> m.X = pf.Variable(lb=0, ub=2)
            >>> m.Y = pf.Variable(lb=0, ub=2)
            >>> m.bad_constraint = m.X >= 3
            >>> m.minimize = m.X + m.Y
            >>> m.optimize()
            >>> m.attr.TerminationStatus
            <TerminationStatusCode.INFEASIBLE: 3>
            >>> m.bad_constraint.attr.IIS
            Traceback (most recent call last):
            ...
            polars.exceptions.ComputeError: RuntimeError: Unable to retrieve attribute 'IISConstr'
            >>> m.compute_IIS()
            >>> m.bad_constraint.attr.IIS
            True
        """
        self.poi.computeIIS()

    @for_solvers("gurobi")
    def dispose(self):
        """
        Tries to close the solver connection by deleting the model and forcing the garbage collector to run.

        Gurobi only. Once this method is called, this model is no longer usable.

        This method will not work if you have a variable that references self.poi.
        Unfortunately, this is a limitation from the underlying solver interface library.
        See https://github.com/metab0t/PyOptInterface/issues/36 for context.

        Examples:
            >>> m = pf.Model(solver="gurobi")
            >>> m.X = pf.Variable(ub=1)
            >>> m.maximize = m.X
            >>> m.optimize()
            >>> m.X.solution
            1.0
            >>> m.dispose()
            >>> m.X.solution
            Traceback (most recent call last):
            ...
            AttributeError: 'Model' object has no attribute 'poi'
        """
        import gc

        env = self.poi._env
        del self.poi
        gc.collect()
        del env
        gc.collect()

    def _set_param(self, name, value):
        self.poi.set_raw_parameter(name, value)

    def _get_param(self, name):
        return self.poi.get_raw_parameter(name)

    def _set_attr(self, name, value):
        try:
            self.poi.set_model_attribute(poi.ModelAttribute[name], value)
        except KeyError as e:
            if self.solver_name == "gurobi":
                self.poi.set_model_raw_attribute(name, value)
            else:
                raise e

    def _get_attr(self, name):
        try:
            return self.poi.get_model_attribute(poi.ModelAttribute[name])
        except KeyError as e:
            if self.solver_name == "gurobi":
                return self.poi.get_model_raw_attribute(name)
            else:
                raise e
