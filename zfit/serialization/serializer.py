#  Copyright (c) 2022 zfit
from __future__ import annotations

import contextlib
import copy
import functools
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union, Mapping, Iterable, Dict, List, TypeVar, Optional

import pydantic
import tensorflow as tf
from frozendict import frozendict
from pydantic import Field
from typing_extensions import Literal, Annotated

from zfit.core.interfaces import ZfitParameter
from zfit.core.serialmixin import ZfitSerializable
from zfit.util.exception import WorkInProgressError
from zfit.util.warnings import warn_experimental_feature


@dataclass
class Aliases:
    hs3_type: str = "type"


alias1 = Aliases(hs3_type="type")


class Types:
    def __init__(self):
        self._pdf_repr = []
        self._param_repr = []
        self.block_forward_refs = True
        self.alias = alias1
        self.DUMMYTYPE = TypeVar("DUMMYTYPE")

    def one_or_many(self, repr):
        if self.block_forward_refs:
            raise NameError(
                "Internal error, should always be caught! If you see this, most likely the annotation"
                " evaluation was not postponed. To fix this, add a `from __future__ import annotations`"
                " and make sure to use Python 3.7+"
            )
        if len(repr) == 0:
            return None
        elif len(repr) == 1:
            return repr[0]
        else:
            return Union[
                Annotated[Union[tuple(repr)], Field(discriminator="hs3_type")],
                self.DUMMYTYPE,
            ]

    @property
    def PDFTypeDiscriminated(self):
        return self.one_or_many(self._pdf_repr)

    @property
    def ParamTypeDiscriminated(self):
        return self.one_or_many(self._param_repr)

    @property
    def ListParamTypeDiscriminated(self):
        return List[self.ParamTypeDiscriminated]

    @property
    def ParamInputTypeDiscriminated(self):
        return Union[self.ParamTypeDiscriminated, float, int]

    @property
    def ListParamInputTypeDiscriminated(self):
        return List[self.ParamInputTypeDiscriminated]

    def add_pdf_repr(self, repr):
        cls = repr._implementation
        from ..core.interfaces import ZfitPDF
        from ..core.interfaces import ZfitParameter

        if issubclass(cls, ZfitPDF):
            self._pdf_repr.append(repr)
        elif issubclass(cls, ZfitParameter):
            self._param_repr.append(repr)


class Serializer:
    types = Types()
    is_initialized = False

    constructor_repr = {}
    type_repr = {}
    _deserializing = False

    @classmethod
    def register(own_cls, repr):
        cls = repr._implementation
        if not issubclass(cls, ZfitSerializable):
            raise TypeError(
                f"{cls} is not a subclass of ZfitSerializable. Possible solution: inherit from "
                f"the SerializableMixin"
            )

        if cls not in own_cls.constructor_repr:
            own_cls.constructor_repr[cls] = repr
        else:
            raise ValueError(f"Class {cls} already registered")

        hs3_type = repr.__fields__["hs3_type"].default
        cls.hs3_type = hs3_type
        cls.__annotations__["hs3_type"] = Literal[hs3_type]
        if hs3_type not in own_cls.type_repr:
            own_cls.type_repr[hs3_type] = repr
        else:
            raise ValueError(f"Type {hs3_type} already registered")

        own_cls.types.add_pdf_repr(repr)

    @warn_experimental_feature
    @classmethod
    def to_hs3(cls, obj):
        cls.initialize()

        serial_kwargs = {"exclude_none": True, "by_alias": True}
        if not isinstance(obj, (list, tuple)):
            pdfs = [obj]
        from zfit.core.interfaces import ZfitPDF

        if not all(isinstance(ob, ZfitPDF) for ob in pdfs):
            raise WorkInProgressError("Only PDFs can be serialized currently")
        from zfit.core.serialmixin import ZfitSerializable

        if not all(isinstance(pdf, ZfitSerializable) for pdf in pdfs):
            raise TypeError("All pdfs must be ZfitSerializable")
        import zfit

        out = {
            "metadata": {
                "HS3": {"version": "experimental"},
                "serializer": {"lib": "zfit", "version": zfit.__version__},
            },
            "pdfs": {},
            "variables": {},
        }
        pdf_number = range(len(pdfs))
        for pdf in pdfs:
            name = pdf.name
            if name in out["pdfs"]:
                name = f"{name}_{pdf_number}"
            out["pdfs"][name] = pdf.get_repr().from_orm(pdf).dict(**serial_kwargs)

            for param in pdf.get_params(floating=None, extract_independent=None):
                if param.name not in out["variables"]:
                    paramdict = param.get_repr().from_orm(param).dict(**serial_kwargs)
                    del paramdict["type"]
                    out["variables"][param.name] = paramdict

            for ob in pdf.obs:
                if ob not in out["variables"]:
                    space = pdf.space.with_obs(ob)
                    spacedict = space.get_repr().from_orm(space).dict(**serial_kwargs)
                    del spacedict["type"]
                    out["variables"][ob] = spacedict

        out = cls.post_serialize(out)

        return out

    @classmethod
    def initialize(cls):
        if not cls.is_initialized:
            cls.types.block_forward_refs = False
            for repr in cls.constructor_repr.values():
                repr.update_forward_refs(
                    **{"Union": Union, "List": List, "Literal": Literal}
                )
            cls.is_initialized = True

    @warn_experimental_feature
    @classmethod
    def from_hs3(cls, load):
        cls.initialize()
        for param, paramdict in load["variables"].items():
            if "value" in paramdict:
                if paramdict.get("floating", True) is False:
                    paramdict["type"] = "ConstantParameter"
                else:
                    paramdict["type"] = "Parameter"
            elif "value_fn" in paramdict:
                paramdict["type"] = "ComposedParameter"
            else:
                paramdict["type"] = "Space"

        load = cls.pre_deserialize(load)

        out = {"pdfs": {}, "variables": {}}
        for name, pdf in load["pdfs"].items():
            repr = Serializer.type_repr[pdf["type"]]
            repr_inst = repr(**pdf)
            out["pdfs"][name] = repr_inst.to_orm()
        for name, param in load["variables"].items():
            repr = Serializer.type_repr[param["type"]]
            repr_inst = repr(**param)
            out["variables"][name] = repr_inst.to_orm()
        return out

    @classmethod
    @contextlib.contextmanager
    def deserializing(cls):
        cls._deserializing = True
        yield
        cls._deserializing = False

    @classmethod
    def post_serialize(cls, out):
        parameter = frozendict({"name": None, "min": None, "max": None})
        replace_forward = {parameter: lambda x: x["name"]}
        out["pdfs"] = replace_matching(out["pdfs"], replace_forward)

        const_params = frozendict({"name": None, "type": None, "floating": False})
        replace_forward = {const_params: lambda x: x["name"]}
        out["pdfs"] = replace_matching(out["pdfs"], replace_forward)
        return out

    @classmethod
    def pre_deserialize(cls, out):
        out = copy.deepcopy(out)
        replace_backward = {
            k: lambda x=k: out["variables"][x] for k in out["variables"].keys()
        }
        out["pdfs"] = replace_matching(out["pdfs"], replace_backward)
        return out


