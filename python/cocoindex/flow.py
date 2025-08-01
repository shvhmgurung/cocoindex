"""
Flow is the main interface for building and running flows.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import inspect
import re

from .validation import (
    validate_flow_name,
    NamingError,
    validate_full_flow_name,
    validate_target_name,
)
from .typing import analyze_type_info

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import (
    Any,
    Callable,
    Generic,
    NamedTuple,
    Sequence,
    TypeVar,
    cast,
    get_args,
    get_origin,
    Iterable,
)

from rich.text import Text
from rich.tree import Tree

from . import _engine  # type: ignore
from . import index
from . import op
from . import setting
from .convert import dump_engine_object, encode_engine_value, make_engine_value_decoder
from .op import FunctionSpec
from .runtime import execution_context
from .setup import SetupChangeBundle
from .typing import encode_enriched_type


class _NameBuilder:
    _existing_names: set[str]
    _next_name_index: dict[str, int]

    def __init__(self) -> None:
        self._existing_names = set()
        self._next_name_index = {}

    def build_name(self, name: str | None, /, prefix: str) -> str:
        """
        Build a name. If the name is None, generate a name with the given prefix.
        """
        if name is not None:
            self._existing_names.add(name)
            return name

        next_idx = self._next_name_index.get(prefix, 0)
        while True:
            name = f"{prefix}{next_idx}"
            next_idx += 1
            self._next_name_index[prefix] = next_idx
            if name not in self._existing_names:
                self._existing_names.add(name)
                return name


_WORD_BOUNDARY_RE = re.compile("(?<!^)(?=[A-Z])")


def _to_snake_case(name: str) -> str:
    return _WORD_BOUNDARY_RE.sub("_", name).lower()


def _create_data_slice(
    flow_builder_state: _FlowBuilderState,
    creator: Callable[[_engine.DataScopeRef | None, str | None], _engine.DataSlice],
    name: str | None = None,
) -> DataSlice[T]:
    if name is None:
        return DataSlice(
            _DataSliceState(
                flow_builder_state,
                lambda target: creator(target[0], target[1])
                if target is not None
                else creator(None, None),
            )
        )
    else:
        return DataSlice(_DataSliceState(flow_builder_state, creator(None, name)))


def _spec_kind(spec: Any) -> str:
    return cast(str, spec.__class__.__name__)


def _transform_helper(
    flow_builder_state: _FlowBuilderState,
    fn_spec: FunctionSpec,
    transform_args: list[tuple[Any, str | None]],
    name: str | None = None,
) -> DataSlice[Any]:
    if not isinstance(fn_spec, FunctionSpec):
        raise ValueError("transform() can only be called on a CocoIndex function")

    return _create_data_slice(
        flow_builder_state,
        lambda target_scope, name: flow_builder_state.engine_flow_builder.transform(
            _spec_kind(fn_spec),
            dump_engine_object(fn_spec),
            transform_args,
            target_scope,
            flow_builder_state.field_name_builder.build_name(
                name, prefix=_to_snake_case(_spec_kind(fn_spec)) + "_"
            ),
        ),
        name,
    )


T = TypeVar("T")
S = TypeVar("S")


class _DataSliceState:
    flow_builder_state: _FlowBuilderState

    _lazy_lock: Lock | None = None  # None means it's not lazy.
    _data_slice: _engine.DataSlice | None = None
    _data_slice_creator: (
        Callable[[tuple[_engine.DataScopeRef, str] | None], _engine.DataSlice] | None
    ) = None

    def __init__(
        self,
        flow_builder_state: _FlowBuilderState,
        data_slice: _engine.DataSlice
        | Callable[[tuple[_engine.DataScopeRef, str] | None], _engine.DataSlice],
    ):
        self.flow_builder_state = flow_builder_state

        if isinstance(data_slice, _engine.DataSlice):
            self._data_slice = data_slice
        else:
            self._lazy_lock = Lock()
            self._data_slice_creator = data_slice

    @property
    def engine_data_slice(self) -> _engine.DataSlice:
        """
        Get the internal DataSlice.
        """
        if self._lazy_lock is None:
            if self._data_slice is None:
                raise ValueError("Data slice is not initialized")
            return self._data_slice
        else:
            if self._data_slice_creator is None:
                raise ValueError("Data slice creator is not initialized")
            with self._lazy_lock:
                if self._data_slice is None:
                    self._data_slice = self._data_slice_creator(None)
                return self._data_slice

    def attach_to_scope(self, scope: _engine.DataScopeRef, field_name: str) -> None:
        """
        Attach the current data slice (if not yet attached) to the given scope.
        """
        if self._lazy_lock is not None:
            with self._lazy_lock:
                if self._data_slice_creator is None:
                    raise ValueError("Data slice creator is not initialized")
                if self._data_slice is None:
                    self._data_slice = self._data_slice_creator((scope, field_name))
                    return
        # TODO: We'll support this by an identity transformer or "aliasing" in the future.
        raise ValueError("DataSlice is already attached to a field")


class DataSlice(Generic[T]):
    """A data slice represents a slice of data in a flow. It's readonly."""

    _state: _DataSliceState

    def __init__(self, state: _DataSliceState):
        self._state = state

    def __str__(self) -> str:
        return str(self._state.engine_data_slice)

    def __repr__(self) -> str:
        return repr(self._state.engine_data_slice)

    def __getitem__(self, field_name: str) -> DataSlice[T]:
        field_slice = self._state.engine_data_slice.field(field_name)
        if field_slice is None:
            raise KeyError(field_name)
        return DataSlice(_DataSliceState(self._state.flow_builder_state, field_slice))

    def row(
        self,
        /,
        *,
        max_inflight_rows: int | None = None,
        max_inflight_bytes: int | None = None,
    ) -> DataScope:
        """
        Return a scope representing each row of the table.
        """
        row_scope = self._state.flow_builder_state.engine_flow_builder.for_each(
            self._state.engine_data_slice,
            execution_options=dump_engine_object(
                _ExecutionOptions(
                    max_inflight_rows=max_inflight_rows,
                    max_inflight_bytes=max_inflight_bytes,
                ),
            ),
        )
        return DataScope(self._state.flow_builder_state, row_scope)

    def for_each(
        self,
        f: Callable[[DataScope], None],
        /,
        *,
        max_inflight_rows: int | None = None,
        max_inflight_bytes: int | None = None,
    ) -> None:
        """
        Apply a function to each row of the collection.
        """
        with self.row(
            max_inflight_rows=max_inflight_rows,
            max_inflight_bytes=max_inflight_bytes,
        ) as scope:
            f(scope)

    def transform(
        self, fn_spec: op.FunctionSpec, *args: Any, **kwargs: Any
    ) -> DataSlice[Any]:
        """
        Apply a function to the data slice.
        """
        transform_args: list[tuple[Any, str | None]] = [
            (self._state.engine_data_slice, None)
        ]
        transform_args += [
            (self._state.flow_builder_state.get_data_slice(v), None) for v in args
        ]
        transform_args += [
            (self._state.flow_builder_state.get_data_slice(v), k)
            for k, v in kwargs.items()
        ]

        return _transform_helper(
            self._state.flow_builder_state, fn_spec, transform_args
        )

    def call(self, func: Callable[..., S], *args: Any, **kwargs: Any) -> S:
        """
        Call a function with the data slice.
        """
        return func(self, *args, **kwargs)


