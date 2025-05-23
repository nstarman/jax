# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
import enum
from functools import partial
import inspect
import itertools as it
from math import prod
import operator as op
from typing import Any, TypeVar, Union

import numpy as np

import jax
import jax.numpy as jnp
from jax.sharding import NamedSharding, PartitionSpec
from jax._src import ad_checkpoint
from jax._src import ad_util
from jax._src import api_util
from jax._src import callback
from jax._src import config
from jax._src import core
from jax._src import custom_derivatives as cd
from jax._src import debugging
from jax._src import dispatch
from jax._src import dtypes
from jax._src import linear_util as lu
from jax._src import ops
from jax._src import pjit
from jax._src import prng
from jax._src import random
from jax._src import sharding_impls
from jax._src import source_info_util
from jax._src import traceback_util
from jax._src import util
from jax._src.core import pvary, pvary_p
from jax._src.core import Tracer, typeof
from jax._src.mesh import (AbstractMesh, Mesh, AxisType, use_abstract_mesh,
                           get_abstract_mesh)
from jax._src.api import _shared_code_pmap, _prepare_pmap
from jax._src.lax import (lax, parallel as lax_parallel, slicing,
                          windowed_reductions, convolution, fft, linalg,
                          special, control_flow, ann)
from jax._src import ffi
from jax._src.lib.mlir import ir
from jax._src.lib.mlir.dialects import sdy
from jax._src.util import (HashableFunction, HashablePartial, unzip2,
                           as_hashable_function, memoize, partition_list,
                           merge_lists, split_list, subs_list2, foreach)
from jax._src.interpreters import batching
from jax._src.interpreters import mlir
from jax._src.interpreters import partial_eval as pe
from jax._src.interpreters import pxla
from jax._src.interpreters import ad
from jax.tree_util import (tree_map, tree_flatten, tree_unflatten,
                           tree_structure, tree_leaves, keystr)
from jax._src.tree_util import (broadcast_prefix, prefix_errors, PyTreeDef,
                                generate_key_paths, KeyPath)
from jax.experimental.multihost_utils import (host_local_array_to_global_array,
                                              global_array_to_host_local_array)

P = PartitionSpec

map, unsafe_map = util.safe_map, map
zip, unsafe_zip = util.safe_zip, zip
traceback_util.register_exclusion(__file__)

# API

Specs = Any  # PyTree[PartitionSpec]
AxisName = Hashable


@traceback_util.api_boundary
def shard_map(f: Callable, mesh: Mesh | AbstractMesh, in_specs: Specs,
              out_specs: Specs, check_rep: bool = True,
              auto: frozenset[AxisName] = frozenset()):
  """Map a function over shards of data.

  Note:
    ``shard_map`` is an experimental API, and still subject to change. For an
    introduction to sharded data, refer to :ref:`sharded-computation`. For a more
    in-depth look at using ``shard_map``, refer to `SPMD multi-device parallelism with shard_map`_.

  Args:
    f: callable to be mapped. Each application of ``f``, or "instance" of ``f``,
      takes as input a shard of the mapped-over arguments and produces a shard
      of the output.
    mesh: a ``jax.sharding.Mesh`` representing the array of devices over which
      to shard the data and on which to execute instances of ``f``. The names of
      the ``Mesh`` can be used in collective communication operations in ``f``.
      This is typically created by a utility function like
      :func:`jax.experimental.mesh_utils.create_device_mesh`.
    in_specs: a pytree with :class:`~jax.sharding.PartitionSpec` instances as leaves,
      with a tree structure that is a tree prefix of the args tuple to be mapped
      over. Similar to :class:`~jax.sharding.NamedSharding`, each ``PartitionSpec``
      represents how the corresponding argument (or subtree of arguments) should
      be sharded along the named axes of ``mesh``. In each ``PartitionSpec``,
      mentioning a ``mesh`` axis name at a position expresses sharding the
      corresponding argument array axis along that positional axis; not
      mentioning an axis name expresses replication. If an argument, or argument
      subtree, has a corresponding spec of None, that argument is not sharded.
    out_specs: a pytree with :class:`~jax.sharding.PartitionSpec` instances as leaves,
      with a tree structure that is a tree prefix of the output of ``f``. Each
      ``PartitionSpec`` represents how the corresponding output shards should be
      concatenated. In each ``PartitionSpec``, metioning a ``mesh`` axis name at
      a position expresses concatenation of that mesh axis's shards along the
      corresponding positional axis. Not mentioning a ``mesh`` axis name
      expresses a promise that the output values are equal along that mesh axis,
      and that rather than concatenating only a single value should be produced.
    check_rep: If True (default) enable additional validity checks and automatic
      differentiation optimizations. The validity checks concern whether any mesh
      axis names not mentioned in ``out_specs`` are consistent with how the outputs
      of ``f`` are replicated. Must be set False if using a Pallas kernel in ``f``.
    auto: (experimental) an optional set of axis names from ``mesh`` over which we
      do not shard the data or map the function, but rather we allow the
      compiler to control sharding. These names cannot be used in ``in_specs``,
      ``out_specs``, or in communication collectives in ``f``.

  Returns:
    A callable that applies the input function ``f`` across data sharded according to
    the ``mesh`` and ``in_specs``.

  Examples:
    For examples, refer to :ref:`sharded-computation` or `SPMD multi-device parallelism with shard_map`_.

  .. _SPMD multi-device parallelism with shard_map: https://docs.jax.dev/en/latest/notebooks/shard_map.html
  """
  return _shard_map(f, mesh, in_specs, out_specs, check_rep, auto)

def _shard_map(f: Callable, mesh: Mesh | AbstractMesh, in_specs: Specs,
               out_specs: Specs | Callable[[], Specs],
               check_rep: bool, auto: frozenset[AxisName]):
  if not callable(f):
    raise TypeError("shard_map requires a callable for its first argument, "
                    f"but got {f} of type {type(f)}.")
  if not isinstance(mesh, (Mesh, AbstractMesh)):
    raise TypeError("shard_map requires a `jax.sharding.Mesh` or a "
                    "`jax.sharding.AbstractMesh` instance for its "
                    f"second argument, but got {mesh} of type {type(mesh)}.")
  if not auto.issubset(mesh.axis_names):
    raise ValueError(f"shard_map requires auto={auto} to be a subset of "
                     f"mesh.axis_names={mesh.axis_names}")
  _check_specs(SpecErrorType.input, in_specs, auto)
  if not callable(out_specs):
    _check_specs(SpecErrorType.out, out_specs, auto)

  @util.wraps(f)
  @traceback_util.api_boundary
  def wrapped(*args):
    fun = lu.wrap_init(f,
                       debug_info=api_util.debug_info("shard_map", f, args, {}))
    args_flat, in_tree = tree_flatten(args)
    fun, out_tree = api_util.flatten_fun_nokwargs(fun, in_tree)
    try: in_specs_flat = broadcast_prefix(in_specs, args,
                                          is_leaf=lambda x: x is None)
    except ValueError:
      e, *_ = prefix_errors(in_specs, args)
      raise e('shard_map in_specs') from None
    dyn_argnums, in_specs_flat = unzip2((i, s) for i, s in enumerate(in_specs_flat)
                                        if s is not None)
    fun, args_flat = api_util.argnums_partial(fun, dyn_argnums, args_flat, False)
    _check_specs_vs_args(f, mesh, in_tree, in_specs, dyn_argnums, in_specs_flat, args_flat)
    in_names_flat = tuple(map(_canonicalize_spec, in_specs_flat))

    @memoize
    def out_names_thunk():
      if callable(out_specs):
        out_specs_ = out_specs()
        _check_specs(SpecErrorType.out, out_specs_, auto)
      else:
        out_specs_ = out_specs
      dummy = tree_unflatten(out_tree(), [object()] * out_tree().num_leaves)
      try: out_specs_flat = broadcast_prefix(out_specs_, dummy)
      except ValueError:
        e, *_ = prefix_errors(out_specs_, dummy)
        raise e('shard_map out_specs') from None
      return tuple(map(_canonicalize_spec, out_specs_flat))

    rewrite = check_rep
    if rewrite:
      if config.varying_axes_in_types.value:
        fun = _implicit_pvary_on_output(fun, out_names_thunk)
      else:
        fun = _efficient_transpose_rewrite(fun, mesh, in_names_flat, out_names_thunk)

    try:
      out_flat = shard_map_p.bind(
          fun, *args_flat, mesh=mesh, in_names=in_names_flat,
          out_names_thunk=out_names_thunk, check_rep=check_rep, rewrite=rewrite,
          auto=auto)
    except _SpecError as e:
      fails, = e.args
      if not callable(out_specs):
        msg = _spec_rank_error(SpecErrorType.out, f, out_tree(), out_specs, fails)
        if any(fail is not no_fail and not fail.shape for fail in fails):
          msg += (" In particular, for rank 0 outputs which are not constant "
                  "over the mesh, add at least one (singleton) axis to them so "
                  "that they can be concatenated using out_specs.")
        raise ValueError(msg) from None
    except _RepError as e:
      fails, = e.args
      if not callable(out_specs):
        msg = _inout_rep_error(f, mesh, out_tree(), out_specs, fails)
        raise ValueError(msg) from None
    return tree_unflatten(out_tree(), out_flat)
  return wrapped

# Internally use AxisNames = dict[int, tuple[AxisName, ...]], not PartitionSpecs
AxisNames = dict[int, tuple[AxisName, ...]]  # TODO(mattjj): make it hashable
def _canonicalize_spec(spec: PartitionSpec) -> AxisNames:
  if isinstance(spec, PartitionSpec):
    return {i: names if isinstance(names, tuple) else (names,)
            for i, names in enumerate(spec) if names is not None}
  else:
    return spec

# Error checking and messages

SpecErrorType = enum.Enum('SpecErrorType', ['input', 'out'])

def _check_specs(error_type: SpecErrorType, specs: Any, auto) -> None:
  if error_type == SpecErrorType.input and specs is None:
    raise TypeError(
        "shard_map in_specs argument must be a pytree of "
        "`jax.sharding.PartitionSpec` instances, but it was None.\n"
        "Instead of `in_specs=None`, did you mean `in_specs=P()`, "
        "where `P = jax.sharding.PartitionSpec`?")
  def check_spec(p):
    if not isinstance(p, PartitionSpec):
      return False
    for names in p:
      if not isinstance(names, tuple):
        names = (names,)
      for name in names:
        if name in auto:
          return False
    return True
  if all(check_spec(p) for p in tree_leaves(specs)): return
  prefix = 'in' if error_type == SpecErrorType.input else 'out'
  msgs = [f"  {prefix}_specs{keystr(key)} is {x} of type {type(x).__name__}, "
          for key, x in generate_key_paths(specs) if not isinstance(x, P)]
  if not msgs:
    for key, p in generate_key_paths(specs):
      for names in p:
        if not isinstance(names, tuple):
          names = (names,)
        for name in names:
          if name in auto:
            msgs.append(f"  {prefix}_specs{keystr(key)} refers to {repr(name)}")
    raise ValueError(
        f"shard_map {prefix}_specs argument cannot refer to an axis "
        f"marked auto ({auto}), but:\n\n"
        + '\n\n'.join(msgs) + '\n\n'
        f"Check the {prefix}_specs values passed to shard_map.")
  raise TypeError(
      f"shard_map {prefix}_specs argument must be a pytree of "
      f"`jax.sharding.PartitionSpec` instances, but:\n\n"
      + '\n\n'.join(msgs) + '\n\n'
      f"Check the {prefix}_specs values passed to shard_map.")

class NoFail: pass
no_fail = NoFail()

def _check_specs_vs_args(
    f: Callable, mesh: Mesh, in_tree: PyTreeDef, in_specs: Specs,
    dyn_argnums: Sequence[int], in_specs_flat: Sequence[P],
    xs: Sequence) -> None:
  in_avals = map(core.shaped_abstractify, xs)
  fail = [a if not len(p) <= a.ndim else no_fail
          for p, a in zip(in_specs_flat, in_avals)]
  if any(f is not no_fail for f in fail):
    fail = _expand_fail(in_tree, dyn_argnums, fail)
    msg = _spec_rank_error(SpecErrorType.input, f, in_tree, in_specs, fail)
    raise ValueError(msg)
  in_names_flat = tuple(map(_canonicalize_spec, in_specs_flat))
  fail = [a if any(a.shape[d] % prod(mesh.shape[n] for n in ns)
                   for d, ns in names.items()) else no_fail
          for a, names in zip(in_avals, in_names_flat)]
  if any(f is not no_fail for f in fail):
    fail = _expand_fail(in_tree, dyn_argnums, fail)
    msg = _spec_divisibility_error(f, mesh, in_tree, in_specs, fail)
    raise ValueError(msg)

def _expand_fail(in_tree: PyTreeDef, dyn_argnums: Sequence[int],
                 fail: Sequence[core.ShapedArray | NoFail]
                 ) -> list[core.ShapedArray | NoFail]:
  fail_: list[core.ShapedArray | NoFail] = [no_fail] * in_tree.num_leaves
  for i, f in zip(dyn_argnums, fail):
    fail_[i] = f
  return fail_

def _spec_rank_error(
    error_type: SpecErrorType, f: Callable, tree: PyTreeDef, specs: Specs,
    fails: list[core.ShapedArray | NoFail]) -> str:
  fun_name = getattr(f, '__name__', str(f))
  if error_type == SpecErrorType.input:
    prefix, base = 'in', 'args'
    ba = _try_infer_args(f, tree)
  else:
    prefix, base = 'out', f'{fun_name}(*args)'
  msgs = []
  for (spec_key, spec), (fail_key, aval) in _iter_paths(tree, specs, fails):
    extra = ""
    if error_type == SpecErrorType.input and ba is not None:
      arg_key, *_ = fail_key
      if arg_key.idx < len(ba.arguments):
        param_name = list(ba.arguments.keys())[arg_key.idx]
        extra = (f", where {base}{arg_key} is bound to {fun_name}'s "
                 f"parameter '{param_name}',")
      else:
        param = list(ba.signature.parameters.values())[-1]
        assert param.kind == inspect.Parameter.VAR_POSITIONAL
        extra = (f", where {base}{arg_key} is the index "
                 f"{arg_key.idx - len(ba.signature.parameters) + 1} component "
                 f"of {fun_name}'s varargs parameter '{param.name}',")
    msgs.append(
        f"* {prefix}_specs{keystr(spec_key)} is {spec} which has length "
        f"{len(spec)}, but "
        f"{base}{keystr(fail_key)}{extra} has shape {aval.str_short()}, "
        f"which has rank {aval.ndim} (and {aval.ndim} < {len(spec)})")
  assert msgs
  if len(msgs) == 1: msgs = [msgs[0][2:]]  # remove the bullet point
  msg = (f"shard_map applied to the function '{fun_name}' was given an "
         f"{prefix}_specs entry which is too long to be compatible with the "
         f"corresponding {prefix}put value from the function:\n\n"
         + '\n\n'.join(msgs) + '\n\n' +
         f"Entries in {prefix}_specs must be of length no greater than the "
         f"number of axes in the corresponding {prefix}put value.\n\n"
         f"Either revise the spec to be shorter, or modify '{fun_name}' so "
         f"that its {prefix}puts have sufficient rank.")
  if any(not aval.ndim for _, (_, aval) in _iter_paths(tree, specs, fails)):
    msg += (f"\n\nFor scalar values (rank 0), consider using an {prefix}_specs "
            "entry of `P()`, where `P = jax.sharding.PartitionSpec`.")
  return msg

