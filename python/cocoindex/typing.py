import collections
import dataclasses
import datetime
import inspect
import types
import typing
import uuid
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Generic,
    Literal,
    NamedTuple,
    Protocol,
    TypeVar,
    overload,
)

import numpy as np
from numpy.typing import NDArray


class VectorInfo(NamedTuple):
    dim: int | None


class TypeKind(NamedTuple):
    kind: str


class TypeAttr:
    key: str
    value: Any

    def __init__(self, key: str, value: Any):
        self.key = key
        self.value = value


Annotation = TypeKind | TypeAttr | VectorInfo

Int64 = Annotated[int, TypeKind("Int64")]
Float32 = Annotated[float, TypeKind("Float32")]
Float64 = Annotated[float, TypeKind("Float64")]
Range = Annotated[tuple[int, int], TypeKind("Range")]
Json = Annotated[Any, TypeKind("Json")]
LocalDateTime = Annotated[datetime.datetime, TypeKind("LocalDateTime")]
OffsetDateTime = Annotated[datetime.datetime, TypeKind("OffsetDateTime")]

if TYPE_CHECKING:
    T_co = TypeVar("T_co", covariant=True)
    Dim_co = TypeVar("Dim_co", bound=int | None, covariant=True, default=None)

    class Vector(Protocol, Generic[T_co, Dim_co]):
        """Vector[T, Dim] is a special typing alias for an NDArray[T] with optional dimension info"""

        def __getitem__(self, index: int) -> T_co: ...
        def __len__(self) -> int: ...

else:

    class Vector:  # type: ignore[unreachable]
        """A special typing alias for an NDArray[T] with optional dimension info"""

        def __class_getitem__(self, params):
            if not isinstance(params, tuple):
                # No dimension provided, e.g., Vector[np.float32]
                dtype = params
                vector_info = VectorInfo(dim=None)
            else:
                # Element type and dimension provided, e.g., Vector[np.float32, Literal[3]]
                dtype, dim_literal = params
                # Extract the literal value
                dim_val = (
                    typing.get_args(dim_literal)[0]
                    if typing.get_origin(dim_literal) is Literal
                    else None
                )
                vector_info = VectorInfo(dim=dim_val)

            # Use NDArray for supported numeric dtypes, else list
            base_type = analyze_type_info(dtype).base_type
            if is_numpy_number_type(base_type) or base_type is np.ndarray:
                return Annotated[NDArray[dtype], vector_info]
            return Annotated[list[dtype], vector_info]


TABLE_TYPES: tuple[str, str] = ("KTable", "LTable")
KEY_FIELD_NAME: str = "_key"


def extract_ndarray_elem_dtype(ndarray_type: Any) -> Any:
    args = typing.get_args(ndarray_type)
    _, dtype_spec = args
    dtype_args = typing.get_args(dtype_spec)
    if not dtype_args:
        raise ValueError(f"Invalid dtype specification: {dtype_spec}")
    return dtype_args[0]


def is_numpy_number_type(t: type) -> bool:
    return isinstance(t, type) and issubclass(t, (np.integer, np.floating))


def is_namedtuple_type(t: type) -> bool:
    return isinstance(t, type) and issubclass(t, tuple) and hasattr(t, "_fields")


def is_struct_type(t: Any) -> bool:
    return isinstance(t, type) and (
        dataclasses.is_dataclass(t) or is_namedtuple_type(t)
    )


class DtypeRegistry:
    """
    Registry for NumPy dtypes used in CocoIndex.
    Maps NumPy dtypes to their CocoIndex type kind.
    """

    _DTYPE_TO_KIND: dict[Any, str] = {
        np.float32: "Float32",
        np.float64: "Float64",
        np.int64: "Int64",
    }

    @classmethod
    def validate_dtype_and_get_kind(cls, dtype: Any) -> str:
        """
        Validate that the given dtype is supported, and get its CocoIndex kind by dtype.
        """
        if dtype is Any:
            raise TypeError(
                "NDArray for Vector must use a concrete numpy dtype, got `Any`."
            )
        kind = cls._DTYPE_TO_KIND.get(dtype)
        if kind is None:
            raise ValueError(
                f"Unsupported NumPy dtype in NDArray: {dtype}. "
                f"Supported dtypes: {cls._DTYPE_TO_KIND.keys()}"
            )
        return kind


class AnalyzedAnyType(NamedTuple):
    """
    When the type annotation is missing or matches any type.
    """