def _data_slice_state(data_slice: DataSlice[T]) -> _DataSliceState:
    return data_slice._state  # pylint: disable=protected-access


class DataScope:
    """
    A data scope in a flow.
    It has multple fields and collectors, and allow users to add new fields and collectors.
    """

    _flow_builder_state: _FlowBuilderState
    _engine_data_scope: _engine.DataScopeRef

    def __init__(
        self, flow_builder_state: _FlowBuilderState, data_scope: _engine.DataScopeRef
    ):
        self._flow_builder_state = flow_builder_state
        self._engine_data_scope = data_scope

    def __str__(self) -> str:
        return str(self._engine_data_scope)

    def __repr__(self) -> str:
        return repr(self._engine_data_scope)

    def __getitem__(self, field_name: str) -> DataSlice[T]:
        return DataSlice(
            _DataSliceState(
                self._flow_builder_state,
                self._flow_builder_state.engine_flow_builder.scope_field(
                    self._engine_data_scope, field_name
                ),
            )
        )

    def __setitem__(self, field_name: str, value: DataSlice[T]) -> None:
        from .validation import validate_field_name

        validate_field_name(field_name)
        value._state.attach_to_scope(self._engine_data_scope, field_name)

    def __enter__(self) -> DataScope:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        del self._engine_data_scope

    def add_collector(self, name: str | None = None) -> DataCollector:
        """
        Add a collector to the flow.
        """
        return DataCollector(
            self._flow_builder_state,
            self._engine_data_scope.add_collector(
                self._flow_builder_state.field_name_builder.build_name(
                    name, prefix="_collector_"
                )
            ),
        )