def _spec_divisibility_error(
    f: Callable, mesh: Mesh, tree: PyTreeDef, specs: Specs,
    fails: list[core.ShapedArray | NoFail]) -> str:
  ba = _try_infer_args(f, tree)
  fun_name = getattr(f, '__name__', str(f))
  msgs = []
  for (spec_key, spec), (fail_key, aval) in _iter_paths(tree, specs, fails):
    extra = ""
    if ba is not None:
      arg_key, *_ = fail_key
      if arg_key.idx < len(ba.arguments):
        param_name = list(ba.arguments.keys())[arg_key.idx]
        extra = (f", where args{arg_key} is bound to {fun_name}'s "
                 f"parameter '{param_name}',")
      else:
        param = list(ba.signature.parameters.values())[-1]
        assert param.kind == inspect.Parameter.VAR_POSITIONAL
        extra = (f", where args{arg_key} is the index "
                 f"{arg_key.idx - len(ba.signature.parameters) + 1} component "
                 f"of {fun_name}'s varargs parameter '{param.name}',")
    names = _canonicalize_spec(spec)
    for d, ns in names.items():
      if aval.shape[d] % prod(mesh.shape[n] for n in ns):
        axis = f"axes {ns}" if len(ns) > 1 else f"axis '{ns[0]}'"
        total = 'total ' if len(ns) > 1 else ''
        sz = prod(mesh.shape[n] for n in ns)
        msgs.append(
            f"* args{keystr(fail_key)} of shape {aval.str_short()}{extra} "
            f"corresponds to in_specs{keystr(spec_key)} of value {spec}, "
            f"which maps array axis {d} (of size {aval.shape[d]}) to mesh "
            f"{axis} (of {total}size {sz}), but {sz} does not evenly divide "
            f"{aval.shape[d]}")
  assert msgs
  if len(msgs) == 1: msgs = [msgs[0][2:]]  # remove the bullet point
  msg = (f"shard_map applied to the function '{fun_name}' was given argument "
         f"arrays with axis sizes that are not evenly divisible by the "
         f"corresponding mesh axis sizes:\n\n"
         f"The mesh given has shape {tuple(mesh.shape.values())} with "
         f"corresponding axis names {mesh.axis_names}.\n\n"
         + '\n\n'.join(msgs) + '\n\n' +
         f"Array arguments' axis sizes must be evenly divisible by the mesh "
         f"axis or axes indicated by the corresponding elements of the "
         f"argument's in_specs entry. Consider checking that in_specs are "
         f"correct, and if so consider changing the mesh axis sizes or else "
         f"padding the input and adapting '{fun_name}' appropriately.")
  return msg

def _inout_rep_error(f: Callable, mesh: Mesh, tree: PyTreeDef, specs: Specs,
                     fails: list[set | NoFail]) -> str:
  fun_name = getattr(f, '__name__', str(f))
  msgs = []
  for (spec_key, spec), (fail_key, rep) in _iter_paths(tree, specs, fails):
    dst = _canonicalize_spec(spec)
    unmentioned = _unmentioned(mesh, dst)
    if len(unmentioned) > 1:
      need_rep = ','.join(map(str, unmentioned))
      got_rep = ','.join(map(str, rep))
      diff = ','.join(map(str, [n for n in unmentioned if n not in rep]))
      msgs.append(
          f"* out_specs{keystr(spec_key)} is {spec} which implies that the "
          f"corresponding output value is replicated across mesh axes "
          f"{{{need_rep}}}, but could only infer replication over {{{got_rep}}}, "
          f"which is missing the required axes {diff}")
    else:
      need_rep_, = unmentioned
      msgs.append(
          f"* out_specs{keystr(spec_key)} is {spec} which implies that the "
          f"corresponding output value is replicated across mesh axis "
          f"'{need_rep_}', but could not infer replication over any axes")
  assert msgs
  if len(msgs) == 1: msgs = [msgs[0][2:]]  # remove the bullet point
  msg = (f"shard_map applied to the function '{fun_name}' was given "
         f"out_specs which require replication which can't be statically "
         f"inferred given the mesh:\n\n"
         f"The mesh given has shape {tuple(mesh.shape.values())} with "
         f"corresponding axis names {mesh.axis_names}.\n\n"
         + '\n\n'.join(msgs) + '\n\n' +
         "Check if these output values are meant to be replicated over those "
         "mesh axes. If not, consider revising the corresponding out_specs "
         "entries. If so, consider disabling the check by passing the "
         "check_rep=False argument to shard_map.")
  return msg

def _unmentioned(mesh: Mesh, names: AxisNames) -> list[AxisName]:
  name_set = {n for ns in names.values() for n in ns}
  return [n for n in mesh.axis_names if n not in name_set]


def _try_infer_args(f, tree):
  dummy_args = tree_unflatten(tree, [False] * tree.num_leaves)
  try:
    return inspect.signature(f).bind(*dummy_args)
  except (TypeError, ValueError):
    return None

T = TypeVar('T')
def _iter_paths(tree: PyTreeDef, specs: Specs, fails: list[T | NoFail]
                ) -> list[tuple[tuple[KeyPath, P], tuple[KeyPath, T]]]:
  failures = tree_unflatten(tree, fails)
  failures_aug = generate_key_paths(failures)
  specs_ = tree_unflatten(tree_structure(specs), generate_key_paths(specs))
  leaf = lambda x: x is None or type(x) is tuple and len(x) == 2 and type(x[1]) is P
  specs_aug = broadcast_prefix(specs_, failures, is_leaf=leaf)
  return [(s, (fail_key, fail_data)) for s, (fail_key, fail_data)
          in zip(specs_aug, failures_aug)
          if s is not None and fail_data is not no_fail]

# Primitive

JaxType = Any
MaybeTracer = Union[JaxType, Tracer]

class ShardMapPrimitive(core.Primitive):
  multiple_results = True

  def bind(self, *args, **params):
    return self._true_bind(*args, **params)

  def bind_with_trace(self, trace, fun_and_args, params):
    fun: lu.WrappedFun
    fun, *args = fun_and_args
    return trace.process_shard_map(shard_map_p, fun, args, **params)

  def get_bind_params(self, params):
    new_params = dict(params)
    jaxpr: core.Jaxpr = new_params.pop('jaxpr')
    subfun = lu.hashable_partial(lu.wrap_init(core.eval_jaxpr,
                                              debug_info=jaxpr.debug_info),
                                 jaxpr, ())
    axes = new_params.pop('out_names')
    new_params['out_names_thunk'] = HashableFunction(lambda: axes, closure=axes)
    return [subfun], new_params

shard_map_p = ShardMapPrimitive('shard_map')

# Staging

@util.cache(max_size=256, trace_context_in_key=True)
def _as_manual_mesh(mesh, auto: frozenset):
  manual_axes = tuple(set(mesh.axis_names) - auto)
  cur_mesh = get_abstract_mesh()
  if cur_mesh.empty:
    cur_mesh = mesh
  explicit_axes, auto_axes = set(), set()  # type: ignore
  for a in auto:
    if cur_mesh._name_to_type[a] == AxisType.Auto:
      auto_axes.add(a)
    else:
      assert cur_mesh._name_to_type[a] == AxisType.Explicit
      explicit_axes.add(a)

  new_axis_types = []
  for n in mesh.axis_names:
    if n in manual_axes:
      new_axis_types.append(AxisType.Manual)
    elif n in auto_axes:
      new_axis_types.append(AxisType.Auto)
    else:
      assert n in explicit_axes
      new_axis_types.append(AxisType.Explicit)
  return AbstractMesh(mesh.axis_sizes, mesh.axis_names,
                      axis_types=tuple(new_axis_types))


def _extend_axis_env(mesh, auto):
  return core.extend_axis_env_nd([(k, v) for k, v in mesh.shape.items()
                                 if k not in auto])

def _shard_map_staging(
    trace: pe.DynamicJaxprTrace, prim: core.Primitive, f: lu.WrappedFun,
    in_tracers: Sequence[Any], *, mesh: Mesh,
    in_names: tuple[AxisNames, ...],
    out_names_thunk: Callable[[], tuple[AxisNames, ...]],
    check_rep: bool,
    rewrite: bool,
    auto: frozenset,
  ) -> Sequence[pe.DynamicJaxprTracer]:
  in_tracers = map(trace.to_jaxpr_tracer, in_tracers)
  in_avals = [t.aval for t in in_tracers]
  in_avals_ = map(partial(_shard_aval, mesh, auto, check_rep), in_names,
                  in_avals)
  manual_mesh = _as_manual_mesh(mesh, auto)
  with (_extend_axis_env(mesh, auto), use_abstract_mesh(manual_mesh),
        config._check_rep(check_rep)):
    jaxpr, out_avals_, consts, () = pe.trace_to_jaxpr_dynamic(f, in_avals_)
  _check_names(out_names_thunk(), out_avals_)
  if check_rep:
    out_rep = [_vma_to_rep(mesh, auto, v.aval.vma) for v in jaxpr.outvars]
    _check_reps(mesh, auto, out_names_thunk(), out_rep)
  out_avals = map(_check_shapedarray, out_avals_)
  out_avals = [_check_shapedarray(_unshard_aval(mesh, check_rep, names, aval))
               for names, aval in zip(out_names_thunk(), out_avals)]
  source_info = source_info_util.current()
  out_tracers = [pe.DynamicJaxprTracer(trace, a, source_info) for a in out_avals]
  invars = map(trace.getvar, in_tracers)
  constvars = map(trace.getvar, map(trace.to_jaxpr_tracer, consts))
  outvars = map(trace.makevar, out_tracers)
  in_names_staged = ({},) * len(consts) + tuple(in_names)  # type: ignore
  with (_extend_axis_env(mesh, auto), use_abstract_mesh(manual_mesh),
        config._check_rep(check_rep)):
    jaxpr = pe.convert_constvars_jaxpr(jaxpr)
  params = dict(mesh=mesh, in_names=in_names_staged,
                out_names=tuple(out_names_thunk()), jaxpr=jaxpr,
                check_rep=check_rep, rewrite=rewrite, auto=auto)
  effs = core.filter_named_axis_effects(jaxpr.effects, mesh.axis_names)
  eqn = pe.new_jaxpr_eqn([*constvars, *invars], outvars, prim, params,
                         effs, source_info)
  trace.frame.add_eqn(eqn)
  return out_tracers
pe.DynamicJaxprTrace.process_shard_map = _shard_map_staging

# TODO add underscore version, for direct-linearize to consume

def _check_shapedarray(aval: core.AbstractValue) -> core.ShapedArray:
  assert isinstance(aval, core.ShapedArray)
  return aval

def _shard_aval(mesh: Mesh, auto, check_rep, names: AxisNames,
                aval: core.AbstractValue) -> core.AbstractValue:
  if type(aval) in core.shard_aval_handlers:
    return core.shard_aval_handlers[type(aval)](mesh, auto, check_rep, names,
                                                aval)
  raise NotImplementedError(f"Unsupported aval type: {type(aval)}")

def _unshard_aval(mesh: Mesh, check_rep, names: AxisNames,
                  aval: core.AbstractValue) -> core.AbstractValue:
  if type(aval) in core.unshard_aval_handlers:
    return core.unshard_aval_handlers[type(aval)](mesh, check_rep, names, aval)
  else:
    raise NotImplementedError(f"Unsupported aval type: {type(aval)}")

def _shard_shaped_array(mesh: Mesh, auto: frozenset, check_rep, names: AxisNames,
                        aval: core.AbstractValue) -> core.AbstractValue:
  assert isinstance(aval, core.ShapedArray)
  new_shape = tuple(sz // prod(mesh.shape[n] for n in names.get(i, ()))
                    for i, sz in enumerate(aval.shape))
  manual_mesh = _as_manual_mesh(mesh, auto)
  new_sharding = NamedSharding(manual_mesh, aval.sharding.spec)
  vma = (frozenset({n for ns in names.values() for n in ns})
         if config.varying_axes_in_types.value and check_rep else frozenset())
  return aval.update(shape=new_shape, sharding=new_sharding, vma=vma)
core.shard_aval_handlers[core.ShapedArray] = _shard_shaped_array

def _unshard_shaped_array(mesh: Mesh, check_rep, names: AxisNames,
                          aval: core.AbstractValue,) -> core.AbstractValue:
  assert isinstance(aval, core.ShapedArray)
  new_shape = tuple(sz * prod(mesh.shape[n] for n in names.get(i, ()))
                    for i, sz in enumerate(aval.shape))
  names_spec = _names_to_pspec(names)._normalized_spec_for_aval(aval.ndim)
  if aval.ndim == 0:
    out_spec = names_spec
  else:
    out_spec = []  # type: ignore
    for name_s, aval_s in zip(names_spec, aval.sharding.spec):
      if name_s and not aval_s:
        out_spec.append(name_s)
      elif aval_s and not name_s:
        out_spec.append(aval_s)
      elif not name_s and not aval_s:
        out_spec.append(None)
      else:
        assert name_s and aval_s
        name_s = name_s if isinstance(name_s, tuple) else (name_s,)
        aval_s = aval_s if isinstance(aval_s, tuple) else (aval_s,)
        out_spec.append(name_s + aval_s)
    out_spec = PartitionSpec(*out_spec)
  new_mesh = (mesh.abstract_mesh if get_abstract_mesh().empty else
              get_abstract_mesh())
  new_sharding = NamedSharding(new_mesh, out_spec)
  manual_axes = set(new_mesh.manual_axes)
  vma = (frozenset(v for v in aval.vma if v in manual_axes)
         if config.varying_axes_in_types.value and check_rep else frozenset())
  return aval.update(shape=new_shape, sharding=new_sharding, vma=vma)
core.unshard_aval_handlers[core.ShapedArray] = _unshard_shaped_array

# Type-checking

RepType = Any