class AnalyzedBasicType(NamedTuple):
    """
    For types that fit into basic type, and annotated with basic type or Json type.
    """

    kind: str


class AnalyzedListType(NamedTuple):
    """
    Any list type, e.g. list[T], Sequence[T], NDArray[T], etc.
    """

    elem_type: Any
    vector_info: VectorInfo | None


class AnalyzedStructType(NamedTuple):
    """
    Any struct type, e.g. dataclass, NamedTuple, etc.
    """

    struct_type: type


class AnalyzedUnionType(NamedTuple):
    """
    Any union type, e.g. T1 | T2 | ..., etc.
    """

    variant_types: list[Any]


class AnalyzedDictType(NamedTuple):
    """
    Any dict type, e.g. dict[T1, T2], Mapping[T1, T2], etc.
    """

    key_type: Any
    value_type: Any


class AnalyzedUnknownType(NamedTuple):
    """
    Any type that is not supported by CocoIndex.
    """


AnalyzedTypeVariant = (
    AnalyzedAnyType
    | AnalyzedBasicType
    | AnalyzedListType
    | AnalyzedStructType
    | AnalyzedUnionType
    | AnalyzedDictType
    | AnalyzedUnknownType
)


@dataclasses.dataclass
class AnalyzedTypeInfo:
    """
    Analyzed info of a Python type.
    """

    # The type without annotations. e.g. int, list[int], dict[str, int]
    core_type: Any
    # The type without annotations and parameters. e.g. int, list, dict
    base_type: Any
    variant: AnalyzedTypeVariant
    attrs: dict[str, Any] | None
    nullable: bool = False


def analyze_type_info(t: Any) -> AnalyzedTypeInfo:
    """
    Analyze a Python type annotation and extract CocoIndex-specific type information.
    """

    annotations: tuple[Annotation, ...] = ()
    base_type = None
    type_args: tuple[Any, ...] = ()
    nullable = False
    while True:
        base_type = typing.get_origin(t)
        if base_type is Annotated:
            annotations = t.__metadata__
            t = t.__origin__
        else:
            if base_type is None:
                base_type = t
            else:
                type_args = typing.get_args(t)
            break
    core_type = t

    attrs: dict[str, Any] | None = None
    vector_info: VectorInfo | None = None
    kind: str | None = None
    for attr in annotations:
        if isinstance(attr, TypeAttr):
            if attrs is None:
                attrs = dict()
            attrs[attr.key] = attr.value
        elif isinstance(attr, VectorInfo):
            vector_info = attr
        elif isinstance(attr, TypeKind):
            kind = attr.kind

    variant: AnalyzedTypeVariant | None = None

    if kind is not None:
        variant = AnalyzedBasicType(kind=kind)
    elif base_type is Any or base_type is inspect.Parameter.empty:
        variant = AnalyzedAnyType()
    elif is_struct_type(base_type):
        variant = AnalyzedStructType(struct_type=t)
    elif is_numpy_number_type(t):
        kind = DtypeRegistry.validate_dtype_and_get_kind(t)
        variant = AnalyzedBasicType(kind=kind)
    elif base_type is collections.abc.Sequence or base_type is list:
        elem_type = type_args[0] if len(type_args) > 0 else Any
        variant = AnalyzedListType(elem_type=elem_type, vector_info=vector_info)
    elif base_type is np.ndarray:
        np_number_type = t
        elem_type = extract_ndarray_elem_dtype(np_number_type)
        variant = AnalyzedListType(elem_type=elem_type, vector_info=vector_info)
    elif base_type is collections.abc.Mapping or base_type is dict or t is dict:
        key_type = type_args[0] if len(type_args) > 0 else Any
        elem_type = type_args[1] if len(type_args) > 1 else Any
        variant = AnalyzedDictType(key_type=key_type, value_type=elem_type)
    elif base_type in (types.UnionType, typing.Union):
        non_none_types = [arg for arg in type_args if arg not in (None, types.NoneType)]
        if len(non_none_types) == 0:
            return analyze_type_info(None)

        nullable = len(non_none_types) < len(type_args)
        if len(non_none_types) == 1:
            result = analyze_type_info(non_none_types[0])
            result.nullable = nullable
            return result

        variant = AnalyzedUnionType(variant_types=non_none_types)
    else:
        if t is bytes:
            kind = "Bytes"
        elif t is str:
            kind = "Str"
        elif t is bool:
            kind = "Bool"
        elif t is int:
            kind = "Int64"
        elif t is float:
            kind = "Float64"
        elif t is uuid.UUID:
            kind = "Uuid"
        elif t is datetime.date:
            kind = "Date"
        elif t is datetime.time:
            kind = "Time"
        elif t is datetime.datetime:
            kind = "OffsetDateTime"
        elif t is datetime.timedelta:
            kind = "TimeDelta"

        if kind is None:
            variant = AnalyzedUnknownType()
        else:
            variant = AnalyzedBasicType(kind=kind)

    return AnalyzedTypeInfo(
        core_type=core_type,
        base_type=base_type,
        variant=variant,
        attrs=attrs,
        nullable=nullable,
    )