class GeneratedField(Enum):
    """
    A generated field is automatically set by the engine.
    """

    UUID = "Uuid"


class DataCollector:
    """A data collector is used to collect data into a collector."""

    _flow_builder_state: _FlowBuilderState
    _engine_data_collector: _engine.DataCollector

    def __init__(
        self,
        flow_builder_state: _FlowBuilderState,
        data_collector: _engine.DataCollector,
    ):
        self._flow_builder_state = flow_builder_state
        self._engine_data_collector = data_collector

    def collect(self, **kwargs: Any) -> None:
        """
        Collect data into the collector.
        """
        regular_kwargs = []
        auto_uuid_field = None
        for k, v in kwargs.items():
            if isinstance(v, GeneratedField):
                if v == GeneratedField.UUID:
                    if auto_uuid_field is not None:
                        raise ValueError("Only one generated UUID field is allowed")
                    auto_uuid_field = k
                else:
                    raise ValueError(f"Unexpected generated field: {v}")
            else:
                regular_kwargs.append((k, self._flow_builder_state.get_data_slice(v)))

        self._flow_builder_state.engine_flow_builder.collect(
            self._engine_data_collector, regular_kwargs, auto_uuid_field
        )

    def export(
        self,
        target_name: str,
        target_spec: op.TargetSpec,
        /,
        *,
        primary_key_fields: Sequence[str],
        vector_indexes: Sequence[index.VectorIndexDef] = (),
        vector_index: Sequence[tuple[str, index.VectorSimilarityMetric]] = (),
        setup_by_user: bool = False,
    ) -> None:
        """
        Export the collected data to the specified target.

        `vector_index` is for backward compatibility only. Please use `vector_indexes` instead.
        """

        validate_target_name(target_name)
        if not isinstance(target_spec, op.TargetSpec):
            raise ValueError(
                "export() can only be called on a CocoIndex target storage"
            )

        # For backward compatibility only.
        if len(vector_indexes) == 0 and len(vector_index) > 0:
            vector_indexes = [
                index.VectorIndexDef(field_name=field_name, metric=metric)
                for field_name, metric in vector_index
            ]

        index_options = index.IndexOptions(
            primary_key_fields=primary_key_fields,
            vector_indexes=vector_indexes,
        )
        self._flow_builder_state.engine_flow_builder.export(
            target_name,
            _spec_kind(target_spec),
            dump_engine_object(target_spec),
            dump_engine_object(index_options),
            self._engine_data_collector,
            setup_by_user,
        )


_flow_name_builder = _NameBuilder()


class _FlowBuilderState:
    """
    A flow builder is used to build a flow.
    """

    engine_flow_builder: _engine.FlowBuilder
    field_name_builder: _NameBuilder

    def __init__(self, full_name: str):
        self.engine_flow_builder = _engine.FlowBuilder(full_name)
        self.field_name_builder = _NameBuilder()

    def get_data_slice(self, v: Any) -> _engine.DataSlice:
        """
        Return a data slice that represents the given value.
        """
        if isinstance(v, DataSlice):
            return v._state.engine_data_slice
        return self.engine_flow_builder.constant(encode_enriched_type(type(v)), v)


@dataclass
class _SourceRefreshOptions:
    """
    Options for refreshing a source.
    """

    refresh_interval: datetime.timedelta | None = None


@dataclass
class _ExecutionOptions:
    max_inflight_rows: int | None = None
    max_inflight_bytes: int | None = None