def _shard_map_typecheck(_, *in_atoms, jaxpr, mesh, in_names, out_names,
                         check_rep, rewrite, auto):
  # TODO(mattjj,parkers): check auto
  for v, x, in_name in zip(jaxpr.invars, in_atoms, in_names):
    if not core.typecompat(v.aval, _shard_aval(
        mesh, auto, check_rep, in_name, x.aval)):
      raise core.JaxprTypeError("shard_map argument avals not compatible with "
                                "jaxpr binder avals and in_names")
  with _extend_axis_env(mesh, auto), config._check_rep(check_rep):
    core.check_jaxpr(jaxpr)
  if check_rep:
    if config.varying_axes_in_types.value:
      out_rep = [_vma_to_rep(mesh, auto, v.aval.vma) for v in jaxpr.outvars]
    else:
      in_rep = map(partial(_in_names_to_rep, mesh), in_names)
      out_rep = _check_rep(mesh, jaxpr, in_rep)
    for rep, dst in zip(out_rep, out_names):
      if not _valid_repeats(mesh, auto, rep, dst):
        raise core.JaxprTypeError("shard_map can't prove output is "
                                  "sufficiently replicated")
  out_avals_sharded = [x.aval for x in jaxpr.outvars]
  out_avals = map(partial(_unshard_aval, mesh, check_rep), out_names,
                  out_avals_sharded)
  effs = core.filter_named_axis_effects(jaxpr.effects, mesh.axis_names)
  return out_avals, effs
core.custom_typechecks[shard_map_p] = _shard_map_typecheck

def _in_names_to_rep(mesh: Mesh, names: AxisNames) -> set[AxisName]:
  return set(mesh.axis_names) - {n for ns in names.values() for n in ns}

def _check_rep(mesh: Mesh, jaxpr: core.Jaxpr, in_rep: Sequence[RepType]
               ) -> Sequence[RepType]:
  env: dict[core.Var, RepType] = {}

  def read(x: core.Atom) -> RepType:
    return env[x] if type(x) is core.Var else None

  def write(v: core.Var, val: RepType) -> None:
    env[v] = val

  foreach(write, jaxpr.constvars, [set(mesh.axis_names)] * len(jaxpr.constvars))
  foreach(write, jaxpr.invars, in_rep)
  last_used = core.last_used(jaxpr)
  for e in jaxpr.eqns:
    rule = _check_rules.get(e.primitive, partial(_rule_missing, e.primitive))
    out_rep = rule(mesh, *map(read, e.invars), **e.params)
    if e.primitive.multiple_results:
      out_rep = (out_rep if isinstance(out_rep, (list, tuple)) else
                 [out_rep] * len(e.outvars))
      foreach(write, e.outvars, out_rep)
    else:
      write(e.outvars[0], out_rep)
    core.clean_up_dead_vars(e, env, last_used)
  return map(read, jaxpr.outvars)

def _valid_repeats(mesh: Mesh, auto, rep: RepType, dst: AxisNames) -> bool:
  return rep is None or (set(_unmentioned(mesh, dst)) - auto).issubset(rep)

def _rule_missing(prim: core.Primitive, *_, **__):
  raise NotImplementedError(
      f"No replication rule for {prim}. As a workaround, pass the "
      "`check_rep=False` argument to `shard_map`. To get this fixed, open an "
      "issue at https://github.com/jax-ml/jax/issues")

# Lowering


def _shardy_shard_map_sharding(
    ctx: mlir.LoweringRuleContext, mesh, auto, names, aval_in
) -> sharding_impls.SdyArraySharding:
  axes = {name: i for i, ns in names.items() for name in ns}
  ns = _make_scoped_manual_sharding(ctx, mesh, axes)
  if dtypes.issubdtype(aval_in.dtype, dtypes.extended):
    ns = sharding_impls.physical_sharding(aval_in, ns)
    aval_in = core.physical_aval(aval_in)
  sdy_sharding = ns._to_sdy_sharding(aval_in.ndim)
  if auto:
    for dim_sharding in sdy_sharding.dimension_shardings:
      # Only allow dimensions which have no sharding to be auto-sharded.
      if not dim_sharding.axes:
        dim_sharding.is_closed = False
  return sdy_sharding


def _shard_map_lowering_shardy(
    ctx, in_nodes, jaxpr, mesh, in_names, out_names, auto, check_rep):
  in_avals_ = [v.aval for v in jaxpr.invars]
  if isinstance(ctx.module_context.axis_context, sharding_impls.SPMDAxisContext):
    # Nested `ManualComputationOp`s cannot refer to axes that are already
    # manual. So figure out what axes are free thus far.
    free_axes = frozenset(mesh.axis_names) - ctx.module_context.axis_context.manual_axes
    shardy_manual_axes = free_axes - auto
  else:
    shardy_manual_axes = frozenset(mesh.axis_names) - auto
  new_axis_context = sharding_impls.SPMDAxisContext(
        mesh, frozenset(mesh.axis_names) - auto)
  sub_ctx = ctx.module_context.replace(axis_context=new_axis_context)

  # The order of manual axes should match the order of mesh.axis_names to avoid
  # non-determinism issues.
  manual_axes = [a for a in mesh.axis_names
                 if a in shardy_manual_axes]
  if np.prod([mesh.shape[a] for a in manual_axes]) == 1:
    # No need for a `ManualComputationOp` if all manual axes are size 1.
    with _extend_axis_env(mesh, auto), config._check_rep(check_rep):
      out_nodes, _ = mlir.jaxpr_subcomp(
          sub_ctx, jaxpr, ctx.name_stack, mlir.TokenSet(), (), *in_nodes,
          dim_var_values=ctx.dim_var_values)
    return out_nodes

  in_shardings = sharding_impls.SdyArrayShardingList(map(
      partial(_shardy_shard_map_sharding, ctx, mesh, auto),
      in_names, ctx.avals_in)).build()
  out_shardings = sharding_impls.SdyArrayShardingList(map(
      partial(_shardy_shard_map_sharding, ctx, mesh, auto),
      out_names, ctx.avals_out)).build()
  output_types = map(mlir.aval_to_ir_type, ctx.avals_out)
  manual_computation_op = sdy.ManualComputationOp(
      output_types, in_nodes, in_shardings, out_shardings,
      sdy.ManualAxesAttr.get(
          ir.ArrayAttr.get([ir.StringAttr.get(i) for i in manual_axes])))
  block = ir.Block.create_at_start(
      manual_computation_op.body, map(mlir.aval_to_ir_type, in_avals_))
  with (ir.InsertionPoint(block), _extend_axis_env(mesh, auto),
        config._check_rep(check_rep)):
    out_nodes_, _ = mlir.jaxpr_subcomp(
        sub_ctx, jaxpr, ctx.name_stack, mlir.TokenSet(), (), *block.arguments,
        dim_var_values=ctx.dim_var_values)
    sdy.ReturnOp([ir.Value(x) for x in out_nodes_])

  return manual_computation_op.results


def _shard_map_lowering(ctx, *in_nodes, jaxpr, mesh, in_names, out_names,
                        check_rep, rewrite, auto):
  if config.use_shardy_partitioner.value:
    return _shard_map_lowering_shardy(
        ctx, in_nodes, jaxpr, mesh, in_names, out_names, auto, check_rep)

  in_avals_ = [v.aval for v in jaxpr.invars]
  out_avals_ = [x.aval for x in jaxpr.outvars]
  in_nodes_ = map(partial(_xla_shard, ctx, mesh, auto), in_names, ctx.avals_in,
                  in_avals_, in_nodes)
  manual_axes = frozenset(mesh.axis_names) - auto
  new_axis_context = sharding_impls.SPMDAxisContext(mesh, manual_axes)
  sub_ctx = ctx.module_context.replace(axis_context=new_axis_context)
  with _extend_axis_env(mesh, auto), config._check_rep(check_rep):
    out_nodes_, tokens_out = mlir.call_lowering(
        "shmap_body", ctx.name_stack, jaxpr, None, sub_ctx, in_avals_,
        out_avals_, ctx.tokens_in, *in_nodes_, dim_var_values=ctx.dim_var_values,
        arg_names=map(_pspec_mhlo_attrs, in_names, in_avals_),
        result_names=map(_pspec_mhlo_attrs, out_names, out_avals_))
  ctx.set_tokens_out(tokens_out)
  return map(partial(_xla_unshard, ctx, mesh, auto), out_names, out_avals_,
             ctx.avals_out, out_nodes_)
mlir.register_lowering(shard_map_p, _shard_map_lowering)

def _make_scoped_manual_sharding(ctx, mesh, axes):
  axis_ctx = ctx.module_context.axis_context
  if isinstance(axis_ctx, sharding_impls.SPMDAxisContext):
    manual_axes = axis_ctx.manual_axes
  else:
    manual_axes = frozenset({})
  return NamedSharding(
      mesh, sharding_impls.array_mapping_to_axis_resources(axes),  # pytype: disable=wrong-arg-types
      _manual_axes=manual_axes)

def _xla_shard(ctx: mlir.LoweringRuleContext, mesh, auto, names,
               aval_in, aval_out, x):
  if prod([size for n, size in mesh.shape.items() if n not in auto]) == 1:
    return x
  axes = {name: i for i, ns in names.items() for name in ns}
  ns = _make_scoped_manual_sharding(ctx, mesh, axes)
  if dtypes.issubdtype(aval_in.dtype, dtypes.extended):
    ns = sharding_impls.physical_sharding(aval_in, ns)
    aval_in = core.physical_aval(aval_in)
  shard_proto = ns._to_xla_hlo_sharding(aval_in.ndim).to_proto()
  unspecified = set(range(aval_in.ndim)) if auto else set()
  sx = mlir.wrap_with_sharding_op(ctx, x, aval_in, shard_proto,
                                  unspecified_dims=unspecified)
  manual_proto = pxla.manual_proto(aval_in, frozenset(mesh.axis_names) - auto, mesh)
  return mlir.wrap_with_full_to_shard_op(ctx, sx, aval_out, manual_proto, unspecified)

def _xla_unshard(ctx: mlir.LoweringRuleContext, mesh, auto, names,
                 aval_in, aval_out, x):
  if prod([size for n, size in mesh.shape.items() if n not in auto]) == 1:
    return x
  axes = {name: i for i, ns in names.items() for name in ns}
  ns = _make_scoped_manual_sharding(ctx, mesh, axes)
  if dtypes.issubdtype(aval_out.dtype, dtypes.extended):
    ns = sharding_impls.physical_sharding(aval_out, ns)
    aval_out = core.physical_aval(aval_out)
  unspecified = set(range(aval_out.ndim)) if auto else set()
  if dtypes.issubdtype(aval_in.dtype, dtypes.extended):
    aval_in = core.physical_aval(aval_in)
  manual_proto = pxla.manual_proto(aval_in, frozenset(mesh.axis_names) - auto, mesh)
  sx = mlir.wrap_with_sharding_op(ctx, x, aval_in, manual_proto, unspecified_dims=unspecified)
  shard_proto = ns._to_xla_hlo_sharding(aval_out.ndim).to_proto()
  return mlir.wrap_with_shard_to_full_op(ctx, sx, aval_out, shard_proto,
                                         unspecified)

def _pspec_mhlo_attrs(names: AxisNames, aval: core.AbstractValue) -> str:
  if isinstance(aval, core.ShapedArray):
    return str(map(names.get, range(aval.ndim)))
  return ''

# Eager evaluation

def get_mesh_from_args(args_flat, mesh):
  for a in args_flat:
    if hasattr(a, 'sharding') and isinstance(a.sharding, NamedSharding):
      if a.sharding.mesh.shape_tuple != mesh.shape_tuple:
        aval = core.shaped_abstractify(a)
        raise ValueError(
            f"Mesh shape of the input {a.sharding.mesh.shape_tuple} does not"
            " match the mesh shape passed to shard_map "
            f" {mesh.shape_tuple} for shape {aval.str_short()}")
      mesh = a.sharding.mesh
  if isinstance(mesh, AbstractMesh):
    raise ValueError(
        "Please pass `jax.Array`s with a `NamedSharding` as input to"
        " `shard_map` when passing `AbstractMesh` to the mesh argument.")
  assert isinstance(mesh, Mesh)
  return mesh

def _rep_to_vma(mesh, auto, rep: frozenset[AxisName]) -> frozenset[AxisName]:
  return frozenset((set(mesh.axis_names) - auto) - rep)

def _rep_to_spec(mesh, auto, rep):
  return _vma_to_spec(mesh, _rep_to_vma(mesh, auto, rep))

def _vma_to_spec(mesh, vma):
  return P(tuple(i for i in mesh.axis_names if i in vma))

def _names_to_vma(names):
  return {n for ns in names.values() for n in ns}

def _vma_to_rep(mesh, auto, vma):
  return frozenset((set(mesh.axis_names) - auto) - vma)

def _shard_map_impl(trace, prim, fun, args, *, mesh, in_names, out_names_thunk,
                    check_rep, rewrite, auto):
  if auto: raise NotImplementedError
  del prim
  if isinstance(mesh, AbstractMesh):
    mesh = get_mesh_from_args(args, mesh)
  cur_mesh = get_abstract_mesh()
  args = map(partial(_unmatch_spec, mesh, check_rep, context_mesh=cur_mesh),
             in_names, args)
  in_rep = map(partial(_in_names_to_rep, mesh), in_names)
  outs, out_rep = _run_shmap(fun, mesh, auto, args, in_rep, check_rep, cur_mesh)
  out_avals = [core.mapped_aval(x.shape[0], 0, core.get_aval(x)) for x in outs]
  _check_names(out_names_thunk(), out_avals)  # pytype: disable=wrong-arg-types
  if check_rep and config.varying_axes_in_types.value:
    _check_reps(mesh, auto, out_names_thunk(), out_rep)
    src_pspecs = tuple(_rep_to_spec(mesh, auto, r) for r in out_rep)
  else:
    src_pspecs = tuple(P(mesh.axis_names) for _ in out_rep)
  dst_pspecs = map(_names_to_pspec, out_names_thunk())
  return map(partial(_match_spec, mesh, check_rep), src_pspecs, dst_pspecs,
             outs)
core.EvalTrace.process_shard_map = _shard_map_impl

def _run_shmap(f, mesh, auto, args, reps, check_rep, context_mesh):
  trace = ShardMapTrace(mesh, auto, check_rep, context_mesh)
  in_tracers = map(partial(ShardMapTracer, trace), reps, args)
  manual_mesh = _as_manual_mesh(mesh, auto)
  with (core.set_current_trace(trace), _extend_axis_env(mesh, auto),
        use_abstract_mesh(manual_mesh), config._check_rep(check_rep)):
    ans = f.call_wrapped(*in_tracers)
    outs, out_rep = unzip2(map(trace.to_val_rep_pair, ans))
  return outs, out_rep

def _names_to_pspec(names: AxisNames) -> PartitionSpec:
  ndmin = max(names) + 1 if names else 0
  unpack = lambda t: t[0] if t is not None and len(t) == 1 else t
  return PartitionSpec(*(unpack(names.get(i)) for i in range(ndmin)))

def _unmatch_spec(mesh: Mesh, check_rep, src: AxisNames, x: JaxType,
                  context_mesh) -> JaxType:
  with (core.eval_context(), jax.disable_jit(False),
        use_abstract_mesh(context_mesh)):
    return jax.jit(HashablePartial(_unmatch, mesh, check_rep,
                                   tuple(src.items())))(x)