def _encode_struct_schema(
    struct_type: type, key_type: type | None = None
) -> dict[str, Any]:
    fields = []

    def add_field(name: str, t: Any) -> None:
        try:
            type_info = encode_enriched_type_info(analyze_type_info(t))
        except ValueError as e:
            e.add_note(
                f"Failed to encode annotation for field - "
                f"{struct_type.__name__}.{name}: {t}"
            )
            raise
        type_info["name"] = name
        fields.append(type_info)

    if key_type is not None:
        add_field(KEY_FIELD_NAME, key_type)

    if dataclasses.is_dataclass(struct_type):
        for field in dataclasses.fields(struct_type):
            add_field(field.name, field.type)
    elif is_namedtuple_type(struct_type):
        for name, field_type in struct_type.__annotations__.items():
            add_field(name, field_type)

    result: dict[str, Any] = {"fields": fields}
    if doc := inspect.getdoc(struct_type):
        result["description"] = doc
    return result


def _encode_type(type_info: AnalyzedTypeInfo) -> dict[str, Any]:
    variant = type_info.variant

    if isinstance(variant, AnalyzedAnyType):
        raise ValueError("Specific type annotation is expected")

    if isinstance(variant, AnalyzedUnknownType):
        raise ValueError(f"Unsupported type annotation: {type_info.core_type}")

    if isinstance(variant, AnalyzedBasicType):
        return {"kind": variant.kind}

    if isinstance(variant, AnalyzedStructType):
        encoded_type = _encode_struct_schema(variant.struct_type)
        encoded_type["kind"] = "Struct"
        return encoded_type

    if isinstance(variant, AnalyzedListType):
        elem_type_info = analyze_type_info(variant.elem_type)
        encoded_elem_type = _encode_type(elem_type_info)
        if isinstance(elem_type_info.variant, AnalyzedStructType):
            if variant.vector_info is not None:
                raise ValueError("LTable type must not have a vector info")
            return {
                "kind": "LTable",
                "row": _encode_struct_schema(elem_type_info.variant.struct_type),
            }
        else:
            vector_info = variant.vector_info
            return {
                "kind": "Vector",
                "element_type": encoded_elem_type,
                "dimension": vector_info and vector_info.dim,
            }

    if isinstance(variant, AnalyzedDictType):
        value_type_info = analyze_type_info(variant.value_type)
        if not isinstance(value_type_info.variant, AnalyzedStructType):
            raise ValueError(
                f"KTable value must have a Struct type, got {value_type_info.core_type}"
            )
        return {
            "kind": "KTable",
            "row": _encode_struct_schema(
                value_type_info.variant.struct_type,
                variant.key_type,
            ),
        }

    if isinstance(variant, AnalyzedUnionType):
        return {
            "kind": "Union",
            "types": [
                _encode_type(analyze_type_info(typ)) for typ in variant.variant_types
            ],
        }


def encode_enriched_type_info(enriched_type_info: AnalyzedTypeInfo) -> dict[str, Any]:
    """
    Encode an enriched type info to a CocoIndex engine's type representation
    """
    encoded: dict[str, Any] = {"type": _encode_type(enriched_type_info)}

    if enriched_type_info.attrs is not None:
        encoded["attrs"] = enriched_type_info.attrs

    if enriched_type_info.nullable:
        encoded["nullable"] = True

    return encoded


@overload
def encode_enriched_type(t: None) -> None: ...


@overload
def encode_enriched_type(t: Any) -> dict[str, Any]: ...


def encode_enriched_type(t: Any) -> dict[str, Any] | None:
    """
    Convert a Python type to a CocoIndex engine's type representation
    """
    if t is None:
        return None

    return encode_enriched_type_info(analyze_type_info(t))


def resolve_forward_ref(t: Any) -> Any:
    if isinstance(t, str):
        return eval(t)  # pylint: disable=eval-used
    return t