class FlowBuilder:
    """
    A flow builder is used to build a flow.
    """

    _state: _FlowBuilderState

    def __init__(self, state: _FlowBuilderState):
        self._state = state

    def __str__(self) -> str:
        return str(self._state.engine_flow_builder)

    def __repr__(self) -> str:
        return repr(self._state.engine_flow_builder)

    def add_source(
        self,
        spec: op.SourceSpec,
        /,
        *,
        name: str | None = None,
        refresh_interval: datetime.timedelta | None = None,
        max_inflight_rows: int | None = None,
        max_inflight_bytes: int | None = None,
    ) -> DataSlice[T]:
        """
        Import a source to the flow.
        """
        if not isinstance(spec, op.SourceSpec):
            raise ValueError("add_source() can only be called on a CocoIndex source")
        return _create_data_slice(
            self._state,
            lambda target_scope, name: self._state.engine_flow_builder.add_source(
                _spec_kind(spec),
                dump_engine_object(spec),
                target_scope,
                self._state.field_name_builder.build_name(
                    name, prefix=_to_snake_case(_spec_kind(spec)) + "_"
                ),
                refresh_options=dump_engine_object(
                    _SourceRefreshOptions(refresh_interval=refresh_interval)
                ),
                execution_options=dump_engine_object(
                    _ExecutionOptions(
                        max_inflight_rows=max_inflight_rows,
                        max_inflight_bytes=max_inflight_bytes,
                    )
                ),
            ),
            name,
        )

    def transform(
        self, fn_spec: FunctionSpec, *args: Any, **kwargs: Any
    ) -> DataSlice[Any]:
        """
        Apply a function to inputs, returning a DataSlice.
        """
        transform_args: list[tuple[Any, str | None]] = [
            (self._state.get_data_slice(v), None) for v in args
        ]
        transform_args += [
            (self._state.get_data_slice(v), k) for k, v in kwargs.items()
        ]

        if not transform_args:
            raise ValueError("At least one input is required for transformation")

        return _transform_helper(self._state, fn_spec, transform_args)

    def declare(self, spec: op.DeclarationSpec) -> None:
        """
        Add a declaration to the flow.
        """
        self._state.engine_flow_builder.declare(dump_engine_object(spec))


@dataclass
class FlowLiveUpdaterOptions:
    """
    Options for live updating a flow.
    """

    live_mode: bool = True
    print_stats: bool = False


class FlowUpdaterStatusUpdates(NamedTuple):
    """
    Status updates for a flow updater.
    """

    # Sources that are still active, i.e. not stopped processing.
    active_sources: list[str]

    # Sources with updates since last time.
    updated_sources: list[str]


class FlowLiveUpdater:
    """
    A live updater for a flow.
    """

    _flow: Flow
    _options: FlowLiveUpdaterOptions
    _engine_live_updater: _engine.FlowLiveUpdater | None = None

    def __init__(self, fl: Flow, options: FlowLiveUpdaterOptions | None = None):
        self._flow = fl
        self._options = options or FlowLiveUpdaterOptions()

    def __enter__(self) -> FlowLiveUpdater:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.abort()
        self.wait()

    async def __aenter__(self) -> FlowLiveUpdater:
        await self.start_async()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.abort()
        await self.wait_async()

    def start(self) -> None:
        """
        Start the live updater.
        """
        execution_context.run(self.start_async())

    async def start_async(self) -> None:
        """
        Start the live updater.
        """
        self._engine_live_updater = await _engine.FlowLiveUpdater.create(
            await self._flow.internal_flow_async(), dump_engine_object(self._options)
        )

    def wait(self) -> None:
        """
        Wait for the live updater to finish.
        """
        execution_context.run(self.wait_async())

    async def wait_async(self) -> None:
        """
        Wait for the live updater to finish. Async version.
        """
        await self._get_engine_live_updater().wait_async()

    def next_status_updates(self) -> FlowUpdaterStatusUpdates:
        """
        Get the next status updates.

        It blocks until there's a new status updates, including the processing finishes for a bunch of source updates,
        and live updater stops (aborted, or no more sources to process).
        """
        return execution_context.run(self.next_status_updates_async())

    async def next_status_updates_async(self) -> FlowUpdaterStatusUpdates:
        """
        Get the next status updates. Async version.
        """
        updates = await self._get_engine_live_updater().next_status_updates_async()
        return FlowUpdaterStatusUpdates(
            active_sources=updates.active_sources,
            updated_sources=updates.updated_sources,
        )

    def abort(self) -> None:
        """
        Abort the live updater.
        """
        self._get_engine_live_updater().abort()

    def update_stats(self) -> _engine.IndexUpdateInfo:
        """
        Get the index update info.
        """
        return self._get_engine_live_updater().index_update_info()

    def _get_engine_live_updater(self) -> _engine.FlowLiveUpdater:
        if self._engine_live_updater is None:
            raise RuntimeError("Live updater is not started")
        return self._engine_live_updater