def _unmatch(mesh, check_rep, src_tup, x):
  src = _names_to_pspec(dict(src_tup))
  if check_rep and config.varying_axes_in_types.value:
    used_axes = {i for _, ns in src_tup for i in ns}
    dst = P(tuple(i for i in mesh.axis_names if i in used_axes))
  else:
    dst = P(mesh.axis_names)
    check_rep = False
  return shard_map(_add_singleton, mesh, (src,), dst, check_rep=check_rep)(x)

def _check_names(names: Sequence[AxisNames], avals: Sequence[core.ShapedArray]
                 ) -> None:
  fail = [a if n and not max(n) < a.ndim else no_fail
          for n, a in zip(names, avals)]
  if any(f is not no_fail for f in fail):
    raise _SpecError(fail)

class _SpecError(Exception):
  pass

def _check_reps(mesh, auto, names, reps):
  fail = [r if not _valid_repeats(mesh, auto, r, n) else no_fail
          for n, r in zip(names, reps)]
  if any(f is not no_fail for f in fail):
    raise _RepError(fail)

class _RepError(Exception):
  pass

def _check_reps2(mesh, reps_dest, reps):
  fail = [src if not dst.issubset(src) else no_fail
          for dst, src in zip(reps_dest, reps)]
  if any(f is not no_fail for f in fail): raise _RepError(fail)

def _match_spec(mesh: Mesh, check_rep, src_pspec: PartitionSpec,
                dst_pspec: PartitionSpec, x: JaxType) -> JaxType:
  fn = HashablePartial(_match, mesh, check_rep, src_pspec, dst_pspec)
  with core.eval_context(), jax.disable_jit(False):
    return jax.jit(fn, out_shardings=NamedSharding(mesh, dst_pspec))(x)

def _match(mesh, check_rep, src_pspec, dst_pspec, x):
  if not config.varying_axes_in_types.value:
    check_rep = False
  return shard_map(_rem_singleton, mesh, src_pspec, dst_pspec,
                   check_rep=check_rep)(x)

def _rem_singleton(x): return jnp.squeeze(x, axis=0)
def _add_singleton(x): return jnp.expand_dims(x, axis=0)

def _maybe_check_special(outs):
  if not config.debug_nans.value and not config.debug_infs.value: return
  bufs = [s.data for leaf in tree_leaves(outs)
          for s in getattr(leaf, 'addressable_shards', [])]
  try:
    dispatch.check_special('shard_map', bufs)
  except dispatch.InternalFloatingPointError as e:
    raise FloatingPointError(f'Invalid value ({e.ty}) encountered in sharded computation.') from None

class ShardMapTrace(core.Trace):
  __slots__ = ("mesh", "auto", "check", "context_mesh")

  mesh: Mesh
  auto: frozenset[AxisName]
  check: bool
  context_mesh: AbstractMesh

  def __init__(self, mesh, auto, check, context_mesh):
    super().__init__()
    self.mesh = mesh
    self.auto = auto
    self.check = check
    self.context_mesh = context_mesh

  def to_val_rep_pair(self, val):
    if isinstance(val, ShardMapTracer):
      return val.val, val.rep
    elif isinstance(val, Tracer):
      raise Exception(f"Shouldn't have any non-shard_map tracers: {val}")
    else:
      val_ = _unmatch_spec(self.mesh, self.check, {}, val, self.context_mesh)
      if self.check and config.varying_axes_in_types.value:
        return val_, frozenset(self.mesh.axis_names) - self.auto
      else:
        return val_, None

  def process_primitive(self, prim, tracers, params):
    in_vals, in_rep = unzip2(map(self.to_val_rep_pair, tracers))
    if config.varying_axes_in_types.value:
      if self.check:
        in_vma  = tuple(map(partial(_rep_to_vma, self.mesh, self.auto), in_rep))
        out_avals, _ = prim.abstract_eval(*(typeof(t) for t in tracers), **params)
        out_avals = tuple(out_avals) if type(out_avals) is list else out_avals
        out_vma = tree_map(lambda a: a.vma, out_avals)
        out_rep = tree_map(partial(_vma_to_rep, self.mesh, self.auto), out_vma)
        in_specs  = tuple(map(partial(_vma_to_spec, self.mesh), in_vma))
        out_specs = tree_map(partial(_vma_to_spec, self.mesh), out_vma)
      else:
        out_rep = frozenset()
        in_specs = out_specs = P(self.mesh.axis_names)
    else:
      in_specs = out_specs = P(self.mesh.axis_names)
      rep_rule = _check_rules.get(prim, partial(_rule_missing, prim))
      out_rep = rep_rule(self.mesh, *in_rep, **params) if self.check else set()

    eager_rule = eager_rules.get(prim)
    if eager_rule:
      out_vals = eager_rule(self.mesh, *in_vals, **params)
    else:
      f = HashablePartial(
          _prim_applier, prim, self.check, tuple(params.items()), self.mesh,
          in_specs, out_specs)
      with (core.eval_context(), jax.disable_jit(False), jax.debug_nans(False),
            jax.debug_infs(False), use_abstract_mesh(self.context_mesh)):
        out_vals = jax.jit(f)(*in_vals)
      _maybe_check_special(out_vals)
    if prim.multiple_results:
      out_rep = (out_rep if isinstance(out_rep, (list, tuple))
                 else [out_rep] * len(out_vals))
      return map(partial(ShardMapTracer, self), out_rep, out_vals)
    return ShardMapTracer(self, out_rep, out_vals)

  def process_call(self, call_primitive, fun, tracers, params):
    raise NotImplementedError(
        f"Eager evaluation of `{call_primitive}` inside a `shard_map` isn't "
        "yet supported. Put a `jax.jit` around the `shard_map`-decorated "
        "function, and open a feature request at "
        "https://github.com/jax-ml/jax/issues !")

  def process_map(self, map_primitive, fun, tracers, params):
    raise NotImplementedError(
        "Eager evaluation of `pmap` inside a `shard_map` isn't yet supported."
        "Put a `jax.jit` around the `shard_map`-decorated function, and open "
        "a feature request at https://github.com/jax-ml/jax/issues !")

  def process_custom_jvp_call(self, prim, fun, jvp, tracers, *, symbolic_zeros):
    # Since ShardMapTrace is only used as a base main, we can drop the jvp.
    del prim, jvp, symbolic_zeros
    in_vals, in_rep = unzip2(map(self.to_val_rep_pair, tracers))
    out_vals, out_rep = _run_shmap(fun, self.mesh, self.auto, in_vals, in_rep, self.check,
                                   self.context_mesh)
    return map(partial(ShardMapTracer, self), out_rep, out_vals)

  def process_custom_vjp_call(self, prim, fun, fwd, bwd, tracers, out_trees,
                              symbolic_zeros):
    if symbolic_zeros:
      msg = ("custom_vjp symbolic_zeros support with shard_map is not "
             "implemented; please open an issue at "
             "https://github.com/jax-ml/jax/issues")
      raise NotImplementedError(msg)
    del prim, fwd, bwd, out_trees, symbolic_zeros
    in_vals, in_rep = unzip2(map(self.to_val_rep_pair, tracers))
    out_vals, out_rep = _run_shmap(fun, self.mesh, self.auto, in_vals, in_rep, self.check,
                                   self.context_mesh)
    return map(partial(ShardMapTracer, self), out_rep, out_vals)


class ShardMapTracer(core.Tracer):
  rep: RepType
  val: JaxType

  def __init__(self, trace, rep, val):
    self._trace = trace
    self.rep = rep
    self.val = val

  @property
  def aval(self):
    aval = core.get_aval(self.val)
    out = core.mapped_aval(self._trace.mesh.size, 0, aval)
    new_sharding = NamedSharding(
        _as_manual_mesh(self._trace.mesh, self._trace.auto),
        out.sharding.spec)  # pytype: disable=attribute-error
    manual_axes = set(self._trace.mesh.axis_names) - self._trace.auto
    vma = (frozenset(manual_axes - self.rep)
           if config.varying_axes_in_types.value and config._check_rep.value
           else frozenset())
    return out.update(sharding=new_sharding, vma=vma)

  def to_concrete_value(self):
    if self.rep == set(self._trace.mesh.axis_names):
      with core.eval_context(), use_abstract_mesh(self._trace.context_mesh):
        return core.to_concrete_value(self.val[0])
    else:
      return None

  def __str__(self) -> str:
    pb_names = set(self._trace.mesh.axis_names) - _rep_to_vma(
        self._trace.mesh, self._trace.auto, self.rep)
    self = pvary(self, tuple(pb_names))
    with core.eval_context(), use_abstract_mesh(self._trace.context_mesh):
      blocks = list(self.val)
    mesh = self._trace.mesh
    axis_names = f"({', '.join(map(str, mesh.axis_names))},)"
    return '\n'.join(
        f"On {device} at mesh coordinates {axis_names} = {idx}:\n{block}\n"
        for (idx, device), block in zip(np.ndenumerate(mesh.devices), blocks))
  __repr__ = __str__  # for debuggers, like `p x`

def _prim_applier(prim, check_rep, params_tup, mesh, in_specs, out_specs, *args):
  def apply(*args):
    outs = prim.bind(*map(_rem_singleton, args), **dict(params_tup))
    return tree_map(_add_singleton, outs)
  out_specs = list(out_specs) if type(out_specs) is tuple else out_specs
  if not config.varying_axes_in_types.value:
    check_rep = False
  return shard_map(apply, mesh, in_specs, out_specs, check_rep=check_rep)(*args)

eager_rules: dict[core.Primitive, Callable] = {}


# TODO(mattjj): working around an apparent XLA or PjRt bug, remove eventually
def _debug_callback_eager_rule(
    mesh,
    *args,
    callback: Callable[..., Any],
    effect: debugging.DebugEffect,
    partitioned: bool,
):
  del effect
  with core.eval_context():
    all_blocks = zip(*map(list, args))
  for (idx, device), blocks in zip(np.ndenumerate(mesh.devices), all_blocks):
    callback(*blocks)
  return []


eager_rules[debugging.debug_callback_p] = _debug_callback_eager_rule

def _device_put_eager_rule(mesh, *xs, srcs, devices, copy_semantics):
  del mesh, srcs, copy_semantics
  for device in devices:
    if device is not None:
      raise ValueError("device_put with explicit device not allowed within "
                       f"shard_map-decorated functions, but got device {device}")
  return xs
eager_rules[dispatch.device_put_p] = _device_put_eager_rule

# Rewrite rules and static replication checking for efficient transposition

_rewrite_rules: dict[core.Primitive, Callable] = {}
register_rewrite = lambda prim: lambda r: _rewrite_rules.setdefault(prim, r)
register_standard_rewrite = lambda prim: \
    _rewrite_rules.setdefault(prim, partial(_standard_rewrite_rule, prim))
register_norewrite = lambda p: \
    _rewrite_rules.setdefault(p, partial(_no_rewrite, p, _check_rules[p]))

_check_rules: dict[core.Primitive, Callable] = {}
register_check = lambda prim: lambda rule: _check_rules.setdefault(prim, rule)
register_standard_check = \
    lambda prim: _check_rules.setdefault(prim, partial(_standard_check, prim))

def _eq_rep(mesh, r1, r2) -> bool:
  if r1 != r2 and r1 is None or r2 is None:
    r1, r2 = _remove_none_rep(mesh, r1), _remove_none_rep(mesh, r2)
  return r1 == r2

def _remove_none_rep(mesh, r):
  return set(mesh.axis_names) if r is None else r

def _no_rewrite(prim, rule, mesh, in_rep, *args, **params):
  out_vals = prim.bind(*args,**params)
  out_rep = rule(mesh, *in_rep, **params)
  if prim.multiple_results:
    out_rep_ = out_rep if type(out_rep) is list else [out_rep] * len(out_vals)
  else:
    out_vals, out_rep_ = [out_vals], [out_rep]
  return out_vals, out_rep_

def _standard_rewrite_rule(prim, mesh, in_rep, *args, **params):
  # The standard rewrite inserts pbroadcasts but doesn't change the primitive.
  out_rep_ = set.intersection(*in_rep) if in_rep else set(mesh.axis_names)
  args_ = [pvary(x, tuple(n for n in src if n not in out_rep_))
           if src - out_rep_ else x for x, src in zip(args, in_rep)]
  out_vals_ = prim.bind(*args_, **params)
  out_rep = [out_rep_] * len(out_vals_) if prim.multiple_results else [out_rep_]
  out_vals = [out_vals_] if not prim.multiple_results else out_vals_
  return out_vals, out_rep

def _standard_check(prim, mesh, *in_rep, **__):
  # The standard check require args' and outputs' replications to be the same,
  # except for Nones which correspond to constants.
  in_rep_ = [r for r in in_rep if r is not None]
  if in_rep_ and in_rep_[:-1] != in_rep_[1:]:
    raise Exception(f"Primitive {prim} requires argument replication types "
                    f"to match, but got {in_rep}. Please open an issue at "
                    "https://github.com/jax-ml/jax/issues and as a temporary "
                    "workaround pass the check_rep=False argument to shard_map")
  return in_rep_[0] if in_rep_ else None

def register_standard_collective(prim):
  register_check(prim)(partial(_standard_collective_check, prim))
  register_rewrite(prim)(partial(_standard_collective_rewrite, prim))

def register_reduction_collective(prim):
  register_check(prim)(partial(_reduction_collective_check, prim))
  register_rewrite(prim)(partial(_reduction_collective_rewrite, prim))

def _standard_collective_check(prim, mesh, x_rep, *, axis_name, **params):
  # The standard collective check is varying -> varying over axis_name.
  del mesh, params
  if x_rep is None or axis_name in x_rep:
    raise Exception(f"Collective {prim} must be applied to a device-varying "
                    f"replication type, but got {x_rep} for collective acting "
                    f"over axis name {axis_name}. Please open an issue at "
                    "https://github.com/jax-ml/jax/issues and as a temporary "
                    "workaround pass the check_rep=False argument to shard_map")
  return x_rep

def _standard_collective_rewrite(prim, mesh, in_rep, x, axis_name, **params):
  # The standard collective rewrite may insert a pbroadcast on the input.
  axis_name = (axis_name,) if not isinstance(axis_name, tuple) else axis_name
  x_rep, = in_rep
  axis_name_set = set(axis_name)
  if pbroadcast_axis_name := axis_name_set & x_rep:
    x = pvary(x, tuple(pbroadcast_axis_name))
  out_val = prim.bind(x, axis_name=axis_name, **params)
  return [out_val], [x_rep - axis_name_set]

def _reduction_collective_check(prim, mesh, x_rep, *, axes, **params):
  # The reduction collective check is varying -> replicated over axes.
  del mesh, params
  axes = (axes,) if not isinstance(axes, tuple) else axes
  if x_rep is None or any(a in x_rep for a in axes):
    raise Exception(f"Collective {prim} must be applied to a device-varying "
                    f"replication type, but got {x_rep} for collective acting "
                    f"over axis name {axes}. Please open an issue at "
                    "https://github.com/jax-ml/jax/issues and as a temporary "
                    "workaround pass the check_rep=False argument to shard_map")
  return x_rep | set(axes)