TYPENAME = "hs3_type"


def elements_match(mapping, replace):
    found = False
    for match, replacement in replace.items():
        if isinstance(mapping, Mapping) and isinstance(match, Mapping):
            for k, v in match.items():
                if k not in mapping:
                    break
                if v is None:
                    continue  # fine so far, a "free field"
                val = mapping.get(k)

                if val == v:
                    continue  # also fine
                break  # we're not fine, so let's stop here
            else:
                found = True
                break
        try:
            direct_hit = mapping == match
        except TypeError:
            continue
        else:
            found = direct_hit or found
        if found:
            break
    else:
        return False, None
    return True, replacement(mapping)


def replace_matching(mapping, replace):
    # we need to test in the very beginning, it could be that the structure is a match
    is_match, new_map = elements_match(mapping, replace)
    if is_match:
        return new_map

    mapping = copy.copy(mapping)
    if isinstance(mapping, Mapping):
        for k, v in mapping.items():
            mapping[k] = replace_matching(v, replace)
    elif (
        not isinstance(mapping, (str, ZfitParameter))
        and not tf.is_tensor(mapping)
        and isinstance(mapping, Iterable)
    ):
        replaced_list = [replace_matching(v, replace) for v in mapping]
        mapping = type(mapping)(replaced_list)
    return mapping


def convert_to_orm(init):
    if isinstance(init, Mapping):
        for k, v in init.items():

            from zfit.core.interfaces import ZfitParameter, ZfitSpace

            if (
                not isinstance(v, (Iterable, Mapping))
                or isinstance(v, (ZfitParameter, ZfitSpace))
                or tf.is_tensor(v)
            ):
                continue
            elif TYPENAME in v:
                type_ = v[TYPENAME]
                init[k] = Serializer.type_repr[type_](**v).to_orm()
            else:
                init[k] = convert_to_orm(v)

        if TYPENAME in init:  # dicts can be the raw data we want.
            cls = Serializer.type_repr[init[TYPENAME]]
            return cls(**init).to_orm()

    elif isinstance(init, (list, tuple)):
        init = type(init)([convert_to_orm(v) for v in init])
    return init


def to_orm_init(func):
    @functools.wraps(func)
    def wrapper(self, init, **kwargs):
        init = convert_to_orm(init)
        return func(self, init, **kwargs)

    return wrapper


class MODES(Enum):
    orm = "orm"
    repr = "repr"


class BaseRepr(pydantic.BaseModel):
    _implementation = pydantic.PrivateAttr()
    _context = pydantic.PrivateAttr(None)
    _constructor = pydantic.PrivateAttr(None)

    dictionary: Optional[Dict] = Field(alias="dict")
    tags: Optional[List[str]] = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if (
            cls._implementation is not None
        ):  # TODO: better way to catch if constructor is set vs BaseClass?
            Serializer.register(cls)

    class Config:
        orm_mode = True
        arbitrary_types_allowed = True
        allow_population_by_field_name = True
        smart_union = True

    @classmethod
    def orm_mode(cls, v):
        if cls._context is None:
            raise ValueError("No context set!")
        return cls._context == MODES.orm

    @classmethod
    def from_orm(cls: pydantic.BaseModel, obj: Any) -> BaseRepr:

        old_mode = cls._context
        try:
            cls._context = MODES.orm
            out = super().from_orm(obj)
        finally:
            cls._context = old_mode
        return out

    def to_orm(self):
        old_mode = type(self)._context
        try:
            type(self)._context = MODES.repr
            if self._implementation is None:
                raise ValueError("No implementation registered!")
            init = self.dict(exclude_none=True)
            type_ = init.pop("hs3_type")
            # assert type_ == self.hs3_type
            out = self._to_orm(init)
        finally:
            type(self)._context = old_mode
        return out

    @to_orm_init
    def _to_orm(self, init):
        constructor = self._constructor
        if constructor is None:
            constructor = self._implementation
        return constructor(**init)