@dataclass
class EvaluateAndDumpOptions:
    """
    Options for evaluating and dumping a flow.
    """

    output_dir: str
    use_cache: bool = True


class Flow:
    """
    A flow describes an indexing pipeline.
    """

    _name: str
    _full_name: str
    _lazy_engine_flow: Callable[[], _engine.Flow] | None

    def __init__(
        self, name: str, full_name: str, engine_flow_creator: Callable[[], _engine.Flow]
    ):
        validate_flow_name(name)
        validate_full_flow_name(full_name)
        self._name = name
        self._full_name = full_name
        engine_flow = None
        lock = Lock()

        def _lazy_engine_flow() -> _engine.Flow:
            nonlocal engine_flow, lock
            if engine_flow is None:
                with lock:
                    if engine_flow is None:
                        engine_flow = engine_flow_creator()
            return engine_flow

        self._lazy_engine_flow = _lazy_engine_flow

    def _render_spec(self, verbose: bool = False) -> Tree:
        """
        Render the flow spec as a styled rich Tree with hierarchical structure.
        """
        spec = self._get_spec(verbose=verbose)
        tree = Tree(f"Flow: {self.full_name}", style="cyan")

        def build_tree(label: str, lines: list[Any]) -> Tree:
            node = Tree(label=label if lines else label + " None", style="cyan")
            for line in lines:
                child_node = node.add(Text(line.content, style="yellow"))
                child_node.children = build_tree("", line.children).children
            return node

        for section, lines in spec.sections:
            section_node = build_tree(f"{section}:", lines)
            tree.children.append(section_node)
        return tree

    def _get_spec(self, verbose: bool = False) -> _engine.RenderedSpec:
        return self.internal_flow().get_spec(
            output_mode="verbose" if verbose else "concise"
        )

    def _get_schema(self) -> list[tuple[str, str, str]]:
        return cast(list[tuple[str, str, str]], self.internal_flow().get_schema())

    def __str__(self) -> str:
        return str(self._get_spec())

    def __repr__(self) -> str:
        return repr(self.internal_flow())

    @property
    def name(self) -> str:
        """
        Get the name of the flow.
        """
        return self._name

    @property
    def full_name(self) -> str:
        """
        Get the full name of the flow.
        """
        return self._full_name

    def update(self) -> _engine.IndexUpdateInfo:
        """
        Update the index defined by the flow.
        Once the function returns, the index is fresh up to the moment when the function is called.
        """
        return execution_context.run(self.update_async())

    async def update_async(self) -> _engine.IndexUpdateInfo:
        """
        Update the index defined by the flow.
        Once the function returns, the index is fresh up to the moment when the function is called.
        """
        async with FlowLiveUpdater(
            self, FlowLiveUpdaterOptions(live_mode=False)
        ) as updater:
            await updater.wait_async()
        return updater.update_stats()

    def evaluate_and_dump(
        self, options: EvaluateAndDumpOptions
    ) -> _engine.IndexUpdateInfo:
        """
        Evaluate the flow and dump flow outputs to files.
        """
        return self.internal_flow().evaluate_and_dump(dump_engine_object(options))

    def internal_flow(self) -> _engine.Flow:
        """
        Get the engine flow.
        """
        if self._lazy_engine_flow is None:
            raise RuntimeError(f"Flow {self.full_name} is already removed")
        return self._lazy_engine_flow()

    async def internal_flow_async(self) -> _engine.Flow:
        """
        Get the engine flow. The async version.
        """
        return await asyncio.to_thread(self.internal_flow)

    def setup(self, report_to_stdout: bool = False) -> None:
        """
        Setup persistent backends of the flow.
        """
        execution_context.run(self.setup_async(report_to_stdout=report_to_stdout))

    async def setup_async(self, report_to_stdout: bool = False) -> None:
        """
        Setup persistent backends of the flow. The async version.
        """
        await make_setup_bundle([self]).describe_and_apply_async(
            report_to_stdout=report_to_stdout
        )

    def drop(self, report_to_stdout: bool = False) -> None:
        """
        Drop persistent backends of the flow.

        The current instance is still valid after it's called.
        For example, you can still call `setup()` after it, to setup the persistent backends again.

        Call `close()` if you want to remove the flow from the current process.
        """
        execution_context.run(self.drop_async(report_to_stdout=report_to_stdout))

    async def drop_async(self, report_to_stdout: bool = False) -> None:
        """
        Drop persistent backends of the flow. The async version.
        """
        await make_drop_bundle([self]).describe_and_apply_async(
            report_to_stdout=report_to_stdout
        )

    def close(self) -> None:
        """
        Close the flow. It will remove the flow from the current process to free up resources.
        After it's called, methods of the flow should no longer be called.

        This will NOT touch the persistent backends of the flow.
        """
        _engine.remove_flow_context(self.full_name)
        self._lazy_engine_flow = None
        with _flows_lock:
            del _flows[self.name]