def _reduction_collective_rewrite(prim, mesh, in_rep, x, axes, **params):
  # The standard collective rewrite may insert a pbroadcast on the input.
  axes = (axes,) if not isinstance(axes, tuple) else axes
  x_rep, = in_rep
  axes_set = set(axes)
  if pbroadcast_axes := axes_set & x_rep:
    x = pvary(x, tuple(pbroadcast_axes))
  out_val, = prim.bind(x, axes=axes, **params)
  return [out_val], [x_rep | axes_set]


for o in it.chain(lax.__dict__.values(), slicing.__dict__.values(),
                  windowed_reductions.__dict__.values(),
                  special.__dict__.values(), convolution.__dict__.values(),
                  fft.__dict__.values(), linalg.__dict__.values(),
                  ops.__dict__.values(), ad_util.__dict__.values(),
                  prng.__dict__.values(), ann.__dict__.values(),
                  random.__dict__.values()):
  if isinstance(o, core.Primitive):
    register_standard_check(o)
    register_standard_rewrite(o)

for p in [control_flow.loops.cumsum_p, control_flow.loops.cumlogsumexp_p,
          control_flow.loops.cumprod_p, control_flow.loops.cummax_p,
          control_flow.loops.cummin_p, pjit.sharding_constraint_p,
          pjit.mesh_cast_p]:
  register_standard_check(p)
  register_standard_rewrite(p)


@register_check(lax_parallel.psum_p)
def _psum_check(_, *in_rep, axes, axis_index_groups):
  assert False  # should be rewritten away

@register_rewrite(lax_parallel.psum_p)
def _psum_rewrite(mesh, in_rep, *args, axes, axis_index_groups):
  # Replace the psum with psum2, insert pbroadcasts on input, replicated output.
  if axis_index_groups is not None: raise NotImplementedError
  axes = (axes,) if not isinstance(axes, tuple) else axes
  axes_ = set(axes)
  out_rep = [r | axes_ for r in in_rep]  # TODO determinism (and elsewhere)
  args_ = [pvary(x, tuple(n for n in mesh.axis_names if n in axes_ & src))
           for x, src in zip(args, in_rep)]
  out_val = lax_parallel.psum_invariant_p.bind(
      *args_, axes=axes, axis_index_groups=axis_index_groups)
  return out_val, out_rep


@register_check(lax_parallel.psum_invariant_p)
def _psum2_check(mesh, *in_rep, axes, axis_index_groups):
  assert type(axes) is tuple
  if any(set(axes) & r for r in in_rep if r is not None):
    raise Exception("Collective psum must be applied to a device-varying "
                    f"replication type, but got {in_rep} for collective acting "
                    f"over axis name {axes}. Please open an issue at "
                    "https://github.com/jax-ml/jax/issues, and as a temporary "
                    "workaround pass the check_rep=False argument to shard_map")
  in_rep = tuple(set(mesh.axis_names) if r is None else r for r in in_rep)
  return [r | set(axes) for r in in_rep]
register_norewrite(lax_parallel.psum_invariant_p)


@register_check(pvary_p)
def _pbroadcast_check(mesh, *in_rep, axes, axis_index_groups):
  assert type(axes) is tuple
  if not all(r is None or set(axes) & r for r in in_rep):
    raise Exception("Collective pbroadcast must be applied to a "
                    "non-device-varying "
                    f"replication type, but got {in_rep} for collective acting "
                    f"over axis name {axes}. Please open an issue at "
                    "https://github.com/jax-ml/jax/issues, and as a temporary "
                    "workaround pass the check_rep=False argument to shard_map")
  in_rep = tuple(set(mesh.axis_names) if r is None else r for r in in_rep)
  return [r - set(axes) for r in in_rep]
register_norewrite(pvary_p)


register_standard_collective(lax_parallel.all_gather_p)
register_standard_collective(lax_parallel.all_to_all_p)
register_standard_collective(lax_parallel.ppermute_p)
register_standard_collective(lax_parallel.reduce_scatter_p)
register_reduction_collective(lax_parallel.pmin_p)
register_reduction_collective(lax_parallel.pmax_p)


@register_check(lax_parallel.axis_index_p)
def _axis_index_check(mesh, *, axis_name):
  axis_name = (axis_name,) if not type(axis_name) is tuple else axis_name
  return set(mesh.shape) - set(axis_name)
register_norewrite(lax_parallel.axis_index_p)


@register_rewrite(pjit.pjit_p)
def _pjit_rewrite(mesh, in_rep, *args, jaxpr, **kwargs):
  jaxpr_, out_rep = _replication_rewrite_nomatch(mesh, jaxpr, in_rep)
  out_vals = pjit.pjit_p.bind(*args, jaxpr=jaxpr_, **kwargs)
  return out_vals, out_rep

@register_check(pjit.pjit_p)
def _pjit_check(mesh, *in_rep, jaxpr, **kwargs):
  return _check_rep(mesh, jaxpr.jaxpr, in_rep)


@register_rewrite(ad_checkpoint.remat_p)
def _remat_rewrite(mesh, in_rep, *args, jaxpr, **kwargs):
  jaxpr_ = pe.close_jaxpr(jaxpr)
  jaxpr_, out_rep = _replication_rewrite_nomatch(mesh, jaxpr_, in_rep)
  jaxpr, () = jaxpr_.jaxpr, jaxpr_.consts
  out_vals = ad_checkpoint.remat_p.bind(*args, jaxpr=jaxpr, **kwargs)
  return out_vals, out_rep

@register_check(ad_checkpoint.remat_p)
def _remat_check(mesh, *in_rep, jaxpr, **kwargs):
  return _check_rep(mesh, jaxpr, in_rep)


@register_check(core.call_p)
def _core_call_check(mesh, *in_rep, call_jaxpr, **kwargs):
  return _check_rep(mesh, call_jaxpr, in_rep)


@register_check(debugging.debug_callback_p)
def _debug_callback_rule(mesh, *in_rep, **_):
  return []
register_norewrite(debugging.debug_callback_p)


@register_check(callback.pure_callback_p)
def _pure_callback_rule(mesh, *_, result_avals, **__):
  return [set()] * len(result_avals)
register_norewrite(callback.pure_callback_p)


@register_check(callback.io_callback_p)
def _io_callback_rule(mesh, *_, result_avals, **__):
  return [set()] * len(result_avals)
register_norewrite(callback.io_callback_p)


@register_check(dispatch.device_put_p)
def _device_put_rule(mesh, *xs, **_):
  return list(xs)
register_norewrite(dispatch.device_put_p)


@register_check(ad.custom_lin_p)
def _custom_lin_rule(mesh, *_, out_avals, **__):
  return [set()] * len(out_avals)
register_norewrite(ad.custom_lin_p)


@register_check(control_flow.loops.scan_p)
def _scan_check(mesh, *in_rep, jaxpr, num_consts, num_carry, **_):
  _, carry_rep_in, _ = split_list(in_rep, [num_consts, num_carry])
  out_rep = _check_rep(mesh, jaxpr.jaxpr, in_rep)
  carry_rep_out, _ = split_list(out_rep, [num_carry])
  if not all(map(partial(_eq_rep, mesh), carry_rep_in, carry_rep_out)):
    raise Exception("Scan carry input and output got mismatched replication "
                    f"types {carry_rep_in} and {carry_rep_out}. Please open an "
                    "issue at https://github.com/jax-ml/jax/issues, and as a "
                    "temporary workaround pass the check_rep=False argument to "
                    "shard_map")
  return out_rep

@register_rewrite(control_flow.loops.scan_p)
def _scan_rewrite(mesh, in_rep, *args, jaxpr, num_consts, num_carry, **params):
  const_rep, carry_rep_in, xs_rep = split_list(in_rep, [num_consts, num_carry])
  for _ in range(1 + num_carry):
    in_rep_ = [*const_rep, *carry_rep_in, *xs_rep]
    _, out_rep = _replication_rewrite_nomatch(mesh, jaxpr, in_rep_)
    carry_rep_out, ys_rep = split_list(out_rep, [num_carry])
    carry_rep_out = map(op.and_, carry_rep_in, carry_rep_out)
    if carry_rep_in == carry_rep_out:
      break
    else:
      carry_rep_in = carry_rep_out
  else:
    assert False, 'Fixpoint not reached'

  args = [pvary(x, tuple(n for n in src if n not in dst))
          if src - dst else x for x, src, dst in zip(args, in_rep, in_rep_)]
  out_rep = [*carry_rep_out, *ys_rep]
  jaxpr_ = _replication_rewrite_match(mesh, jaxpr, in_rep_, out_rep)

  out_vals = control_flow.loops.scan_p.bind(
      *args, jaxpr=jaxpr_, num_consts=num_consts, num_carry=num_carry, **params)
  return out_vals, out_rep

@register_check(control_flow.loops.while_p)
def _while_check(mesh, *in_rep, body_jaxpr, cond_nconsts, body_nconsts, **_):
  _, bconst_rep, carry_rep_in = split_list(in_rep, [cond_nconsts, body_nconsts])
  carry_rep_out = _check_rep(mesh, body_jaxpr.jaxpr, [*bconst_rep, *carry_rep_in])
  if tuple(carry_rep_in) != tuple(carry_rep_out):
    raise Exception("while_loop carry input and output got mismatched "
                    f"replication types {carry_rep_in} and {carry_rep_out}. "
                    "Please open an issue at "
                    "https://github.com/jax-ml/jax/issues, and as a temporary "
                    "workaround pass the check_rep=False argument to shard_map")
  return carry_rep_out

@register_rewrite(control_flow.loops.while_p)
def _while_rewrite(mesh, in_rep, *args, cond_jaxpr, body_jaxpr, cond_nconsts,
                   body_nconsts):
  # while while isn't transposable, we insert pbroadcasts for consistent carry
  cconst_rep, bconst_rep, carry_rep_in = split_list(in_rep, [cond_nconsts, body_nconsts])
  num_carry = len(args) - cond_nconsts - body_nconsts
  for _ in range(1 + num_carry):
    in_rep_ = [*bconst_rep, *carry_rep_in]
    _, carry_rep_out = _replication_rewrite_nomatch(mesh, body_jaxpr, in_rep_)
    if tuple(carry_rep_in) == tuple(carry_rep_out):
      break
    carry_rep_in = map(op.and_, carry_rep_in, carry_rep_out)
  else:
    assert False, "Fixpoint not reached"

  cond_jaxpr_, _ = _replication_rewrite_nomatch(
      mesh, cond_jaxpr, (*cconst_rep, *carry_rep_in))
  body_jaxpr_ = _replication_rewrite_match(
      mesh, body_jaxpr, (*bconst_rep, *carry_rep_in), carry_rep_out)
  args_ = [pvary(x, tuple(n for n in src if n not in dst))
           if src - dst else x for x, src, dst in zip(args, in_rep, in_rep_)]
  out_vals = control_flow.loops.while_p.bind(
      *args_, cond_jaxpr=cond_jaxpr_, body_jaxpr=body_jaxpr_,
      cond_nconsts=cond_nconsts, body_nconsts=body_nconsts)
  return out_vals, carry_rep_out

@register_check(control_flow.conditionals.cond_p)
def _cond_rule(mesh, *in_rep, branches):
  _, *args_rep = in_rep
  out_rep = _check_rep(mesh, branches[0].jaxpr, args_rep)
  for branch in branches[1:]:
    out_rep_ = _check_rep(mesh, branch.jaxpr, args_rep)
    if not all(map(partial(_eq_rep, mesh), out_rep, out_rep_)):
      raise Exception("The branches of cond produced mismatched replication "
                      "types. Please open an issue at "
                      "https://github.com/jax-ml/jax/issues, and as a "
                      "temporary workaround pass the check_rep=False argument "
                      "to shard_map")
  return out_rep

@register_rewrite(control_flow.conditionals.cond_p)
def _cond_rewrite(mesh, in_rep, *args, branches):
  pred_rep, *args_rep = in_rep
  _, out_rep = _replication_rewrite_nomatch(mesh, branches[0], args_rep)
  for branch in branches[1:]:
    _, out_rep_ = _replication_rewrite_nomatch(mesh, branch, args_rep)
    if out_rep:
      out_rep = map(op.and_, out_rep, out_rep_)
    else:
      out_rep = out_rep_
  out_rep = map(partial(op.and_, pred_rep), out_rep)
  branches_ = tuple(_replication_rewrite_match(mesh, branch, args_rep, out_rep)
                    for branch in branches)
  out_vals = control_flow.conditionals.cond_p.bind(*args, branches=branches_)
  return out_vals, out_rep

@register_check(control_flow.conditionals.platform_index_p)
def _platform_index_rule(mesh, *_, **__):
  return set(mesh.axis_names)
register_norewrite(control_flow.conditionals.platform_index_p)

@register_rewrite(core.closed_call_p)
def _closed_call_rewrite(mesh, in_rep, *args, call_jaxpr, **kwargs):
  new_jaxpr, out_rep = _replication_rewrite_nomatch(mesh, call_jaxpr, in_rep)
  out_vals = core.closed_call_p.bind(*args, jaxpr=new_jaxpr, **kwargs)
  return out_vals, out_rep

@register_check(core.closed_call_p)
def _closed_call_check(mesh, *in_rep, call_jaxpr, **kwargs):
  return _check_rep(mesh, call_jaxpr.jaxpr, in_rep)


@register_check(cd.custom_jvp_call_p)
def _custom_jvp_call_check(mesh, *in_rep, call_jaxpr, jvp_jaxpr_fun,
                           num_consts, symbolic_zeros):
  return _check_rep(mesh, call_jaxpr.jaxpr, in_rep)

@register_rewrite(cd.custom_vjp_call_jaxpr_p)
def _custom_vjp_call_jaxpr_rewrite(
    mesh, in_rep, *args, fun_jaxpr, fwd_jaxpr_thunk, bwd, num_consts, out_trees,
    symbolic_zeros):
  if symbolic_zeros:
    msg = ("Please open an issue at https://github.com/jax-ml/jax/issues and as"
           " a temporary workaround pass the check_rep=False argument to "
           "shard_map")
    raise NotImplementedError(msg)

  fun_jaxpr_, out_rep = _replication_rewrite_nomatch(mesh, fun_jaxpr, in_rep)
  _, in_rep_ = split_list(in_rep, [num_consts])
  out_rep2 = []

  @pe._memoize
  def fwd_jaxpr_thunk_(*zeros):
    fwd_jaxpr = core.ClosedJaxpr(*fwd_jaxpr_thunk(*zeros))
    fwd_jaxpr_, out_rep = _replication_rewrite_nomatch(mesh, fwd_jaxpr, in_rep_)
    out_rep2.append(out_rep)
    return fwd_jaxpr_.jaxpr, fwd_jaxpr_.consts

  bwd_ = _rewrite_bwd(bwd, mesh, lambda: out_rep2[0], in_rep_)

  outs = cd.custom_vjp_call_jaxpr_p.bind(
      *args, fun_jaxpr=fun_jaxpr_, fwd_jaxpr_thunk=fwd_jaxpr_thunk_, bwd=bwd_,
      num_consts=num_consts, out_trees=out_trees, symbolic_zeros=symbolic_zeros)
  out_rep = out_rep2[0] if out_rep2 else out_rep
  return outs, out_rep

@register_check(cd.custom_vjp_call_jaxpr_p)
def _custom_vjp_call_jaxpr_check(mesh, *in_rep, fun_jaxpr, **_):
  return _check_rep(mesh, fun_jaxpr.jaxpr, in_rep)

@register_check(control_flow.solves.linear_solve_p)
def _linear_solve_check(mesh, *in_rep, jaxprs, **_):
  out_rep = _standard_check(control_flow.solves.linear_solve_p, mesh, *in_rep)
  return [out_rep] * len(jaxprs.solve.out_avals)
register_standard_rewrite(control_flow.solves.linear_solve_p)

@register_check(ffi.ffi_call_p)
def _ffi_call_check(mesh, *in_rep, result_avals, **_):
  out_rep = _standard_check(ffi.ffi_call_p, mesh, *in_rep)
  return [out_rep] * len(result_avals)
register_standard_rewrite(ffi.ffi_call_p)

del _check_rules[lax.tie_p]

@register_check(lax.tie_p)
def _tie_check(mesh, x_rep, y_rep):
  return x_rep
register_norewrite(lax.tie_p)


# Batching

def _shard_map_batch(
    trace: batching.BatchTrace, prim: core.Primitive, fun: lu.WrappedFun,
    in_tracers: Sequence[batching.BatchTracer], mesh: Mesh,
    in_names: tuple[AxisNames, ...],
    out_names_thunk: Callable[[], tuple[AxisNames, ...]],
    check_rep: bool,
    rewrite: bool,
    auto: frozenset) -> Sequence[batching.BatchTracer]:
  in_vals, in_dims = unzip2(map(trace.to_batch_info, in_tracers))
  if any(isinstance(d, batching.RaggedAxis) for d in in_dims):
    raise NotImplementedError
  new_in_names = [{ax + (d is not batching.not_mapped and d <= ax): names[ax]
                   for ax in names} for names, d in zip(in_names, in_dims)]
  spmd_axis_name = trace.axis_data.spmd_name
  if spmd_axis_name is not None:
    used = {n for names in in_names for ns in names.values() for n in ns}
    if not config.disable_vmap_shmap_error.value and set(spmd_axis_name) & used:
      raise ValueError("vmap spmd_axis_name cannot appear in shard_map in_specs")
    new_in_names = [{**ns, d:spmd_axis_name} if d is not batching.not_mapped
                    else ns for ns, d in zip(new_in_names, in_dims)]
    new_size = trace.axis_data.size // prod(mesh.shape[n] for n in spmd_axis_name)
    new_axis_data = batching.AxisData(trace.axis_data.name, new_size,
                                      trace.axis_data.spmd_name, None)
  else:
    new_axis_data = trace.axis_data
  fun, out_dims = batching.batch_subtrace(fun, trace.tag, new_axis_data, tuple(in_dims))
  @as_hashable_function(closure=out_names_thunk)
  def new_out_names_thunk():
    return _batch_out_names(spmd_axis_name, out_dims(), out_names_thunk())

  new_params = dict(mesh=mesh, in_names=new_in_names,
                    out_names_thunk=new_out_names_thunk, check_rep=check_rep,
                    rewrite=rewrite, auto=auto)
  with core.set_current_trace(trace.parent_trace):
    out_vals = prim.bind(fun, *in_vals, **new_params)
  make_tracer = partial(batching.BatchTracer, trace,
                        source_info=source_info_util.current())
  return map(make_tracer, out_vals, out_dims())
batching.BatchTrace.process_shard_map = _shard_map_batch

def _batch_out_names(spmd_axis_name, dims, out_names):
  out_names_ = [{ax + (d is not batching.not_mapped and d <= ax): names[ax]
                  for ax in names} for names, d in zip(out_names, dims)]
  if spmd_axis_name is not None:
    used = {n for names in out_names for ns in names.values() for n in ns}
    if not config.disable_vmap_shmap_error.value and set(spmd_axis_name) & used:
      raise ValueError("vmap spmd_axis_name cannot appear in shard_map out_specs")
    out_names_ = [{**ns, d:spmd_axis_name} if d is not batching.not_mapped
                  else ns for ns, d in zip(out_names_, dims)]
  return out_names_


# Autodiff

def _shard_map_jvp(trace, shard_map_p, f, tracers, mesh, in_names,
                   out_names_thunk, check_rep, rewrite, auto):
  primals, tangents = unzip2(map(trace.to_primal_tangent_pair, tracers))
  which_nz = [     type(t) is not ad.Zero           for t in tangents]
  tangents = [t if type(t) is not ad.Zero else None for t in tangents]
  args, in_tree = tree_flatten((primals, tangents))
  f_jvp = ad.jvp_subtrace(f, trace.tag)
  f_jvp, which_nz_out = ad.nonzero_tangent_outputs(f_jvp)
  tangent_in_names = [ax for ax, nz in zip(in_names, which_nz) if nz]

  @as_hashable_function(closure=out_names_thunk)
  def new_out_names_thunk():
    out_ax = out_names_thunk()
    return (*out_ax, *(ax for ax, nz in zip(out_ax, which_nz_out()) if nz))
  params = dict(mesh=mesh, in_names=(*in_names, *tangent_in_names),
                out_names_thunk=new_out_names_thunk, check_rep=check_rep,
                rewrite=rewrite, auto=auto)
  f_jvp, out_tree = ad.traceable(f_jvp, in_tree)
  result = shard_map_p.bind_with_trace(trace.parent_trace, (f_jvp,) + tuple(args), params)
  primal_out, tangent_out = tree_unflatten(out_tree(), result)
  tangent_out = [ad.Zero(core.get_aval(p).to_tangent_aval()) if t is None else t
                 for p, t in zip(primal_out, tangent_out)]
  return [ad.JVPTracer(trace, p, t) for p, t in zip(primal_out, tangent_out)]
ad.JVPTrace.process_shard_map = _shard_map_jvp

def _shard_map_partial_eval(trace: pe.JaxprTrace, shard_map_p,
                            f: lu.WrappedFun, tracers, mesh, in_names,
                            out_names_thunk, check_rep, rewrite, auto):
  tracers = map(trace.to_jaxpr_tracer, tracers)
  in_pvals = [t.pval for t in tracers]
  in_knowns, in_avals, in_consts = pe.partition_pvals(in_pvals)
  unk_in_names, known_in_names = pe.partition_list(in_knowns, in_names)
  in_avals_sharded = map(partial(_shard_aval, mesh, auto, check_rep),
                         unk_in_names, in_avals)
  f = pe.trace_to_subjaxpr_nounits_fwd2(f, trace.tag, f.debug_info, False)
  f = _promote_scalar_residuals(f)
  f_known, aux = pe.partial_eval_wrapper_nounits2(
      f, (*in_knowns,), (*in_avals_sharded,))
  all_names = _all_newly_manual_mesh_names(mesh, auto, trace)

  @as_hashable_function(closure=out_names_thunk)
  def known_out_names():
    _, _, out_knowns, res_avals, _, _ = aux()
    _, out_known_names = pe.partition_list(out_knowns, out_names_thunk())
    if check_rep and config.varying_axes_in_types.value:
      res_names = [{0: tuple(i for i in mesh.axis_names if i in a.vma)}
                  for a in res_avals]
    else:
      res_names = [{0: all_names}] * len(res_avals)
    return (*out_known_names, *res_names)

  known_params = dict(mesh=mesh, in_names=(*known_in_names,),
                      out_names_thunk=known_out_names, check_rep=check_rep,
                      rewrite=rewrite, auto=auto)
  out = shard_map_p.bind_with_trace(trace.parent_trace, (f_known, *in_consts),
                                    known_params)
  in_fwd, out_fwd, out_knowns, res_avals, jaxpr, env = aux()
  num_res = sum(f1 is None and f2 is None for f1, f2 in zip(in_fwd, out_fwd))
  out_consts, non_fwd_res = split_list(out, [len(out) - num_res])
  assert not jaxpr.constvars
  unk_out_names, _ = pe.partition_list(out_knowns, out_names_thunk())
  known_out_names_ = known_out_names()
  res = subs_list2(in_fwd, out_fwd, in_consts, out_consts, non_fwd_res)
  # TODO make res_avals be the full set, not just the non-fwd ones
  res_avals_iter = iter(res_avals)
  res_names = []
  for f1, f2 in zip(in_fwd, out_fwd):
    if f1 is not None:
      res_names.append(known_in_names[f1])
    elif f2 is not None:
      res_names.append(known_out_names_[f2])
    else:
      if check_rep and config.varying_axes_in_types.value:
        res_vma = next(res_avals_iter).vma
        res_names.append({0: tuple(n for n in mesh.axis_names if n in res_vma)})
      else:
        res_names.append({0: all_names})
  unk_in_names = (*res_names,) + ({},) * len(env) + (*unk_in_names,)  # type: ignore[assignment]
  const_tracers = map(trace.new_instantiated_const, res)
  env_tracers = map(trace.to_jaxpr_tracer, env)
  unk_arg_tracers = [t for t in tracers if not t.is_known()]
  out_avals_sharded = [v.aval for v in jaxpr.outvars]
  unk_params = dict(mesh=mesh, in_names=unk_in_names,
                    out_names=unk_out_names, jaxpr=jaxpr,
                    check_rep=(check_rep if config.varying_axes_in_types.value
                               else False),
                    rewrite=rewrite, auto=auto)
  out_avals = map(partial(_unshard_aval, mesh, check_rep), unk_out_names,
                  out_avals_sharded)
  out_tracers = [pe.JaxprTracer(trace, pe.PartialVal.unknown(a), None)
                 for a in out_avals]
  effs = core.filter_named_axis_effects(jaxpr.effects, mesh.axis_names)
  eqn = pe.new_eqn_recipe((*const_tracers, *env_tracers, *unk_arg_tracers),
                          out_tracers, shard_map_p, unk_params,
                          effs, source_info_util.current())
  for t in out_tracers: t.recipe = eqn
  return merge_lists(out_knowns, out_tracers, out_consts)
pe.JaxprTrace.process_shard_map = _shard_map_partial_eval

def _shard_map_linearize(trace, shard_map_p, f: lu.WrappedFun,
                         tracers, mesh, in_names,
                         out_names_thunk, check_rep, rewrite, auto):
  primals, tangents = unzip2(map(trace.to_primal_tangent_pair, tracers))
  nzs_in = tuple(type(t) is not ad.Zero for t in tangents)
  f_primal, linearize_outs_thunk = ad.linearize_subtrace(f, trace.tag, nzs_in, f.debug_info)
  f_primal = _promote_scalar_residuals_lin(f_primal, linearize_outs_thunk)
  tangent_in_names = [ax for ax, nz in zip(in_names, nzs_in) if nz]
  res_names = _all_newly_manual_mesh_names(mesh, auto, trace)

  @as_hashable_function(closure=linearize_outs_thunk)
  def fwd_out_names_thunk():
    _, _, _, _, in_fwd, out_fwd = linearize_outs_thunk()
    out_names = out_names_thunk()
    num_res_out = sum(f1 is None and f2 is None for f1, f2 in zip(in_fwd, out_fwd))
    # This is incorrect so we set `check_rep=False` in the tangent (as in JVP).
    return (*({0: res_names} for _ in range(num_res_out)), *out_names)
  fwd_params = dict(
      mesh=mesh, in_names=in_names,
      out_names_thunk=fwd_out_names_thunk, check_rep=check_rep,
      rewrite=rewrite, auto=auto)
  all_fwd_results = shard_map_p.bind_with_trace(
      trace.parent_trace, (f_primal, *primals), fwd_params)
  residual_avals, nzs_out, lin_jaxpr, env, in_fwd, out_fwd = linearize_outs_thunk()
  num_res_out = sum(f1 is None and f2 is None for f1, f2 in zip(in_fwd, out_fwd))
  non_fwd_res = all_fwd_results[:num_res_out]
  primals_out = all_fwd_results[num_res_out:]
  residuals = subs_list2(in_fwd, out_fwd, primals, primals_out, non_fwd_res)
  args_to_promote = [getattr(aval, 'shape', ()) == () and f1 is None and f2 is None
                     for aval, f1, f2 in zip(residual_avals, in_fwd, out_fwd)]
  with (_extend_axis_env(mesh, auto),
        use_abstract_mesh(_as_manual_mesh(mesh, auto)),
        config._check_rep(check_rep)):
    lin_jaxpr = _promote_scalar_residuals_jaxpr(lin_jaxpr, args_to_promote)
  out_names = out_names_thunk()
  residual_names = [in_names[f1] if f1 is not None else
                    out_names[f2] if f2 is not None else
                    {0: res_names} for f1, f2 in zip(in_fwd, out_fwd)]
  new_in_names = (*residual_names, *({} for _ in range(len(env))),
                  *(ax for ax, nz in zip(in_names, nzs_in) if nz))
  tangent_out_names = tuple(ax for ax, nz in zip(out_names_thunk(), nzs_out) if nz)
  @as_hashable_function(closure=tangent_out_names)
  def tangent_out_names_thunk():
    return tangent_out_names
  tangent_params = dict(
      mesh=mesh, in_names=new_in_names, out_names_thunk=tangent_out_names_thunk,
      check_rep=False, rewrite=rewrite, auto=auto)

  # TODO(mattjj): avoid round-tripping the jaxpr through eval_jaxpr here
  def f_tangent(*args):
    return core.eval_jaxpr(lin_jaxpr, (), *args)

  nz_tangents_in = [t for (t, nz) in zip(tangents, nzs_in) if nz]
  nz_tangents_out = shard_map_p.bind_with_trace(
      trace.tangent_trace,
      (lu.wrap_init(f_tangent, debug_info=lin_jaxpr.debug_info),
       *residuals, *env, *nz_tangents_in), tangent_params)
  nz_tangents_out_iter = iter(nz_tangents_out)
  tangents_out = [next(nz_tangents_out_iter) if nz else ad.Zero.from_primal_value(primal)
                  for nz, primal in zip(nzs_out, primals_out)]
  return map(partial(ad.maybe_linearize_tracer, trace), primals_out, nzs_out, tangents_out)
ad.LinearizeTrace.process_shard_map = _shard_map_linearize

@lu.transformation2
def _promote_scalar_residuals_lin(f, linearize_outs_thunk, *args, **kwargs):
  ans = f(*args, **kwargs)
  _, _, _, _, in_fwd, out_fwd = linearize_outs_thunk()
  num_res_out = sum(f1 is None and f2 is None for f1, f2 in zip(in_fwd, out_fwd))
  residuals = ans[:num_res_out]
  primals = ans[num_res_out:]
  residuals = [jax.lax.broadcast(x, (1,)) if not getattr(x, 'shape', ()) else x
               for x in residuals]
  return *residuals, *primals