def _create_lazy_flow(
    name: str | None, fl_def: Callable[[FlowBuilder, DataScope], None]
) -> Flow:
    """
    Create a flow without really building it yet.
    The flow will be built the first time when it's really needed.
    """
    flow_name = _flow_name_builder.build_name(name, prefix="_flow_")
    flow_full_name = get_flow_full_name(flow_name)

    def _create_engine_flow() -> _engine.Flow:
        flow_builder_state = _FlowBuilderState(flow_full_name)
        root_scope = DataScope(
            flow_builder_state, flow_builder_state.engine_flow_builder.root_scope()
        )
        fl_def(FlowBuilder(flow_builder_state), root_scope)
        return flow_builder_state.engine_flow_builder.build_flow(
            execution_context.event_loop
        )

    return Flow(flow_name, flow_full_name, _create_engine_flow)


_flows_lock = Lock()
_flows: dict[str, Flow] = {}


def get_flow_full_name(name: str) -> str:
    """
    Get the full name of a flow.
    """
    return f"{setting.get_app_namespace(trailing_delimiter='.')}{name}"


def open_flow(name: str, fl_def: Callable[[FlowBuilder, DataScope], None]) -> Flow:
    """
    Open a flow, with the given name and definition.
    """
    with _flows_lock:
        if name in _flows:
            raise KeyError(f"Flow with name {name} already exists")
        fl = _flows[name] = _create_lazy_flow(name, fl_def)
    return fl


def add_flow_def(name: str, fl_def: Callable[[FlowBuilder, DataScope], None]) -> Flow:
    """
    DEPRECATED: Use `open_flow()` instead.
    """
    return open_flow(name, fl_def)


def remove_flow(fl: Flow) -> None:
    """
    DEPRECATED: Use `Flow.close()` instead.
    """
    fl.close()


def flow_def(
    name: str | None = None,
) -> Callable[[Callable[[FlowBuilder, DataScope], None]], Flow]:
    """
    A decorator to wrap the flow definition.
    """
    return lambda fl_def: open_flow(name or fl_def.__name__, fl_def)


def flow_names() -> list[str]:
    """
    Get the names of all flows.
    """
    with _flows_lock:
        return list(_flows.keys())


def flows() -> dict[str, Flow]:
    """
    Get all flows.
    """
    with _flows_lock:
        return dict(_flows)


def flow_by_name(name: str) -> Flow:
    """
    Get a flow by name.
    """
    with _flows_lock:
        return _flows[name]


def ensure_all_flows_built() -> None:
    """
    Ensure all flows are built.
    """
    execution_context.run(ensure_all_flows_built_async())


async def ensure_all_flows_built_async() -> None:
    """
    Ensure all flows are built.
    """
    for fl in flows().values():
        await fl.internal_flow_async()


def update_all_flows(
    options: FlowLiveUpdaterOptions,
) -> dict[str, _engine.IndexUpdateInfo]:
    """
    Update all flows.
    """
    return execution_context.run(update_all_flows_async(options))


async def update_all_flows_async(
    options: FlowLiveUpdaterOptions,
) -> dict[str, _engine.IndexUpdateInfo]:
    """
    Update all flows.
    """
    await ensure_all_flows_built_async()

    async def _update_flow(name: str, fl: Flow) -> tuple[str, _engine.IndexUpdateInfo]:
        async with FlowLiveUpdater(fl, options) as updater:
            await updater.wait_async()
            return (name, updater.update_stats())

    fls = flows()
    all_stats = await asyncio.gather(
        *(_update_flow(name, fl) for (name, fl) in fls.items())
    )
    return dict(all_stats)


def _get_data_slice_annotation_type(
    data_slice_type: type[DataSlice[T] | inspect._empty],
) -> type[T] | None:
    type_args = get_args(data_slice_type)
    if data_slice_type is inspect.Parameter.empty or data_slice_type is DataSlice:
        return None
    if get_origin(data_slice_type) != DataSlice or len(type_args) != 1:
        raise ValueError(f"Expect a DataSlice[T] type, but got {data_slice_type}")
    return cast(type[T] | None, type_args[0])


_transform_flow_name_builder = _NameBuilder()


class TransformFlowInfo(NamedTuple):
    engine_flow: _engine.TransientFlow
    result_decoder: Callable[[Any], T]


class TransformFlow(Generic[T]):
    """
    A transient transformation flow that transforms in-memory data.
    """

    _flow_fn: Callable[..., DataSlice[T]]
    _flow_name: str
    _flow_arg_types: list[Any]
    _param_names: list[str]

    _lazy_lock: asyncio.Lock
    _lazy_flow_info: TransformFlowInfo | None = None

    def __init__(
        self,
        flow_fn: Callable[..., DataSlice[T]],
        flow_arg_types: Sequence[Any],
        /,
        name: str | None = None,
    ):
        self._flow_fn = flow_fn
        self._flow_name = _transform_flow_name_builder.build_name(
            name, prefix="_transform_flow_"
        )
        self._flow_arg_types = list(flow_arg_types)
        self._lazy_lock = asyncio.Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> DataSlice[T]:
        return self._flow_fn(*args, **kwargs)

    @property
    def _flow_info(self) -> TransformFlowInfo:
        if self._lazy_flow_info is not None:
            return self._lazy_flow_info
        return execution_context.run(self._flow_info_async())

    async def _flow_info_async(self) -> TransformFlowInfo:
        if self._lazy_flow_info is not None:
            return self._lazy_flow_info
        async with self._lazy_lock:
            if self._lazy_flow_info is None:
                self._lazy_flow_info = await self._build_flow_info_async()
            return self._lazy_flow_info

    async def _build_flow_info_async(self) -> TransformFlowInfo:
        flow_builder_state = _FlowBuilderState(self._flow_name)
        sig = inspect.signature(self._flow_fn)
        if len(sig.parameters) != len(self._flow_arg_types):
            raise ValueError(
                f"Number of parameters in the flow function ({len(sig.parameters)}) "
                f"does not match the number of argument types ({len(self._flow_arg_types)})"
            )

        kwargs: dict[str, DataSlice[T]] = {}
        for (param_name, param), param_type in zip(
            sig.parameters.items(), self._flow_arg_types
        ):
            if param.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise ValueError(
                    f"Parameter `{param_name}` is not a parameter can be passed by name"
                )
            encoded_type = encode_enriched_type(param_type)
            if encoded_type is None:
                raise ValueError(f"Parameter `{param_name}` has no type annotation")
            engine_ds = flow_builder_state.engine_flow_builder.add_direct_input(
                param_name, encoded_type
            )
            kwargs[param_name] = DataSlice(
                _DataSliceState(flow_builder_state, engine_ds)
            )

        output = self._flow_fn(**kwargs)
        flow_builder_state.engine_flow_builder.set_direct_output(
            _data_slice_state(output).engine_data_slice
        )
        engine_flow = (
            await flow_builder_state.engine_flow_builder.build_transient_flow_async(
                execution_context.event_loop
            )
        )
        self._param_names = list(sig.parameters.keys())

        engine_return_type = (
            _data_slice_state(output).engine_data_slice.data_type().schema()
        )
        python_return_type: type[T] | None = _get_data_slice_annotation_type(
            sig.return_annotation
        )
        result_decoder = make_engine_value_decoder(
            [], engine_return_type["type"], analyze_type_info(python_return_type)
        )

        return TransformFlowInfo(engine_flow, result_decoder)

    def __str__(self) -> str:
        return str(self._flow_info.engine_flow)

    def __repr__(self) -> str:
        return repr(self._flow_info.engine_flow)

    def internal_flow(self) -> _engine.TransientFlow:
        """
        Get the internal flow.
        """
        return self._flow_info.engine_flow

    def eval(self, *args: Any, **kwargs: Any) -> T:
        """
        Evaluate the transform flow.
        """
        return execution_context.run(self.eval_async(*args, **kwargs))

    async def eval_async(self, *args: Any, **kwargs: Any) -> T:
        """
        Evaluate the transform flow.
        """
        flow_info = await self._flow_info_async()
        params = []
        for i, arg in enumerate(self._param_names):
            if i < len(args):
                params.append(encode_engine_value(args[i]))
            elif arg in kwargs:
                params.append(encode_engine_value(kwargs[arg]))
            else:
                raise ValueError(f"Parameter {arg} is not provided")
        engine_result = await flow_info.engine_flow.evaluate_async(params)
        return flow_info.result_decoder(engine_result)