@lu.transformation2
def _promote_scalar_residuals(f: Callable, *args, **kwargs):
  jaxpr, (in_fwds, out_fwds, out_pvals, out_consts, env) = f(*args, **kwargs)
  which = [f1 is None and f2 is None and not v.aval.shape
           for f1, f2, v in zip(in_fwds, out_fwds, jaxpr.constvars)]
  jaxpr = _promote_scalar_residuals_jaxpr(jaxpr, which)
  out_consts = [jax.lax.broadcast(x, (1,)) if not getattr(x, 'shape', ()) else x
                for x in out_consts]
  return jaxpr, (in_fwds, out_fwds, out_pvals, out_consts, env)

def _promote_scalar_residuals_jaxpr(jaxpr: core.Jaxpr, which: Sequence[bool]):
  def fun(*res_and_args):
    res, args = split_list(res_and_args, [len(jaxpr.constvars)])
    res = [_rem_singleton(x) if w else x for x, w in zip(res, which)]
    return core.eval_jaxpr(jaxpr, res, *args)
  res_avals = [core.unmapped_aval(1, 0, v.aval) if w else v.aval
               for v, w in zip(jaxpr.constvars, which)]
  in_avals = [*res_avals, *[v.aval for v in jaxpr.invars]]
  jaxpr, _, _, () = pe.trace_to_jaxpr_dynamic(
      lu.wrap_init(fun, debug_info=jaxpr.debug_info), in_avals)
  return jaxpr


def _unmentioned2(mesh: Mesh, names: AxisNames,
                  auto: frozenset[AxisName]) -> list[AxisName]:
  # We use a filtered-down version of unmentioned to avoid defensive-psum over
  # more chips than required in the transpose-no-check-rep case.
  name_set = {n for ns in names.values() for n in ns} | auto
  return [n for n in _all_mesh_names_except_spmd(mesh, auto)
          if n not in name_set]


def _shard_map_transpose(out_cts, *args,
                         jaxpr: core.Jaxpr, mesh, in_names, out_names,
                         check_rep, rewrite, auto):
  mb_div = lambda x, y: x / y if y != 1 else x
  out_cts = [
      ad.Zero(_shard_aval(mesh, auto, check_rep, ns, x.aval))
      if type(x) is ad.Zero else x if rewrite or dtypes.dtype(x) == dtypes.float0
      else mb_div(x, prod(map(mesh.shape.get, _unmentioned2(mesh, ns, auto))))
      for ns, x in zip(out_names, out_cts)
  ]
  args = tuple(x if type(x) is not ad.UndefinedPrimal else
               ad.UndefinedPrimal(_shard_aval(mesh, auto, check_rep, ns, x.aval))
               for ns, x in zip(in_names, args))
  all_args, in_tree = tree_flatten((out_cts, args))

  def fun_trans_callable(out_cts, args):
    # TODO(mattjj): when #26811 lands, delete this and just run backward_pass
    in_undef = map(ad.is_undefined_primal, args)
    res, undefs = partition_list(in_undef, args)
    jaxpr_known, jaxpr_unknown, _, _ = pe.partial_eval_jaxpr_nounits(
        pe.close_jaxpr(jaxpr), in_undef, False)
    res_reshaped = core.jaxpr_as_fun(jaxpr_known)(*res)
    in_cts = ad.backward_pass(
        jaxpr_unknown.jaxpr, False, (), (*res_reshaped, *undefs), out_cts
    )[len(res_reshaped):]
    _, in_ct_names = partition_list(in_undef, in_names)
    in_cts = [ad.Zero(_unshard_aval(mesh, check_rep, ns, x.aval))
              if type(x) is ad.Zero else x if rewrite
              else jax.lax.psum(x, tuple(_unmentioned2(mesh, ns, auto)))
              for ns, x in zip(in_ct_names, in_cts)]
    res_zeros = [ad_util.zero_from_primal(r) for r in res]
    return merge_lists(in_undef, res_zeros, in_cts)

  fun_trans = lu.wrap_init(fun_trans_callable, debug_info=jaxpr.debug_info)
  fun_trans, nz_arg_cts = ad.nonzero_outputs(fun_trans)
  fun_trans_flat, out_tree = api_util.flatten_fun_nokwargs(fun_trans, in_tree)

  new_in_names = \
      [n for n, x in zip(out_names, out_cts) if type(x) is not ad.Zero] + \
      [n for n, x in zip(in_names, args) if type(x) is not ad.UndefinedPrimal]

  def new_out_names_thunk():
    return tuple(names for names, nz in zip(in_names, nz_arg_cts()) if nz)

  try:
    out_flat = shard_map_p.bind(
        fun_trans_flat, *all_args, mesh=mesh, in_names=tuple(new_in_names),
        out_names_thunk=new_out_names_thunk, check_rep=check_rep, rewrite=rewrite,
        auto=auto)
  except (FloatingPointError, ZeroDivisionError) as e:
    print("Invalid nan value encountered in the backward pass of a shard_map "
          "function. Calling the de-optimized backward pass.")
    try:
      # TODO(mattjj): Remove this and do `fun_trans.call_wrapped(out_cts, args)`
      # in eager mode so that output of shmap are not manual.
      with jax.disable_jit(True):
        _ = shard_map_p.bind(
            fun_trans_flat, *all_args, mesh=mesh, in_names=tuple(new_in_names),
            out_names_thunk=new_out_names_thunk, check_rep=check_rep,
            rewrite=rewrite, auto=auto)
    except (FloatingPointError, ZeroDivisionError) as e2:
      raise e2 from None
    else:
      dispatch._raise_no_nan_in_deoptimized(e)
  return tree_unflatten(out_tree(), out_flat)
ad.primitive_transposes[shard_map_p] = _shard_map_transpose

# Remat

def _partial_eval_jaxpr_custom_rule(
    saveable: Callable[..., pe.RematCases_], unks_in: Sequence[bool],
    inst_in: Sequence[bool], eqn: core.JaxprEqn
) -> tuple[core.JaxprEqn, core.JaxprEqn, Sequence[bool], Sequence[bool],
           list[core.Var]]:
  jaxpr, mesh = eqn.params['jaxpr'], eqn.params['mesh']
  check_rep, auto = eqn.params['check_rep'], eqn.params['auto']
  with _extend_axis_env(mesh, auto), config._check_rep(check_rep):
    jaxpr_known, jaxpr_staged, unks_out, inst_out, num_res = \
        pe.partial_eval_jaxpr_custom(jaxpr, unks_in, inst_in, False, False, saveable)
  num_out_primals = len(jaxpr_known.outvars) - num_res
  in_fwd = pe._jaxpr_forwarding(jaxpr_known)[num_out_primals:]
  out_vars, res_vars = split_list(jaxpr_known.outvars, [num_out_primals])
  idx_map = {id(v): i for i, v in enumerate(out_vars)}
  out_fwd = [idx_map.get(id(v)) for v in res_vars]
  which = [f1 is None and f2 is None for f1, f2 in zip(in_fwd, out_fwd)]
  mesh = eqn.params['mesh']
  with (_extend_axis_env(mesh, auto),
        use_abstract_mesh(_as_manual_mesh(mesh, auto)),
        config._check_rep(check_rep)):
    jaxpr_known = pe.prune_jaxpr_outputs(jaxpr_known, [True] * num_out_primals + which)
    jaxpr_known, jaxpr_staged = _add_reshapes(which, jaxpr_known, jaxpr_staged)
  jaxpr_known = core.remove_named_axis_effects(jaxpr_known, mesh.axis_names)
  jaxpr_staged = core.remove_named_axis_effects(jaxpr_staged, mesh.axis_names)
  ins_known, _ = partition_list(unks_in, eqn.invars)
  out_binders_known, _ = partition_list(unks_out, eqn.outvars)
  _, ins_staged = partition_list(inst_in, eqn.invars)
  _, out_binders_staged = partition_list(inst_out, eqn.outvars)
  newvar = core.gensym()
  residuals, staged_in_res_names = [], []
  for var, w in zip(jaxpr_staged.invars[:num_res], which):
    if w:
      rn = ({0: tuple(i for i in mesh.axis_names if i in var.aval.vma)}  # type: ignore
            if check_rep and config.varying_axes_in_types.value
            else {0: _all_newly_manual_mesh_names(mesh, auto)})
      residuals.append(newvar(_unshard_aval(mesh, check_rep, rn, var.aval)))
      staged_in_res_names.append(rn)
  if check_rep and config.varying_axes_in_types.value:
    out_res_names_known = [
        {0: tuple(i for i in mesh.axis_names if i in var.aval.vma)}
        for var, o in zip(res_vars, out_fwd) if o is None
    ]
  else:
    out_res_names_known = [{0: _all_newly_manual_mesh_names(mesh, auto)}] * sum(which)
  params_known, params_staged = _pe_custom_params(
      unks_in, inst_in, map(op.not_, unks_out), inst_out, in_fwd, out_fwd,
      out_res_names_known, staged_in_res_names,
      dict(eqn.params, jaxpr=jaxpr_known), dict(eqn.params, jaxpr=jaxpr_staged))
  eqn_known = pe.new_jaxpr_eqn(ins_known, [*out_binders_known, *residuals],
                               eqn.primitive, params_known, jaxpr_known.effects,
                               eqn.source_info, eqn.ctx)
  full_res = subs_list2(in_fwd, out_fwd, ins_known, out_binders_known, residuals)
  eqn_staged = pe.new_jaxpr_eqn([*full_res, *ins_staged], out_binders_staged,
                                eqn.primitive, params_staged,
                                jaxpr_staged.effects, eqn.source_info, eqn.ctx)
  assert len(eqn_staged.invars) == len(jaxpr_staged.invars)
  new_inst = [x for x, inst in zip(eqn.invars, inst_in)
              if type(x) is core.Var and not inst]
  new_inst += [out_binders_known[f] for f in {i for i in out_fwd if i is not None}]
  return eqn_known, eqn_staged, unks_out, inst_out, new_inst + residuals
pe.partial_eval_jaxpr_custom_rules[shard_map_p] = \
    _partial_eval_jaxpr_custom_rule

def _add_reshapes(which: Sequence[bool],
                  jaxpr_known: core.Jaxpr,
                  jaxpr_staged: core.Jaxpr) -> tuple[core.Jaxpr, core.Jaxpr]:
  # add singleton axes to residuals which are from jaxpr_known and are scalars
  which_ = [w and not v.aval.shape  # pytype: disable=attribute-error
            for w, v in zip(which, jaxpr_staged.invars[:len(which)])]
  if not any(which_): return jaxpr_known, jaxpr_staged
  assert not jaxpr_known.constvars and not jaxpr_staged.constvars

  def known(*args):
    out = core.eval_jaxpr(jaxpr_known, (), *args)
    out_known, res = split_list(out, [len(out) - sum(which)])
    res = [_add_singleton(x) if not x.shape else x for x in res]
    return [*out_known, *res]
  avals_in = [v.aval for v in jaxpr_known.invars]
  jaxpr_known, _, (), () = pe.trace_to_jaxpr_dynamic(
      lu.wrap_init(known, debug_info=jaxpr_known.debug_info), avals_in)

  def staged(*args):
    res_, ins = split_list(args, [len(which)])
    res = [_rem_singleton(x) if w else x for x, w in zip(res_, which_)]
    return core.eval_jaxpr(jaxpr_staged, (), *res, *ins)
  res_avals = [core.unmapped_aval(1, 0, v.aval) if w else v.aval
               for w, v in zip(which_, jaxpr_staged.invars[:len(which)])]
  avals_in = [*res_avals, *[v.aval for v in jaxpr_staged.invars[len(which):]]]
  jaxpr_staged, _, (), () = pe.trace_to_jaxpr_dynamic(
      lu.wrap_init(staged, debug_info=jaxpr_staged.debug_info), avals_in)

  return jaxpr_known, jaxpr_staged

def _pe_custom_params(unks_in, inst_in, kept_outs_known, kept_outs_staged,
                      in_fwd, out_fwd, out_res_names_known, staged_in_res_names,
                      params_known, params_staged):
  # prune inputs to jaxpr_known according to unks_in
  in_names_known, _ = partition_list(unks_in, params_known['in_names'])
  _, out_names_known = partition_list(kept_outs_known, params_known['out_names'])
  out_names_known = out_names_known + out_res_names_known
  assert len(out_names_known) == len(params_known['jaxpr'].outvars)
  new_params_known = dict(params_known, in_names=tuple(in_names_known),
                          out_names=tuple(out_names_known))

  # added num_res new inputs to jaxpr_staged, pruning according to inst_in
  _, in_names_staged = partition_list(inst_in, params_staged['in_names'])
  iter_staged = iter(staged_in_res_names)
  res_names = [in_names_known[f1] if f1 is not None else
               out_names_known[f2] if f2 is not None else
               next(iter_staged) for f1, f2 in zip(in_fwd, out_fwd)]

  in_names_staged = res_names + in_names_staged
  _, out_names_staged = partition_list(kept_outs_staged, params_staged['out_names'])
  new_params_staged = dict(params_staged, in_names=tuple(in_names_staged),
                           out_names=tuple(out_names_staged))
  if not config.varying_axes_in_types.value:
    new_params_staged.update(check_rep=False)
  return new_params_known, new_params_staged

# TODO(mattjj): remove this mechanism when we revise mesh scopes
def _all_mesh_names_except_spmd(
    mesh: Mesh, auto: frozenset[AxisName], trace=None
) -> tuple[AxisName, ...]:
  axis_env = core.get_axis_env()
  spmd_names = axis_env.spmd_axis_names
  return tuple(name for name in mesh.axis_names if name not in spmd_names and
               name not in auto)

def _all_newly_manual_mesh_names(
    mesh: Mesh, auto: frozenset[AxisName], trace=None
) -> tuple[AxisName, ...]:
  axis_env = core.get_axis_env()
  vmap_spmd_names = set(axis_env.spmd_axis_names)
  if not (ctx_mesh := get_abstract_mesh()).empty:
    mesh = ctx_mesh
    already_manual_names = set(ctx_mesh.manual_axes)
  else:
    # TODO(mattjj): remove this mechanism when we revise mesh scopes
    already_manual_names = set(axis_env.axis_sizes)  # may include vmap axis_names
  return tuple(name for name in mesh.axis_names
               if name not in auto | vmap_spmd_names | already_manual_names)


# DCE

# TODO(mattjj): de-duplicate with pe.dce_jaxpr_call_rule, and/or _pmap_dce_rule?
def _shard_map_dce(used_outputs: list[bool], eqn: core.JaxprEqn
                   ) -> tuple[list[bool], core.JaxprEqn | None]:
  if not any(used_outputs) and not pe.has_effects(eqn):
    return [False] * len(eqn.invars), None
  mesh = eqn.params["mesh"]
  auto = eqn.params["auto"]
  check_rep = eqn.params["check_rep"]
  with _extend_axis_env(mesh, auto), config._check_rep(check_rep):
    jaxpr, used_inputs = pe.dce_jaxpr(eqn.params['jaxpr'], used_outputs)
  if not any(used_inputs) and not any(used_outputs) and not jaxpr.effects:
    return used_inputs, None
  else:
    _, in_names = partition_list(used_inputs, eqn.params['in_names'])
    _, out_names = partition_list(used_outputs, eqn.params['out_names'])
    new_params = dict(eqn.params, jaxpr=jaxpr, in_names=tuple(in_names),
                      out_names=tuple(out_names))
    effs = core.filter_named_axis_effects(jaxpr.effects, mesh.axis_names)
    new_eqn = pe.new_jaxpr_eqn(
        [v for v, used in zip(eqn.invars, used_inputs) if used],
        [x for x, used in zip(eqn.outvars, used_outputs) if used],
        eqn.primitive, new_params, effs, eqn.source_info, eqn.ctx)
    return used_inputs, new_eqn
pe.dce_rules[shard_map_p] = _shard_map_dce

# Implementing pmap in terms of shard_map

def pmap(f, axis_name=None, *, in_axes=0, out_axes=0,
         static_broadcasted_argnums=(), devices=None, backend=None,
         axis_size=None, donate_argnums=(), global_arg_shapes=None):
  devices = tuple(devices) if devices is not None else devices
  axis_name, static_broadcasted_tuple, donate_tuple = _shared_code_pmap(
      f, axis_name, static_broadcasted_argnums, donate_argnums, in_axes, out_axes)

  def infer_params(*args, **kwargs):
    p = _prepare_pmap(f, in_axes, out_axes, static_broadcasted_tuple,
                      donate_tuple, devices, backend, axis_size, args, kwargs)
    for arg in p.flat_args:
      dispatch.check_arg(arg)
    mesh = Mesh(_get_devices(p, backend), (axis_name,))
    _pmapped, in_specs, out_specs = _cached_shard_map(
        p.flat_fun, mesh, p.in_axes_flat, p.out_axes_thunk, axis_name)
    flat_global_args = host_local_array_to_global_array(
        p.flat_args, mesh, list(in_specs))
    jitted_f = jax.jit(
        _pmapped,
        donate_argnums=(i for i, val in enumerate(p.donated_invars) if val))
    return jitted_f, flat_global_args, p.out_tree, mesh, out_specs

  def wrapped(*args, **kwargs):
    (jitted_f, flat_global_args, out_tree, mesh,
     out_specs) = infer_params(*args, **kwargs)
    outs = jitted_f(*flat_global_args)
    outs = global_array_to_host_local_array(outs, mesh, out_specs())
    return tree_unflatten(out_tree(), outs)

  def lower(*args, **kwargs):
    jitted_f, _, _, _, _ = infer_params(*args, **kwargs)
    return jitted_f.lower(*args, **kwargs)
  wrapped.lower = lower

  return wrapped


@lu.cache
def _cached_shard_map(flat_fun, mesh, in_axes_flat, out_axes_thunk, axis_name):
  in_specs = tuple(map(partial(_axis_to_spec, axis_name), in_axes_flat))
  out_specs = lambda: map(partial(_axis_to_spec, axis_name), out_axes_thunk())
  fun = _handle_reshapes(flat_fun, in_axes_flat, out_axes_thunk)
  return (_shard_map(fun.call_wrapped, mesh, in_specs, out_specs,
                     check_rep=False, auto=frozenset()),
          in_specs, out_specs)

@lu.transformation2
def _handle_reshapes(f, in_axes, out_axes_thunk, *args, **kwargs):
  args = tree_map(lambda x, ax: x if ax is None else jnp.squeeze(x, axis=ax),
                  list(args), list(in_axes))
  out = f(*args)
  return tree_map(lambda x, ax: x if ax is None else jnp.expand_dims(x, axis=ax),
                  list(out), list(out_axes_thunk()))

def _axis_to_spec(axis_name, ax):
  if isinstance(ax, int):
    specs = [None] * ax + [axis_name]
    return P(*specs)
  elif ax is None:
    return P()
  else:
    raise TypeError(ax)

def _get_devices(p, backend):
  if backend is not None and p.devices is None:
    devs = jax.devices(backend=backend)
  else:
    devs = jax.devices() if p.devices is None else p.devices
  if jax.process_count() > 1:
    return devs[:p.global_axis_size]
  return devs[:p.local_axis_size]

@lu.transformation2
def _implicit_pvary_on_output(f, out_names_thunk, *args, **kwargs):
  out_flat = f(*args, **kwargs)
  return [pvary(o, tuple(_names_to_vma(n) - typeof(o).vma))
          for o, n in zip(out_flat, out_names_thunk())]

### Rewrite!

Val = Any

class RewriteTracer(core.Tracer):
  rep: set[AxisName]
  val: Val

  def __init__(self, trace, rep, val):
    self._trace = trace
    self.rep = rep
    self.val = val

  @property
  def aval(self) -> core.AbstractValue:
    return core.get_aval(self.val)

  def to_concrete_value(self):
    return core.to_concrete_value(self.val)

  def __str__(self) -> str:
    return str(self.val)  # TODO(mattjj): could show replication info here
  __repr__ = __str__  # for debuggers, like `p x`

class RewriteTrace(core.Trace):
  __slots__ = ("parent_trace", "tag", "mesh")

  parent_trace : core.Trace
  tag : core.TraceTag
  mesh: Mesh

  def __init__(self, parent_trace, tag, mesh):
    super().__init__()
    self.parent_trace = parent_trace
    self.tag = tag
    self.mesh = mesh

  def to_val_rep_pair(self, val):
    # TODO: add a tag to tell if self
    if isinstance(val, RewriteTracer) and val._trace.tag is self.tag:
      return val.val, val.rep
    else:
      return val, set(self.mesh.axis_names)

  def process_primitive(self, prim, in_tracers, params):
    rule = _rewrite_rules.get(prim, partial(_rule_missing, prim))
    in_vals, in_reps = unzip2(map(self.to_val_rep_pair, in_tracers))
    with core.set_current_trace(self.parent_trace):
      out_vals, out_reps = rule(self.mesh, in_reps, *in_vals, **params)
    out_tracers = map(partial(RewriteTracer, self), out_reps, out_vals)
    return out_tracers if prim.multiple_results else out_tracers[0]

  def process_call(self, call_primitive, f, in_tracers, params):
    in_vals, in_reps = unzip2(map(self.to_val_rep_pair, in_tracers))
    f, out_reps = _rewrite_subtrace(f, self.tag, self.mesh, tuple(in_reps))
    with core.set_current_trace(self.parent_trace):
      out_vals = call_primitive.bind(f, *in_vals, **params)
    return map(partial(RewriteTracer, self), out_reps(), out_vals)

  def process_custom_jvp_call(self, prim, fun, jvp, tracers, *, symbolic_zeros):
    in_vals, in_reps = unzip2(map(self.to_val_rep_pair, tracers))
    fun, out_reps1 = _rewrite_subtrace(fun, self.tag, self.mesh, in_reps)
    jvp, out_reps2 = _rewrite_jvp_subtrace(jvp, self.tag, self.mesh, in_reps * 2)
    with core.set_current_trace(self.parent_trace):
      out_vals = prim.bind(fun, jvp, *in_vals, symbolic_zeros=symbolic_zeros)
    fst, out_reps = lu.merge_linear_aux(out_reps1, out_reps2)
    return map(partial(RewriteTracer, self), out_reps, out_vals)

  def process_custom_vjp_call(self, prim: core.Primitive, fun: lu.WrappedFun,
                              fwd: lu.WrappedFun, bwd: lu.WrappedFun, tracers,
                              out_trees: Callable[[], Sequence[PyTreeDef]],
                              symbolic_zeros: bool):
    if symbolic_zeros:
      msg = ("Please open an issue at https://github.com/jax-ml/jax/issues and "
             "as a temporary workaround pass the check_rep=False argument to "
             "shard_map")
      raise NotImplementedError(msg)
    in_vals, in_reps = unzip2(map(self.to_val_rep_pair, tracers))
    fun, out_reps1 = _rewrite_subtrace(fun, self.tag, self.mesh, in_reps)
    fwd_in_reps = [r_ for r in in_reps for r_ in [r, set(self.mesh.axis_names)]]
    fwd, out_reps2 = _rewrite_subtrace(fwd, self.tag, self.mesh, fwd_in_reps)
    bwd = _rewrite_bwd(bwd, self.mesh, out_reps2, in_reps)
    with core.set_current_trace(self.parent_trace):
      out_vals = prim.bind(fun, fwd, bwd, *in_vals, out_trees=out_trees,
                          symbolic_zeros=symbolic_zeros)
    fst, out_reps = lu.merge_linear_aux(out_reps1, out_reps2)
    if not fst:
      _, res_tree = out_trees()
      _, out_reps = split_list(out_reps, [res_tree.num_leaves])
    return map(partial(RewriteTracer, self), out_reps, out_vals)

def _efficient_transpose_rewrite(fun, mesh, in_names, out_names_thunk):
  in_reps = map(partial(_in_names_to_rep, mesh), in_names)
  out_reps_dst = lambda: [set(_unmentioned(mesh, n)) for n in out_names_thunk()]
  fun, out_reps_src = _efficient_transpose_rewrite_nomatch(fun, mesh, in_reps)
  return _match_rep(fun, mesh, out_reps_src, out_reps_dst)

@lu.transformation_with_aux2
def _efficient_transpose_rewrite_nomatch(f, store, mesh, in_reps, *args):
  with core.take_current_trace() as parent:
    tag = core.TraceTag()
    t = RewriteTrace(parent_trace=parent, tag=tag, mesh=mesh)
    in_tracers = map(partial(RewriteTracer, t), in_reps, args)
    with core.set_current_trace(t):
      ans = f(*in_tracers)
    out_vals, out_reps = unzip2(map(t.to_val_rep_pair, ans))
    del t, in_tracers, ans
  store.store(out_reps)
  return out_vals

@lu.transformation2
def _match_rep(f, mesh, out_reps_src_, out_reps_dst_, *args):
  outs = f(*args)
  out_reps_src = out_reps_src_() if callable(out_reps_src_) else out_reps_src_
  out_reps_dst = out_reps_dst_() if callable(out_reps_dst_) else out_reps_dst_
  _check_reps2(mesh, out_reps_dst, out_reps_src)
  outs = [pvary(x, tuple(n for n in src if n not in dst)) if src - dst
          else x for x, src, dst in zip(outs, out_reps_src, out_reps_dst)]
  return outs

# TODO(mattjj): caching
def _replication_rewrite_match(
    mesh: Mesh,
    jaxpr: core.ClosedJaxpr,
    in_rep: Sequence[set[AxisName]],
    out_rep_dst: Sequence[set[AxisName]],
) -> core.ClosedJaxpr:
  f = lu.wrap_init(partial(core.eval_jaxpr, jaxpr.jaxpr, jaxpr.consts),
                   debug_info=jaxpr.jaxpr.debug_info)
  f, out_rep = _efficient_transpose_rewrite_nomatch(f, mesh, in_rep)
  f = _match_rep(f, mesh, out_rep, out_rep_dst)
  jaxpr_, _, consts, () = pe.trace_to_jaxpr_dynamic(f, jaxpr.in_avals)
  return core.ClosedJaxpr(jaxpr_, consts)

# TODO(mattjj): caching
def _replication_rewrite_nomatch(
    mesh: Mesh,
    jaxpr: core.ClosedJaxpr,
    in_rep: Sequence[set[AxisName]],
) -> tuple[core.ClosedJaxpr, list[set[AxisName]]]:
  f = lu.wrap_init(partial(core.eval_jaxpr, jaxpr.jaxpr, jaxpr.consts),
                   debug_info=jaxpr.jaxpr.debug_info)
  f, out_rep = _efficient_transpose_rewrite_nomatch(f, mesh, in_rep)
  jaxpr_, _, consts, () = pe.trace_to_jaxpr_dynamic(f, jaxpr.in_avals)
  return core.ClosedJaxpr(jaxpr_, consts), out_rep()

@lu.transformation_with_aux2
def _rewrite_subtrace(f: Callable, store: lu.Store, tag: core.TraceTag,
                      mesh: Mesh, in_reps, *in_vals):
  with core.take_current_trace() as parent_trace:
    assert len(in_reps) == len(in_vals), (len(in_reps), len(in_vals))
    t = RewriteTrace(parent_trace, tag, mesh)
    in_tracers = map(partial(RewriteTracer, t), in_reps, in_vals)
    with core.set_current_trace(t):
      outs = f(*in_tracers)
    out_vals, out_reps = unzip2(map(t.to_val_rep_pair, outs))
    store.store(out_reps)
    return out_vals

@lu.transformation_with_aux2
def _rewrite_jvp_subtrace(f: Callable, store: lu.Store, tag: core.TraceTag,
                          mesh: Mesh, in_reps, *in_vals):
  with core.take_current_trace() as parent_trace:
    assert len(in_reps) == len(in_vals), (len(in_reps), len(in_vals))
    t = RewriteTrace(parent_trace, tag, mesh)
    in_tracers = [x if type(x) is cd.SymbolicZero else RewriteTracer(t, r, x)
                  for r, x in zip(in_reps, in_vals)]
    with core.set_current_trace(t):
      out_tracers: list[RewriteTracer | cd.SymbolicZero] = f(*in_tracers)
    out_vals, out_reps = unzip2(map(t.to_val_rep_pair, out_tracers))
    out_primals, out_tangents = split_list(out_vals, [len(out_vals) // 2])
    out_primal_reps, out_tangent_reps = split_list(out_reps, [len(out_vals) // 2])
    out_reps = map(_merge_reps, out_primal_reps, out_tangent_reps, out_tangents)
    out_tangents = map(_match_replication, out_tangent_reps, out_reps, out_tangents)
    store.store(out_reps)
    return out_primals + out_tangents

def _merge_reps(primal_rep, tangent_rep, error_message_val):
  if primal_rep - tangent_rep:
    raise ValueError("custom_jvp primal output is more replicated than its "
                     "corresponding tangent of type "
                     f"{core.typeof(error_message_val).str_short()}")
  return primal_rep

def _rewrite_bwd(bwd: lu.WrappedFun,
                 mesh: Mesh, in_reps, reps_dst) -> lu.WrappedFun:
  def new_bwd(*args):
    tag = core.TraceTag()
    bwd_, reps_thunk = _rewrite_subtrace(bwd, tag, mesh, in_reps())
    out = bwd_.call_wrapped(*args)
    return map(_match_replication, reps_thunk(), reps_dst, out)
  return lu.wrap_init(new_bwd, debug_info=bwd.debug_info)

def _match_replication(src, dst, x):
  if dst - src:
    x, = lax_parallel.psum_invariant_p.bind(
        x, axes=tuple(n for n in dst if n not in src), axis_index_groups=None)
  if src - dst:
    x = pvary(x, tuple(n for n in src if n not in dst))
  return x