def transform_flow() -> Callable[[Callable[..., DataSlice[T]]], TransformFlow[T]]:
    """
    A decorator to wrap the transform function.
    """

    def _transform_flow_wrapper(fn: Callable[..., DataSlice[T]]) -> TransformFlow[T]:
        sig = inspect.signature(fn)
        arg_types = []
        for param_name, param in sig.parameters.items():
            if param.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise ValueError(
                    f"Parameter `{param_name}` is not a parameter can be passed by name"
                )
            value_type_annotation: type[T] | None = _get_data_slice_annotation_type(
                param.annotation
            )
            if value_type_annotation is None:
                raise ValueError(
                    f"Parameter `{param_name}` for {fn} has no value type annotation. "
                    "Please use `cocoindex.DataSlice[T]` where T is the type of the value."
                )
            arg_types.append(value_type_annotation)

        _transform_flow = TransformFlow(fn, arg_types)
        functools.update_wrapper(_transform_flow, fn)
        return _transform_flow

    return _transform_flow_wrapper


def make_setup_bundle(flow_iter: Iterable[Flow]) -> SetupChangeBundle:
    """
    Make a bundle to setup flows with the given names.
    """
    full_names = []
    for fl in flow_iter:
        fl.internal_flow()
        full_names.append(fl.full_name)
    return SetupChangeBundle(_engine.make_setup_bundle(full_names))


def make_drop_bundle(flow_iter: Iterable[Flow]) -> SetupChangeBundle:
    """
    Make a bundle to drop flows with the given names.
    """
    full_names = []
    for fl in flow_iter:
        fl.internal_flow()
        full_names.append(fl.full_name)
    return SetupChangeBundle(_engine.make_drop_bundle(full_names))


def setup_all_flows(report_to_stdout: bool = False) -> None:
    """
    Setup all flows registered in the current process.
    """
    with _flows_lock:
        flow_list = list(_flows.values())
    make_setup_bundle(flow_list).describe_and_apply(report_to_stdout=report_to_stdout)


def drop_all_flows(report_to_stdout: bool = False) -> None:
    """
    Drop all flows registered in the current process.
    """
    with _flows_lock:
        flow_list = list(_flows.values())
    make_drop_bundle(flow_list).describe_and_apply(report_to_stdout=report_to_stdout)
