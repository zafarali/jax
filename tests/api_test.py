# Copyright 2018 Google LLC
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


import collections
from contextlib import contextmanager
import copy
import enum
from functools import partial
import operator
import re
import subprocess
import sys
import types
import unittest
import warnings
import weakref
import functools
import itertools as it
import operator as op

from absl import logging
from absl.testing import absltest, parameterized
import numpy as np

import concurrent.futures

import jax
import jax.numpy as jnp
from jax import float0, jit, grad, device_put, jacfwd, jacrev, hessian
from jax import core, dtypes, lax
from jax._src import api
from jax.core import Primitive
from jax.interpreters import ad
from jax.interpreters import xla
from jax.interpreters.sharded_jit import PartitionSpec as P
from jax.lib import xla_bridge as xb
from jax import test_util as jtu
from jax import tree_util
from jax import linear_util as lu
import jax._src.util

from jax.config import config
config.parse_flags_with_absl()
FLAGS = config.FLAGS


python_version = (sys.version_info[0], sys.version_info[1])
numpy_version = tuple(map(int, np.__version__.split('.')[:3]))


class CPPJitTest(jtu.BufferDonationTestCase):
  """Shared tests between the Python and the C++ jax,jit implementations.

  Because the Python implementation supports more features, we need to have the
  Python tests that extend the C++ tests (and not the other way around).
  """

  @property
  def jit(self):
    # Right now, the CPP tests also test the Python code-path when jaxlib is
    # too old.
    # TODO(jblespiau,phawkins): Remove this when jaxlib has been released.
    # This is in the future, because we are making a breaking change to
    # Tensorflow.
    return api._cpp_jit

  def test_jit_of_noncallable(self):
    self.assertRaisesRegex(TypeError, "Expected a callable value.*",
                           lambda: self.jit(3))

  def test_jit_of_generator(self):

    def gen(x):
      yield x

    self.assertRaisesRegex(TypeError,
                           "Expected a function, got a generator function.*",
                           lambda: self.jit(gen))

  @parameterized.parameters([
      # Integer support
      (1, 2, 3, 4, 5),
      # Numpy array support
      (
          np.asarray(1, np.int32),
          np.asarray(2, np.int32),
          np.asarray(3, np.int32),
          np.asarray(4, np.int32),
          np.asarray(5, np.int32),
      ),
  ])
  def test_jit_static_args(self, one, two, three, four, five):
    side = []

    def f(x, y, z, flag=False, flag2=False):
      del flag2  # unused
      assert flag
      side.append(None)
      return 100 * x + 10 * y + z

    f1 = self.jit(f, static_argnums=(3, 4))
    assert f1(one, two, three, True, False) == 123
    assert len(side) == 1
    assert f1(one, two, three, True, False) == 123
    assert len(side) == 1  # Obvious cache hit.
    assert f1(two, one, three, True, False) == 213
    assert len(side) == 1  # Should cache hit because same signature.
    assert f1(two, one, three, True, True) == 213
    assert len(side) == 2

    side[:] = []
    f2 = self.jit(f, static_argnums=(0, 2, 3, 4))
    assert f2(1, 2, 3, True, False) == 123
    assert len(side) == 1
    assert f2(1, 3, 3, True, False) == 133
    assert len(side) == 1
    assert f2(2, 2, 3, True, False) == 223
    assert len(side) == 2
    assert f2(2, 4, 3, True, False) == 243
    assert len(side) == 2
    assert f2(2, 4, 3, True, True) == 243
    assert len(side) == 3
    assert f2(2, 5, 3, True, True) == 253
    assert len(side) == 3

  def test_static_args_equality(self):
    class A():

      def __hash__(self):
        return 1

      def __eq__(self, other):
        return isinstance(other, A)

    side = []
    def f(x, static_arg):
      del static_arg
      side.append(None)
      return x * 100

    f1 = self.jit(f, static_argnums=(1,))

    self.assertEqual(f1(1, A()), 100)
    self.assertLen(side, 1)
    self.assertEqual(f1(1, A()), 100)
    self.assertLen(side, 1)
    if self.jit == api._cpp_jit:
      f1_cpp = getattr(f1, "_cpp_jitted_f", f1)
      self.assertEqual(f1_cpp._cache_size(), 1)

  @parameterized.parameters([
      (1, 2, 3),
      (
          np.asarray(1, np.int32),
          np.asarray(2, np.int32),
          np.asarray(3, np.int32),
      ),
  ])
  def test_jit_kwargs(self, one, two, three):
    side = []
    # For the CPP jit, we need to clear the cache to prevent cache hits between
    # parameterized tests.
    if hasattr(self.jit, "cache_clear"):
      self.jit.cache_clear()

    def f(x, y, z):
      side.append(None)
      return 100 * x + 10 * y + z

    f = self.jit(f)
    assert f(one, two, three) == 123
    assert len(side) == 1
    assert f(one, two, three) == 123
    assert len(side) == 1

    assert f(one, two, z=three) == 123
    assert len(side) == 2  # actually recompiles from kwarg
    assert f(one, two, z=three) == 123
    assert len(side) == 2  # but should still cache

    f(one, two, z=np.zeros(3))  # doesn't crash
    if config.x64_enabled:
      # In the above call, three is of a new type (int64), thus it should
      # trigger a new compilation.
      assert len(side) == 3

  def test_jit_device(self):
    device = xb.devices()[-1]
    x = self.jit(lambda x: x, device=device)(3.)
    self.assertIsInstance(x, xla.DeviceArray)
    self.assertEqual(x.device_buffer.device(), device)

  def test_complex_support(self):
    self.assertEqual(self.jit(lambda x: x + 1)(1 + 1j), 2 + 1j)

  def test_jit_with_many_args_works(self):

    @self.jit
    def f(args_list):
      return sum(args_list)

    self.assertEqual(f(list(range(500))), sum(range(500)))

  # Jit and Donate arguments

  def test_jit_donate_argnums_warning_raised(self):
    x = jnp.array([1.0, 2.0], jnp.float32)
    y = jnp.array([1, 2], jnp.int32)
    f = self.jit(lambda x, y: x.sum() + y.sum(), donate_argnums=(0, 1))
    with warnings.catch_warnings(record=True) as w:
      warnings.simplefilter("always")
      f(x, y)

      self.assertLen(w, 1)
      self.assertTrue(issubclass(w[-1].category, UserWarning))
      self.assertIn(
          "Some donated buffers were not usable: f32[2]{0}, s32[2]{0}",
          str(w[-1].message))

  @jtu.skip_on_devices("cpu")  # In/out aliasing not supported on CPU.
  def test_jit_donate_argnums_invalidates_input(self):
    # We can't just use `lambda x: x` because JAX simplifies this away to an
    # empty XLA computation.
    move = self.jit(lambda x: x + x - x, donate_argnums=0)
    x = jnp.ones([])
    y = move(x)
    self.assertDeleted(x)
    self.assertEqual(y, 1.)

  @jtu.skip_on_devices("cpu")  # In/out aliasing not supported on CPU.
  def test_jit_donate_argnums_static_argnums(self):
    jit_fun = self.jit(
        lambda a, b, c, d: ((a + b + c), (a + b + d)),
        static_argnums=(0, 1),
        donate_argnums=(2, 3))

    c = jax.device_put(jnp.array([1., 1.]))
    d = jax.device_put(jnp.array([1., 1., 1.]))
    e, f = jit_fun(1, 2, c, d)
    np.testing.assert_allclose(e, jnp.array([4., 4.]))
    np.testing.assert_allclose(f, jnp.array([4., 4., 4.]))
    self.assertDeleted(c)
    self.assertDeleted(d)

  @jtu.skip_on_devices("cpu")  # In/out aliasing not supported on CPU.
  def test_jnp_array_copy(self):
    # https://github.com/google/jax/issues/3412

    @partial(self.jit, donate_argnums=(0,))
    def _test(array):
      return array.at[0].set(77)

    x = jnp.asarray([0, 1])
    x_copy = jnp.array(x, copy=True)
    with warnings.catch_warnings():
      warnings.simplefilter("ignore")
      _test(x)  # donation

    # Gives: RuntimeError: Invalid argument: CopyToHostAsync() called on invalid buffer.
    print(x_copy)  # doesn't crash

  def test_jit_global_cache(self):
    def f(x):
      assert python_should_be_executing
      return x

    python_should_be_executing = True
    self.jit(f)(2)
    python_should_be_executing = False
    self.jit(f)(3)

  def test_jit_shallow_copy(self):
    def f(x):
      return copy.copy(x)
    self.jit(f)(1)

  def test_jit_deep_copy(self):
    def f(x):
      return copy.deepcopy(x)
    self.jit(f)(1)

  def test_disable_jit(self):
    effects = []

    @self.jit
    def f(x):
      effects.append(1)
      return x

    with api.disable_jit():
      f(2)
      f(2)
    assert len(effects) == 2

    f(2)
    f(2)
    assert len(effects) == 3

  def test_static_argnum_on_method(self):

    class A:

      @functools.partial(self.jit, static_argnums=(0,))
      def my_func_jit(self, x):
        return x+2

    A().my_func_jit(3)

  def test_static_argnum_on_static_method_is_not_supported(self):
    with self.assertRaisesRegex(TypeError, "Expected a callable value"):

      class A:

        @functools.partial(self.jit, static_argnums=(0,))
        @classmethod
        def my_classmethod_jit(cls, x):
          return x+2

  def test_classmethod_is_not_supported(self):
    with self.assertRaisesRegex(TypeError, "Expected a callable value"):

      class A:

        @functools.partial(self.jit)
        @staticmethod
        def my_staticmethod_jit(x):
          return x + 2

  def test_concurrent_jit(self):
    @self.jit
    def f(x):
      return x + x - 3.

    xs = [np.random.randn(i) for i in range(10)]
    with concurrent.futures.ThreadPoolExecutor() as executor:
      futures = [executor.submit(partial(f, x)) for x in xs]
      ys = [f.result() for f in futures]
    for x, y in zip(xs, ys):
      self.assertAllClose(x * 2 - 3., y)

  def test_trivial_computations(self):
    x = jnp.array([1, 2, 3])
    y = self.jit(lambda x: x)(x)
    self.assertIs(x, y)

    z1, z2 = self.jit(lambda x: (x, x))(x)
    self.assertIs(z1, z2)

    x1, x2 = jnp.array([1, 2]), jnp.array([2, 3])
    z1, z2, z3 = self.jit(lambda x, y: (y, 1, x))(x1, x2)
    self.assertIs(z1, x2)
    self.assertIs(z3, x1)
    self.assertEqual(z2, 1)

  def test_trivial_computations_with_tokens(self):
    @self.jit
    def noop(arr, token):
      return arr, token

    arr = jax.numpy.ones(10)
    token = jax.lax.create_token()

    self.assertEqual(token, noop(arr, token)[1])

  def test_jit_bad_input(self):
    def f(x):
      return x

    self.assertRaisesRegex(
        TypeError, ".* 'foo' of type <.*'str'> is not a valid JAX type",
        lambda: self.jit(f)("foo"))

  def test_jit_on_all_devices(self):
    # Verifies we can run the same computation on every device present, even
    # if they are, for example, different models of GPU.
    data = np.random.rand(1000).astype(np.float32)
    f = self.jit(jnp.negative)
    for device in jax.local_devices():
      x = device_put(data, device=device)
      np.testing.assert_array_equal(-data, f(x))

  def test_jit_nested_donate_ignored(self):
    jit_fun = self.jit(lambda x: self.jit(lambda y: y**2, donate_argnums=0)(x))
    a = jax.device_put(jnp.array(1))

    # NOTE(mattjj): stopped raising error here and instead just ignored
    # with self.assertRaisesRegex(ValueError, "nested.*not supported"):
    #   jit_fun(a)

    jit_fun(a)  # doesn't crash

  def test_jit_reference_dropping(self):
    x = jnp.ones(10)
    f = (lambda x: lambda: x)(x)  # reference to x in f's closure
    g = self.jit(f)
    x = weakref.ref(x)      # no more strong ref to x in this scope
    assert x() is not None  # x is still around
    f()                     # f runs
    g()                     # g runs
    g()                     # g runs a second time
    del f                   # delete the raw callable
    assert x() is not None  # x is still around
    g()                     # g still runs
    del g                   # no more references to x
    assert x() is None      # x is gone

  def test_jit_raises_on_first_invocation_on_non_hashable_static_argnum(self):
    if self.jit != api._python_jit:
      raise unittest.SkipTest("this test only applies to _python_jit")
    f = lambda x, y: x + 3
    jitted_f = self.jit(f, static_argnums=(1,))

    msg = ("Non-hashable static arguments are not supported, as this can lead "
           "to unexpected cache-misses. Static argument (index 1) of type "
           "<class 'numpy.ndarray'> for function <lambda> is non-hashable.")
    with self.assertRaisesRegex(ValueError, re.escape(msg)):
      jitted_f(1, np.asarray(1))

  def test_cpp_jit_raises_on_non_hashable_static_argnum(self):
    if self.jit != api._cpp_jit:
      raise unittest.SkipTest("this test only applies to _cpp_jit")

    f = lambda x, y: x + 3
    jitted_f = api._cpp_jit(f, static_argnums=[1])

    jitted_f(1, 1)

    msg = ("Non-hashable static arguments are not supported. An error occured "
           "while trying to hash an object of type <class 'numpy.ndarray'>, 1. "
           "The error was:\nTypeError: unhashable type: 'numpy.ndarray'")

    with self.assertRaisesRegex(ValueError, re.escape(msg)):
      jitted_f(1, np.asarray(1))

    class HashableWithoutEq:

      def __hash__(self):
        return 1

      def __eq__(self, other):
        raise NotImplementedError(
            "A Python error is as is, without stack trace")

    with self.assertRaisesRegex(
        ValueError,
        re.escape("static arguments should be comparable using __eq__")):
      jitted_f(1, HashableWithoutEq())

  def test_cpp_jitted_function_returns_PyBuffer(self):
    if self.jit != api._cpp_jit:
      raise unittest.SkipTest("this test only applies to _cpp_jit")

    jitted_f = self.jit(lambda a: a + 1)
    jitted_f(1)
    self.assertIsInstance(jitted_f(2), xla._CppDeviceArray)

  @jtu.skip_on_devices("cpu")
  def test_explicit_backend(self):
    f = lambda x: x + 1
    jitted_f = jit(f, backend=jtu.device_under_test())
    jitted_f_cpu = jit(f, backend="cpu")

    result = jitted_f(1.)
    result_cpu = jitted_f_cpu(1.)
    self.assertEqual(result.device_buffer.platform(), jtu.device_under_test())
    self.assertEqual(result_cpu.device_buffer.platform(), "cpu")

  @jtu.skip_on_devices("cpu")
  def test_device_to_device_copy_between_backends(self):
    # b/186624243
    f = lambda x: x + 1
    jitted_f = jit(f, backend=jtu.device_under_test())
    jitted_f_cpu = jit(f, backend="cpu")

    x = np.arange(30).reshape(1, 10, 3)
    result = jitted_f(x)
    result_cpu = jitted_f_cpu(result)
    result_2 = jitted_f(result_cpu)
    result_cpu_2 = jitted_f_cpu(result_2)
    self.assertAllClose(result_2, x + 3)
    self.assertAllClose(result_cpu_2, x + 4)

  @jtu.skip_on_devices("cpu")
  def test_mismatched_nested_backends(self):
    @partial(jit, backend=jtu.device_under_test())
    def f(x):
      return jit(lambda x: x + 1, backend="cpu")(x)

    with self.assertRaisesRegex(
        ValueError,
        f"Outer-jit backend specification {jtu.device_under_test()} must match "
        f"explicit inner-jit backend specification cpu."):
      f(1.)

  def test_omnistaging(self):
    # See https://github.com/google/jax/issues/5206
    key_list = [None]

    def init():
      key, subkey = jax.random.split(key_list[0])
      key_list[0] = key
      return jax.random.normal(subkey, ())

    key_list[0] = np.array([2384771982, 3928867769], dtype=np.uint32)
    init()
    self.jit(init)()
    self.assertIsInstance(key_list[0], core.Tracer)

  def test_jit_wrapped_attributes(self):
    def f(x: int) -> int:
      """docstring of f."""
      return x + 1
    f.some_value = 4
    jf = self.jit(f)
    for attr in ["doc", "name", "module", "qualname", "annotations"]:
      self.assertEqual(
        {attr: getattr(f, f"__{attr}__")},
        {attr: getattr(jf, f"__{attr}__")})
    self.assertEqual(f.some_value, jf.some_value)

  def test_jit_python_builtin(self):
    x = jnp.array([1, 2])
    expected = x + 1
    jit_add = self.jit(operator.add, static_argnums=(1,))
    actual = jit_add(x, 1)
    self.assertArraysEqual(expected, actual)

  def test__infer_argnums_and_argnames(self):
    def f(x, y=1):
      pass

    argnums, argnames = api._infer_argnums_and_argnames(
        f, argnums=None, argnames=None)
    assert argnums == ()
    assert argnames == ()

    argnums, argnames = api._infer_argnums_and_argnames(
        f, argnums=0, argnames=None)
    assert argnums == (0,)
    assert argnames == ('x',)

    argnums, argnames = api._infer_argnums_and_argnames(
        f, argnums=None, argnames='y')
    assert argnums == (1,)
    assert argnames == ('y',)

    argnums, argnames = api._infer_argnums_and_argnames(
        f, argnums=0, argnames='y')  # no validation
    assert argnums == (0,)
    assert argnames == ('y',)

    def g(x, y, *args):
      pass

    argnums, argnames = api._infer_argnums_and_argnames(
        g, argnums=(1, 2), argnames=None)
    assert argnums == (1, 2)
    assert argnames == ('y',)

    def h(x, y, **kwargs):
      pass

    argnums, argnames = api._infer_argnums_and_argnames(
        h, argnums=None, argnames=('foo', 'bar'))
    assert argnums == ()
    assert argnames == ('foo', 'bar')

  def test_jit_with_static_argnames(self):

    def f(x):
      assert x == 'foo'
      return 1

    f_nums = self.jit(f, static_argnums=0)
    assert f_nums('foo') == 1
    assert f_nums(x='foo') == 1

    f_names = self.jit(f, static_argnames='x')
    assert f_names('foo') == 1
    assert f_names(x='foo') == 1

  def test_new_static_argnum_on_keyword_arguments(self):
    f = self.jit(lambda x: x, static_argnums=0)
    y = f(x=4)
    assert y == 4

  def test_new_static_argnum_with_default_arguments(self):
    f = self.jit(lambda x=4: x, static_argnums=0)
    y = f()
    assert y == 4

  def test_jit_with_mismatched_static_argnames(self):
    x_is_tracer, y_is_tracer = False, False
    def f(x, y):
      assert isinstance(x, core.Tracer) == x_is_tracer
      assert isinstance(y, core.Tracer) == y_is_tracer
      return 1

    # If both static_argnums and static_argnames are provided, they are allowed
    # to disagree and `jit` will respect the user's choices.
    f_nums = self.jit(f, static_argnums=1, static_argnames=())
    x_is_tracer, y_is_tracer = True, False
    assert f_nums(2, 'foo') == 1
    x_is_tracer, y_is_tracer = True, True
    assert f_nums(1, y=2) == 1

    f_names = self.jit(f, static_argnums=(), static_argnames='y')
    x_is_tracer, y_is_tracer = True, True
    assert f_names(2, 3) == 1
    x_is_tracer, y_is_tracer = True, False
    assert f_names(1, y='foo') == 1

    f_mixed = self.jit(f, static_argnums=(1,), static_argnames='x')
    x_is_tracer, y_is_tracer = True, False
    assert f_mixed(2, 'foo') == 1
    x_is_tracer, y_is_tracer = True, True
    assert f_mixed(1, y=3) == 1
    x_is_tracer, y_is_tracer = False, True
    assert f_mixed(x='foo', y=3) == 1

  # TODO(zhangqiaorjc): Test pruning constants after DCE pass prunes primitive
  # applications.
  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "_num_args={}".format(num_args),
     "num_args": num_args}
    for num_args in [2, 3, 4]))
  def test_jit_with_pruned_args(self, num_args):
    def f(*args):
      used = np.array(2)
      return args[1] + used
    f_pruned = self.jit(f)
    args = range(num_args)
    with jtu.count_device_put() as count:
      np.testing.assert_allclose(f_pruned(*args), 3)
    self.assertEqual(count[0], 1)


class PythonJitTest(CPPJitTest):

  @property
  def jit(self):
    return api._python_jit


class APITest(jtu.JaxTestCase):

  def test_grad_bad_input(self):
    def f(x):
      return x

    self.assertRaisesRegex(
        TypeError, ".* 'foo' of type <.*'str'> is not a valid JAX type",
        lambda: grad(f)("foo"))

  def test_grad_argnums(self):
    def f(x, y, z, flag=False):
      assert flag
      return 1.0 * x + 2.0 * y + 3.0 * z

    assert grad(f)(1.0, 1.0, 1.0, flag=True) == 1.0
    assert grad(f, argnums=1)(1.0, 1.0, 1.0, flag=True) == 2.0
    assert grad(f, argnums=(2, 0))(1.0, 1.0, 1.0, flag=True) == (3.0, 1.0)

  def test_value_and_grad_argnums(self):
    def f(x, y, z, flag=False):
      assert flag
      return 1.0 * x + 2.0 * y + 3.0 * z

    y = f(1.0, 1.0, 1.0, flag=True)
    assert api.value_and_grad(f)(1.0, 1.0, 1.0, flag=True) == (y, 1.0)
    assert api.value_and_grad(f, argnums=1)(1.0, 1.0, 1.0, flag=True) == (y, 2.0)
    assert api.value_and_grad(f, argnums=(2, 0))(1.0, 1.0, 1.0, flag=True) == (y, (3.0, 1.0))

  def test_grad_of_jit(self):
    side = []

    @jit
    def f(x):
      side.append(None)
      return x * x

    assert grad(f)(1.0) == 2.0
    assert len(side) == 1
    assert grad(f)(2.0) == 4.0
    assert len(side) == 1

  def test_jit_of_grad(self):
    side = []

    @jit
    def f(x):
      side.append(None)
      return x * x

    g = jit(grad(f))
    assert g(1.0) == 2.0
    assert len(side) == 1
    assert g(2.0) == 4.0
    assert len(side) == 1

  def test_bad_input(self):
    def f(x):
      return x

    self.assertRaisesRegex(
      TypeError, ".* 'foo' of type <.*'str'> is not a valid JAX type",
      lambda: grad(f)("foo"))

    self.assertRaisesRegex(
      TypeError, ".* 'foo' of type <.*'str'> is not a valid JAX type",
      lambda: jit(f)("foo"))

  def test_grad_tuple_output(self):
    jtu.check_raises(lambda: grad(lambda x: (x,x))(1.0), TypeError,
                     "Gradient only defined for scalar-output functions. ")

  def test_grad_unit_output(self):
    jtu.check_raises(lambda: grad(lambda x: ())(np.zeros(3)), TypeError,
                     "Gradient only defined for scalar-output functions. ")

  def test_grad_nonscalar_output(self):
    jtu.check_raises(lambda: grad(lambda x: x)(np.zeros(3)), TypeError,
                     "Gradient only defined for scalar-output functions. ")

  def test_unwrapped_numpy(self):
    def f(x):
      return np.exp(x)

    with self.assertRaisesRegex(Exception, "The numpy.ndarray conversion .*"):
      grad(f)(np.zeros(3))

  def test_binop_mismatch(self):
    def f(x, y):
      return x + y

    jtu.check_raises(
        lambda: f(jnp.zeros(3), jnp.zeros(4)),
        TypeError,
        "add got incompatible shapes for broadcasting: (3,), (4,).")

    jtu.check_raises(
        lambda: grad(f)(np.zeros(3), np.zeros(4)),
        TypeError,
        "add got incompatible shapes for broadcasting: (3,), (4,).")

  def test_dot_mismatch(self):
    def f(x, y):
      return jnp.dot(x, y)

    self.assertRaisesRegex(
      TypeError, "Incompatible shapes for dot: got \\(3L?,\\) and \\(4L?,\\).",
      lambda: grad(f)(np.zeros(3), np.zeros(4)))

  def test_abstract_error_message(self):
    for castfun in [float, complex, int]:
      def f(x):
        return castfun(x)

      self.assertRaisesRegex(
          TypeError,
          f"[Tt]ry using `x.astype\\({castfun.__name__}\\)`",
          lambda: jit(f)(1.0))

  def test_switch_value_jit(self):
    def f(x):
      y = x > 0
      if y:
        return x
      else:
        return -x

    assert grad(f)(1.0) == 1.0
    assert grad(f)(-1.0) == -1.0
    with self.assertRaisesRegex(core.ConcretizationTypeError,
                                "Abstract tracer value"):
      jit(f)(1)

  def test_list_index_err(self):
    L = [1, 2, 3]
    def f(n):
      return L[n]

    assert jit(f, static_argnums=(0,))(0) == L[0]
    self.assertRaisesRegex(
        TypeError,
        r"The __index__\(\) method was called on the JAX Tracer object.*",
        lambda: jit(f)(0))

  def test_range_err(self):
    def f(x, n):
      for i in range(n):
        x = x + i
      return x

    assert jit(f, static_argnums=(1,))(0, 5) == 10
    self.assertRaisesRegex(
        TypeError,
        r"The __index__\(\) method was called on the JAX Tracer object.*",
        lambda: jit(f)(0, 5))

  def test_cast_int(self):
    f = lambda x: int(x)
    self.assertRaisesRegex(
        TypeError,
        "('(?:JaxprTracer|DynamicJaxprTracer)' object cannot be interpreted as an integer"
        "|Abstract tracer value encountered where concrete value is expected.*)", lambda: jit(f)(0))

  def test_casts(self):
    for castfun in [hex, oct]:
      f = lambda x: castfun(x)
      self.assertRaisesRegex(
          TypeError,
          r"The __index__\(\) method was called on the JAX Tracer object.*", lambda: jit(f)(0))

  def test_unimplemented_interpreter_rules(self):
    foo_p = Primitive('foo')
    def foo(x):
      return foo_p.bind(x)

    jtu.check_raises(lambda: foo(1.0), NotImplementedError,
                     "Evaluation rule for 'foo' not implemented")

    jtu.check_raises(lambda: jit(foo)(1.0), NotImplementedError,
                     "Abstract evaluation for 'foo' not implemented")

    jtu.check_raises(lambda: grad(foo)(1.0), NotImplementedError,
                     "Differentiation rule for 'foo' not implemented")

    foo_p.def_abstract_eval(lambda x: x)

    jtu.check_raises(lambda: jit(foo)(1.0), NotImplementedError,
                     "XLA translation rule for primitive 'foo' not found")

    foo_p.def_impl(lambda x: x)
    ad.defjvp(foo_p, lambda g, x: foo(g))

    jtu.check_raises(lambda: grad(foo)(1.0), NotImplementedError,
                     "Transpose rule (for reverse-mode differentiation) for 'foo' not implemented")

  def test_device_put_and_get(self):
    x = np.arange(12.).reshape((3, 4)).astype("float32")
    dx = api.device_put(x)
    self.assertIsInstance(dx, xla.DeviceArray)
    x2 = api.device_get(dx)
    self.assertIsInstance(x2, np.ndarray)
    assert np.all(x == x2)

    y = [x, (2 * x, 3 * x)]
    dy = api.device_put(y)
    y2 = api.device_get(dy)
    self.assertIsInstance(y2, list)
    self.assertIsInstance(y2[0], np.ndarray)
    assert np.all(y2[0] == x)
    self.assertIsInstance(y2[1], tuple)
    self.assertIsInstance(y2[1][0], np.ndarray)
    assert np.all(y2[1][0] == 2 * x)
    self.assertIsInstance(y2[1][1], np.ndarray)
    assert np.all(y2[1][1] == 3 * x)

  def test_device_get_scalar(self):
    x = np.arange(12.).reshape((3, 4)).astype("float32")
    x = api.device_put(x)
    self.assertIsInstance(x, xla.DeviceArray)
    y = [x, 2]
    y2 = api.device_get(y)
    self.assertIsInstance(y2, list)
    self.assertIsInstance(y2[0], np.ndarray)
    assert np.all(y2[0] == x)
    self.assertIsInstance(y2[1], int)
    self.assertEqual(y2[1], 2)

  @parameterized.parameters([(3,)], [(2, 0)])
  def test_device_put_across_devices(self, shape):
    if len(api.local_devices()) < 2:
      raise unittest.SkipTest("this test requires multiple devices")
    d1, d2 = api.local_devices()[:2]
    data = np.random.randn(*shape).astype(np.float32)
    x = api.device_put(data, device=d1)
    self.assertEqual(x.device_buffer.device(), d1)
    y = api.device_put(x, device=d2)
    self.assertEqual(y.device_buffer.device(), d2)
    np.testing.assert_array_equal(data, np.array(y))
    # Make sure these don't crash
    api.device_put(x)
    api.device_put(y)

  @jtu.skip_on_devices("cpu")
  def test_device_put_across_platforms(self):
    default_device = jax.devices()[0]
    cpu_device = jax.devices("cpu")[0]

    np_arr = np.array([1,2,3])
    scalar = 1
    device_arr = jnp.array([1,2,3])
    assert device_arr.device_buffer.device() is default_device

    for val in [np_arr, device_arr, scalar]:
      x = api.device_put(val, device=cpu_device)
      self.assertEqual(x.device_buffer.device(), cpu_device)

  @jtu.skip_on_devices("tpu")
  def test_jacobian(self):
    R = np.random.RandomState(0).randn
    A = R(4, 3)
    x = R(3)

    f = lambda x: jnp.dot(A, x)
    assert np.allclose(jacfwd(f)(x), A)
    assert np.allclose(jacrev(f)(x), A)

    f = lambda x: jnp.tanh(jnp.dot(A, x))
    assert np.allclose(jacfwd(f)(x), jacrev(f)(x))

  @jtu.skip_on_devices("tpu")
  def test_hessian(self):
    R = np.random.RandomState(0).randn
    A = R(4, 4)
    x = R(4)

    f = lambda x: jnp.dot(x, jnp.dot(A, x))
    assert np.allclose(hessian(f)(x), A + A.T)

  def test_std_basis(self):
    basis = api._std_basis(jnp.zeros(3))
    assert getattr(basis, "shape", None) == (3, 3)
    assert np.allclose(basis, np.eye(3))

    basis = api._std_basis(jnp.zeros((3, 3)))
    assert getattr(basis, "shape", None) == (9, 3, 3)
    assert np.allclose(basis, np.eye(9).reshape(9, 3, 3))

    basis = api._std_basis([0., (jnp.zeros(3), jnp.zeros((3, 4)))])
    assert isinstance(basis, list) and len(basis) == 2
    assert getattr(basis[0], "shape", None) == (16,)
    assert isinstance(basis[1], tuple) and len(basis[1]) == 2
    assert getattr(basis[1][0], "shape", None) == (16, 3)
    assert getattr(basis[1][1], "shape", None) == (16, 3, 4)

  @jtu.skip_on_devices("tpu")
  def test_jacobian_on_pytrees(self):
    for jacfun in [jacfwd, jacrev]:
      ans = jacfun(lambda x, y: (x, y))(0., 1.)
      expected = (1., 0.)
      self.assertAllClose(ans, expected, check_dtypes=False)

      ans = jacfun(lambda x, y: (x, y), 1)(0., 1.)
      expected = (0., 1.)
      self.assertAllClose(ans, expected, check_dtypes=False)

      ans = jacfun(lambda x, y: (x, y), (0, 1))(0., 1.)
      expected = ((1., 0.),
                  (0., 1.),)
      self.assertAllClose(ans, expected, check_dtypes=False)

      ans = jacfun(lambda x: x[:2])((1., 2., 3.))
      expected = ((1., 0., 0.),
                  (0., 1., 0.))
      self.assertAllClose(ans, expected, check_dtypes=False)

      R = np.random.RandomState(0).randn
      x = R(2)
      y = R(3)
      ans = jacfun(lambda x, y: {'x': x, 'xy': jnp.outer(x, y)})(x, y)
      expected = {'x': np.eye(2),
                  'xy': np.kron(np.eye(2), y[:, None]).reshape(2, 3, 2)}
      self.assertAllClose(ans, expected, check_dtypes=False)

  @jtu.skip_on_devices("tpu")
  def test_hessian_on_pytrees(self):
    ans = hessian(lambda x: jnp.array(x)**2)((1., 2.))
    expected = ((np.array([2., 0.]), np.array([0., 0.])),
                (np.array([0., 0.]), np.array([0., 2.])))
    self.assertAllClose(ans, expected, check_dtypes=False)

  @jtu.skip_on_devices("tpu")
  def test_issue1372(self):
    def quad(x):
      return jnp.dot(x, x)

    def f(x, u):
      return quad(x) + quad(u)

    x, u = jnp.ones(5), jnp.ones(2)

    rev = jacrev
    fwd = jacfwd

    # Diagonal entries
    self.assertEqual(rev(rev(f, 0), 0)(x, u).shape, (5, 5))
    self.assertEqual(rev(fwd(f, 0), 0)(x, u).shape, (5, 5))
    self.assertEqual(fwd(rev(f, 0), 0)(x, u).shape, (5, 5))
    self.assertEqual(fwd(fwd(f, 0), 0)(x, u).shape, (5, 5))
    self.assertEqual(rev(rev(f, 1), 1)(x, u).shape, (2, 2))
    self.assertEqual(rev(fwd(f, 1), 1)(x, u).shape, (2, 2))
    self.assertEqual(fwd(rev(f, 1), 1)(x, u).shape, (2, 2))
    self.assertEqual(fwd(fwd(f, 1), 1)(x, u).shape, (2, 2))

    # Off-diagonal entries by reverse-mode on the outside
    self.assertEqual(rev(rev(f, 1), 0)(x, u).shape, (2, 5))
    self.assertEqual(rev(fwd(f, 1), 0)(x, u).shape, (2, 5))
    self.assertEqual(rev(rev(f, 0), 1)(x, u).shape, (5, 2))
    self.assertEqual(rev(fwd(f, 0), 1)(x, u).shape, (5, 2))

    # Off-diagonal entries by forward-mode on the outside
    self.assertEqual(fwd(rev(f, 1), 0)(x, u).shape, (2, 5))
    self.assertEqual(fwd(fwd(f, 1), 0)(x, u).shape, (2, 5))
    self.assertEqual(fwd(rev(f, 0), 1)(x, u).shape, (5, 2))
    self.assertEqual(fwd(fwd(f, 0), 1)(x, u).shape, (5, 2))


  def test_large_device_constant(self):
    ans = jit(lambda x: 2 * x)(jnp.ones(int(2e6)))  # doesn't crash
    self.assertAllClose(ans, np.ones(int(2e6)) * 2., check_dtypes=False)

  def test_grad_and_aux_basic(self):
    g, aux = grad(lambda x: (x**3, [x**2]), has_aux=True)(3.)
    self.assertAllClose(g, grad(lambda x: x**3)(3.))
    self.assertAllClose(aux, [9.], check_dtypes=False)

  def test_grad_and_aux_error(self):
    with self.assertRaisesRegex(TypeError, "two-element tuple"):
      grad(lambda x: (1, 2, 3), has_aux=True)(1.)

    with self.assertRaisesRegex(TypeError, "two-element tuple"):
      grad(lambda x: x, has_aux=True)(1.)

    with self.assertRaisesRegex(TypeError, "two-element tuple"):
      grad(lambda x: (x,), has_aux=True)(1.)

  def test_grad_and_aux_nested(self):
    def f(x):
      g, aux = grad(lambda x: (x**3, [x**3]), has_aux=True)(x)
      return aux[0]

    f2 = lambda x: x**3

    self.assertEqual(grad(f)(4.), grad(f2)(4.))
    self.assertEqual(jit(grad(f))(4.), grad(f2)(4.))
    self.assertEqual(jit(grad(jit(f)))(4.), grad(f2)(4.))

    def f(x):
      g, aux = grad(lambda x: (x**3, [x**3]), has_aux=True)(x)
      return aux[0] * jnp.sin(x)

    f2 = lambda x: x**3 * jnp.sin(x)

    self.assertEqual(grad(f)(4.), grad(f2)(4.))
    self.assertEqual(jit(grad(f))(4.), grad(f2)(4.))
    self.assertEqual(jit(grad(jit(f)))(4.), grad(f2)(4.))

  def test_grad_and_aux_constant(self):
    g, aux = grad(lambda x: (x**3, [4.]), has_aux=True)(4.)
    self.assertEqual(g, grad(lambda x: x**3)(4.))
    self.assertEqual(aux, [4.])

    g, aux = grad(lambda x: (x**3, [x**2, 4.]), has_aux=True)(4.)
    self.assertEqual(g, grad(lambda x: x**3)(4.))
    self.assertEqual(aux, [4.**2, 4.])

  def test_grad_and_aux_no_tracers(self):
    # see https://github.com/google/jax/issues/1950
    def f(x):
      aux = dict(identity=x, p1=x+1)
      return x ** 2, aux

    _, aux = jax.grad(f, has_aux=True)(3.)
    self.assertIsInstance(aux, dict)
    for val in aux.values():
      self.assertNotIsInstance(val, core.Tracer)

  def test_jvp_mismatched_arguments(self):
    self.assertRaisesRegex(
      TypeError,
      ("primal and tangent arguments to jax.jvp must have the same tree "
       "structure"),
      lambda: api.jvp(lambda x, y: x * y, (np.float32(2),), ()))
    # If primals and tangents must both be tuples or both lists
    self.assertRaisesRegex(
      TypeError,
      ("primal and tangent arguments to jax.jvp must have the same tree "
       "structure"),
      lambda: api.jvp(lambda x, y: x * y, (np.float32(2),), [np.float32(2)]))
    self.assertRaisesRegex(
      TypeError,
      "primal and tangent arguments to jax.jvp do not match.",
      lambda: api.jvp(lambda x: -x, (np.float16(2),), (np.float32(4),)))
    # If primals and tangents are not of the same shape then raise error
    fun = lambda x: x+1
    with self.assertRaisesRegex(
      ValueError, "jvp called with different primal and tangent shapes"):
      api.jvp(fun, (jnp.array([1.,2.,3.]),), (jnp.array([1.,2.,3.,4.]),))
    with self.assertRaisesRegex(
      ValueError, "jvp called with different primal and tangent shapes"):
      api.jvp(fun, (jnp.float32(10.),), (jnp.array([1.,2.,3.], dtype=jnp.float32),))
    with self.assertRaisesRegex(
      ValueError, "jvp called with different primal and tangent shapes"):
      api.jvp(fun, (jnp.array([1.,2.,3.], dtype=jnp.float32),), (jnp.float32(20.),))
    with self.assertRaisesRegex(
      ValueError, "jvp called with different primal and tangent shapes"):
      api.jvp(fun, (jnp.array([1.,2.,3.]),), (20.,))

  def test_jvp_non_tuple_arguments(self):
    def f(x, y): return x + y
    self.assertRaisesRegex(
        TypeError,
        "primal and tangent arguments to jax.jvp must be tuples or lists; found float and tuple.",
        lambda: api.jvp(f, 0., (1.,)))
    self.assertRaisesRegex(
        TypeError,
        "primal and tangent arguments to jax.jvp must be tuples or lists; found tuple and ndarray.",
        lambda: api.jvp(f, (0.,), np.array([1., 2.])))

  def test_vjp_mismatched_arguments(self):
    _, pullback = api.vjp(lambda x, y: x * y, np.float32(3), np.float32(4))
    self.assertRaisesRegex(
      TypeError,
      "Tree structure of cotangent input.*does not match",
      lambda: pullback((np.float32(7), np.float32(100))))
    self.assertRaisesRegex(
      TypeError,
      "Type of cotangent input to vjp pullback.*is not the expected tangent type",
      lambda: pullback((np.float16(42))))

  def test_vjp_bad_cotangent_shape(self):
    x = np.ones((2, 5), dtype=np.float32)
    y = np.ones((5, 3), dtype=np.float32)
    def f_jax(x, y):
      return jnp.matmul(x, y)
    res, pullback = jax.vjp(f_jax, x, y)
    with self.assertRaisesRegex(
        ValueError,
        "Shape of cotangent input to vjp pullback function .* must be the same as the shape of corresponding primal input .*"):
      pullback(np.ones((2, 4), dtype=np.float32))

  def test_jvp_jit_cached(self):
    """Bug in caching in presence of JVP and JIT."""

    def func(x):
      def inner(y):
        return y * x

      # Must have two calls to the inner jit (the second one hits the cache)
      res1 = api.jit(inner)(4.)
      res2 = api.jit(inner)(5.)
      return res1 + res2

    self.assertAllClose((45., 9.), api.jvp(func, (5.,), (1.,)))

  def test_linear_transpose_abstract(self):
    x = types.SimpleNamespace(shape=(3,), dtype=np.dtype(np.float32))
    y = jnp.arange(3, dtype=np.float32)
    transpose_fun = api.linear_transpose(lambda x: 2 * x, x)
    z, = transpose_fun(y)
    self.assertArraysEqual(2 * y, z, check_dtypes=True)

  def test_linear_transpose_integer(self):
    f = lambda x: 2 * x
    transpose = api.linear_transpose(f, 1)
    actual, = transpose(3)
    expected = 6
    self.assertEqual(actual, expected)

  def test_linear_transpose_error(self):
    with self.assertRaisesRegex(
        TypeError, "linear_transpose only supports"):
      api.linear_transpose(lambda x: 2. * x, 1)
    transpose_fun = api.linear_transpose(lambda x: [x, x], 1.0)
    with self.assertRaisesRegex(TypeError, "cotangent tree does not match"):
      transpose_fun(1.0)

    transpose_fun = api.linear_transpose(lambda x: jnp.stack([x, x]), 1.0)
    with self.assertRaisesRegex(TypeError, "cotangent type does not match"):
      transpose_fun(1.0)

    transpose_fun = api.linear_transpose(lambda x: 1j * x, 1.0)
    with self.assertRaisesRegex(TypeError, "cotangent type does not match"):
      transpose_fun(1.0)

    transpose_fun = api.linear_transpose(lambda x: x, 1.0)
    with self.assertRaisesRegex(TypeError, "cotangent type does not match"):
      transpose_fun(1j)

  def test_linear_transpose_complex(self):
    f = lambda x: (1 + 2j) * x
    transpose = api.linear_transpose(f, 1j)
    actual, = transpose(3 + 4j)
    expected = -5 + 10j
    self.assertEqual(actual, expected)

  def test_linear_transpose_zeros(self):
    f = lambda x: x[0]
    transpose = api.linear_transpose(f, [1., 2.])
    actual, = transpose(3.)
    expected = [3., 0.]
    self.assertEqual(actual, expected)

  def test_complex_grad_raises_error(self):
    self.assertRaises(TypeError, lambda: grad(lambda x: jnp.sin(x))(1 + 2j))

  def test_holomorphic_grad(self):
    out = grad(lambda x: jnp.sin(x), holomorphic=True)(1 + 2j)
    expected = 2.0327230070196656 - 3.0518977991518j
    self.assertAllClose(out, expected, check_dtypes=False)

  def test_nonholomorphic_grad(self):
    zs = 0.5j * np.arange(5) + np.arange(5)

    def f(z):
      return jnp.sum(jnp.cos(jnp.abs(z)))

    ans = grad(f)(zs)
    expected = np.array([ 0.        +0.j,
                          -0.80430663+0.40215331j,
                          -0.70368982+0.35184491j,
                           0.1886467 -0.09432335j,
                           0.86873727-0.43436864j])
    self.assertAllClose(ans, expected, check_dtypes=False,
                        atol=jtu.default_gradient_tolerance,
                        rtol=jtu.default_gradient_tolerance)

  def test_complex_output_jacrev_raises_error(self):
    self.assertRaises(TypeError, lambda: jacrev(lambda x: jnp.sin(x))(1 + 2j))

  def test_nonholomorphic_jacrev(self):
    # code based on https://github.com/google/jax/issues/603
    zs = 0.5j * np.arange(5) + np.arange(5)

    def f(z):
      return jnp.cos(jnp.linalg.norm(2 * z))

    ans = jacrev(f)(zs)
    expected = grad(f)(zs)
    self.assertAllClose(ans, expected)

  def test_complex_input_jacfwd_raises_error(self):
    self.assertRaises(TypeError, lambda: jacfwd(lambda x: jnp.sin(x))(1 + 2j))

  def test_legacy_devicearray_repr(self):
    dx = device_put(3.)
    str(dx.item())  # doesn't crash

  def test_devicearray_repr(self):
    x = device_put(jnp.zeros(3))
    self.assertIsInstance(x, xla.DeviceArray)
    repr(x)  # doesn't crash

    x = device_put(jnp.ones(3) + 1j * jnp.ones(3))
    self.assertIsInstance(x, xla.DeviceArray)
    repr(x)  # doesn't crash

  def test_devicearray_delete(self):
    x = device_put(1.)
    x.delete()
    self.assertRaisesRegex(RuntimeError, "DeviceArray has been deleted.",
                           lambda: repr(x))

  def test_devicearray_block_until_ready(self):
    x = device_put(1.)
    y = x.block_until_ready()
    # Tests mostly that block_until_ready() does not produce an error.
    self.assertTrue(y is x)

  def test_devicearray_weakref_friendly(self):
    x = device_put(1.)
    y = weakref.ref(x)
    self.assertEqual(y(), 1.)
    del x
    self.assertIsNone(y())

  def test_namedtuple_transparency(self):
    # See https://github.com/google/jax/issues/446
    Point = collections.namedtuple("Point", ["x", "y"])

    def f(pt):
      return jnp.sqrt(pt.x ** 2 + pt.y ** 2)

    pt = Point(1., 2.)

    f(pt)  # doesn't crash
    g = api.grad(f)(pt)
    self.assertIsInstance(g, Point)

    f_jit = api.jit(f)
    self.assertAllClose(f(pt), f_jit(pt), check_dtypes=False)

  def test_namedtuple_subclass_transparency(self):
    # See https://github.com/google/jax/issues/806
    Point = collections.namedtuple("Point", ["x", "y"])

    class ZeroPoint(Point):
      def is_zero(self):
        return (self.x == 0) and (self.y == 0)

    pt = ZeroPoint(0., 0.)

    def f(pt):
      return 0. if pt.is_zero() else jnp.sqrt(pt.x ** 2 + pt.y ** 2)

    f(pt)  # doesn't crash
    _ = api.grad(f)(pt)
    self.assertIsInstance(pt, ZeroPoint)

  @parameterized.parameters(1, 2, 3)
  def test_shape_dtype_struct(self, i):
    s = api.ShapeDtypeStruct(shape=(i, 2, 3), dtype=jnp.float32)
    self.assertEqual(s.shape, (i, 2, 3))
    self.assertEqual(s.dtype, jnp.float32)
    self.assertEqual(s.ndim, 3)
    self.assertEqual(s.size, i * 2 * 3)
    self.assertLen(s, i)
    for f in (str, repr):
      self.assertEqual(
          f(s), "ShapeDtypeStruct(shape=({}, 2, 3), dtype=float32)".format(i))

  def test_shape_dtype_struct_scalar(self):
    s = api.ShapeDtypeStruct(shape=(), dtype=jnp.float32)
    self.assertEmpty(s.shape)
    self.assertEqual(s.size, 1)
    self.assertEqual(s.ndim, 0)
    with self.assertRaisesRegex(TypeError, "len[(][)] of unsized object"):
      _ = len(s)

  def test_eval_shape(self):
    def fun(x, y):
      return jnp.tanh(jnp.dot(x, y) + 3.)

    x = jnp.ones((2, 3))
    y = jnp.ones((3, 4))
    out_shape = api.eval_shape(fun, x, y)

    self.assertEqual(out_shape.shape, (2, 4))

  def test_eval_shape_constants(self):
    def fun():
      x = jnp.ones((2, 3))
      y = jnp.ones((3, 4))
      return jnp.tanh(jnp.dot(x, y) + 3.)

    out_shape = api.eval_shape(fun)

    self.assertEqual(out_shape.shape, (2, 4))

  def test_eval_shape_tuple_unpacking(self):
    def fun(x, y):
      a, b = x
      return a + b + y

    x = (jnp.ones(2), jnp.ones(2))
    y = 3.
    out_shape = api.eval_shape(fun, x, y)

    self.assertEqual(out_shape.shape, (2,))

  def test_eval_shape_tuple_itemgetting(self):
    def fun(x, y):
      return x[0] + x[1] + y

    x = (jnp.ones(2), jnp.ones(2))
    y = 3.
    out_shape = api.eval_shape(fun, x, y)

    self.assertEqual(out_shape.shape, (2,))

  def test_eval_shape_output_dict(self):
    def fun(x, y):
      return {'hi': x[0] + x[1] + y}

    x = (jnp.ones(2), jnp.ones(2))
    y = 3.
    out_shape = api.eval_shape(fun, x, y)
    out_shape = tree_util.tree_map(np.shape, out_shape)

    self.assertEqual(out_shape, {'hi': (2,)})

  def test_eval_shape_shape_error(self):
    def fun(x, y):
      return jnp.tanh(jnp.dot(x, y) + 3.)

    x = jnp.ones((3, 3))
    y = jnp.ones((4, 4))

    self.assertRaises(TypeError, lambda: api.eval_shape(fun, x, y))

  def test_eval_shape_duck_typing(self):
    def fun(A, b, x):
      return jnp.dot(A, x) + b

    class MyArgArray(object):
      def __init__(self, shape, dtype):
        self.shape = shape
        self.dtype = np.dtype(dtype)

    A = MyArgArray((3, 4), jnp.float32)
    b = MyArgArray((5,), jnp.float32)
    x = MyArgArray((4, 5), jnp.float32)
    out_shape = api.eval_shape(fun, A, b, x)

    self.assertEqual(out_shape.shape, (3, 5))

  def test_eval_shape_duck_typing2(self):
    # https://github.com/google/jax/issues/5683
    class EasyDict(dict):
      def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

    x = EasyDict(shape=(3,), dtype=np.dtype('float32'))
    out_shape = api.eval_shape(lambda x: x, x)  # doesn't crash
    self.assertEqual(out_shape.shape, (3,))

  def test_eval_shape_names(self):
    def fun(x, y):
      return lax.psum(x, 'i') + y

    class MyArgArray(object):
      def __init__(self, shape, dtype, named_shape):
        self.shape = shape
        self.dtype = jnp.dtype(dtype)
        self.named_shape = named_shape

    x = MyArgArray((3, 2), jnp.float32, {'i': 10})
    y = MyArgArray((3, 2), jnp.float32, {'j': 5})
    with core.extend_axis_env('i', 10, None):
      with core.extend_axis_env('j', 5, None):
        out_shape = api.eval_shape(fun, x, y)

    self.assertEqual(out_shape.named_shape, {'j': 5})

  def test_issue_871(self):
    T = jnp.array([[1., 2.], [3., 4.], [5., 6.]])
    x = jnp.array([1, 2, 3])
    msg = ("linearized function called on tangent values inconsistent with "
           "the original primal values")

    y, f_jvp = api.linearize(jnp.sum, x)
    with self.assertRaisesRegex(ValueError, msg):
      f_jvp(T)

    y, f_jvp = api.linearize(api.jit(jnp.sum), x)
    with self.assertRaisesRegex(ValueError, msg):
      f_jvp(T)

  def test_partial_eval_lower(self):
    # this is a simplified model of a bug that arose when we first used @jit in
    # a jvp rule. it's in this file because we want to use make_jaxpr.

    # NOTE(mattjj): I no longer understand what this was meant to test. My guess
    # is it was related to staging out the broadcast into a jaxpr to be
    # transposed, but after #1749 that's no longer a problem. After changing
    # make_jaxpr (and jit) to stage out sub-calls fully, this test started to
    # fail; I left it in as skipped because deleting tests feels wrong.
    raise unittest.SkipTest("obsolete test")

    @api.jit
    def f(a, b, c):
      a = lax.broadcast(a, (2,))
      return lax.select(a, b, c)

    a = np.ones((3, 3), dtype=np.bool_)
    b = np.ones((2, 3, 3))
    c = np.ones((2, 3, 3))

    jaxpr = api.make_jaxpr(lambda b, c: f(a, b, c))(b, c)
    subjaxpr = next(eqn.params["call_jaxpr"] for eqn in jaxpr.jaxpr.eqns
                    if "call_jaxpr" in eqn.params)
    self.assertEqual(len(subjaxpr.eqns), 1)

  def test_grad_of_int_errors(self):
    # Errors without allow_int=True
    dfn = grad(lambda x: x ** 2)
    self.assertRaisesRegex(
      TypeError,
      (r"grad requires real- or complex-valued inputs \(input dtype that is a "
       r"sub-dtype of np.floating or np.complexfloating\), but got int.*."),
      lambda: dfn(3))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_jvp_of_int_identity(self):
    primals = (1,)
    tangents = (np.zeros(shape=(), dtype=float0),)

    _, out = api.jvp(lambda x: x, primals, tangents)
    self.assertEqual(out, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_jvp_of_int_add(self):
    primals = (2,)
    tangents = (np.zeros(shape=(), dtype=float0),)

    _, out_tangent = api.jvp(lambda x: x+1, primals, tangents)
    self.assertEqual(out_tangent, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_jit_jvp_of_int(self):
    primals = (2,)
    tangents = (np.zeros(shape=(), dtype=float0),)

    _, out_tangent = api.jvp(jax.jit(lambda x: x+1), primals, tangents)
    self.assertEqual(out_tangent, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_vjp_of_int_index(self):
    primal, fn_vjp = api.vjp(lambda x, i: x[i], np.ones(2)*2, 1)
    tangent_x, tangent_i = fn_vjp(1.)
    self.assertEqual(primal, 2.)
    self.assertAllClose(tangent_x, jnp.array([0., 1.]))
    self.assertEqual(tangent_i, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_vjp_of_int_shapes(self):
    out, fn_vjp = api.vjp(lambda x: lax.reshape(x, (2, 2)), np.ones((4, 1),
                                                                    dtype=int))
    tangent, = fn_vjp(out)
    self.assertArraysEqual(tangent, np.zeros(shape=(4, 1), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_jit_vjp_of_int(self):
    primal, fn_vjp = api.vjp(lambda x, y: x+y, 2, 1)
    tangent_x, tangent_i = jax.jit(fn_vjp)(1)
    self.assertEqual(primal, 3)
    self.assertEqual(tangent_x, np.zeros(shape=(), dtype=float0))
    self.assertEqual(tangent_i, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_vjp_of_int_fulllike(self):
    # Regression test for tangent and cotangent mismatch in convert_element_type
    # transpose rule wrt a ConstVar
    f = lax.full_like
    out, vjp = api.vjp(f, np.zeros((2, 2)), 1)
    self.assertAllClose(out, jnp.ones((2, 2)))
    tangent_x, tangent_y = vjp(out)
    self.assertAllClose(tangent_x, jnp.zeros((2, 2)))
    self.assertEqual(tangent_y, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_grad_of_int(self):
    # Need real-valued output, but testing integer input.
    out = api.grad(lambda x: x+0., allow_int=True)(1)
    self.assertEqual(out, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_grad_of_bool(self):
    def cond(pred):
      return lax.cond(pred, lambda _: 1., lambda _: 2., 1.)
    value, grd = api.value_and_grad(cond, allow_int=True)(True)
    self.assertEqual(value, 1.)
    self.assertEqual(grd, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_grad_of_int_index(self):
    grad_x, grad_i = api.grad(lambda x, i: x[i], argnums=(0, 1),
                              allow_int=True)(np.ones(2), 1)
    self.assertAllClose(grad_x, jnp.array([0., 1.]))
    self.assertEqual(grad_i, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_jit_grad_of_int(self):
    grad_f = api.grad(lambda x, i: x[i], argnums=(0, 1), allow_int=True)
    grad_x, grad_i = jax.jit(grad_f)(np.ones(2), 1)
    self.assertAllClose(grad_x, jnp.array([0., 1.]))
    self.assertEqual(grad_i, np.zeros(shape=(), dtype=float0))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_float0_reshape(self):
    # dtype-agnostic operations are supported
    float0_array = jax.grad(lambda x: jnp.sum(x+0.),
                            allow_int=True)(np.ones((2, 4), dtype=int))

    self.assertArraysEqual(float0_array.reshape((4, 2)),
                           np.zeros((4, 2), dtype=float0))
    self.assertArraysEqual(float0_array.transpose(),
                           np.zeros((4, 2), dtype=float0))

  def test_float0_error(self):
    # float0 is incompatible with other dtypes
    float0_array = jax.grad(lambda x: x+0., allow_int=True)(1)
    error_text = "float0s do not support any operations by design"

    with self.assertRaisesRegex(TypeError, error_text):
      # dispatch via DeviceArray
      _ = float0_array + jnp.zeros(())

    with self.assertRaisesRegex(TypeError, error_text):
      # dispatch via lax
      _ = lax.add(float0_array, jnp.zeros(()))

  def test_grad_complex_result_errors(self):
    dfn = grad(lambda x: x ** 2 + 1j)
    self.assertRaisesRegex(
      TypeError,
      (r"grad requires real-valued outputs \(output dtype that is a "
       r"sub-dtype of np.floating\), but got complex.*"),
      lambda: dfn(3.))

  def test_holomorphic_grad_of_float_errors(self):
    dfn = grad(lambda x: x ** 2, holomorphic=True)
    self.assertRaisesRegex(
      TypeError,
      (r"grad with holomorphic=True requires inputs with complex dtype, "
       r"but got float.*"),
      lambda: dfn(3.))

  def test_holomorphic_jacrev_of_float_errors(self):
    dfn = jacrev(lambda x: x ** 2, holomorphic=True)
    self.assertRaisesRegex(
      TypeError,
      (r"jacrev with holomorphic=True requires inputs with complex dtype, "
       r"but got float.*"),
      lambda: dfn(3.))

  def test_holomorphic_jacfwd_of_float_errors(self):
    dfn = jacfwd(lambda x: x ** 2, holomorphic=True)
    self.assertRaisesRegex(
      TypeError,
      (r"jacfwd with holomorphic=True requires inputs with complex dtype, "
       r"but got float.*"),
      lambda: dfn(3.))

  def test_jacfwd_of_complex_errors(self):
    dfn = jacfwd(lambda x: x ** 2)
    self.assertRaisesRegex(
      TypeError,
      (r"jacfwd requires real-valued inputs \(input dtype that is a "
       r"sub-dtype of np.floating\), but got complex.*"),
      lambda: dfn(3. + 1j))

  def test_xla_computation(self):
    # these tests basically check the examples in the xla_computation docstring

    def e(x):
      return jnp.sin(jnp.cos(x))
    c = api.xla_computation(e)(2.)
    self.assertIn('cosine', c.as_hlo_text())
    self.assertIn('sine', c.as_hlo_text())

    def f(x):
      return x - lax.psum(x, 'i')
    axis_env = [('i', 4)]
    c = api.xla_computation(f, axis_env=axis_env)(2)
    self.assertIn('all-reduce', c.as_hlo_text())
    self.assertIn('replica_groups={{0,1,2,3}}', c.as_hlo_text())

    def g(x):
      rowsum = lax.psum(x, 'i')
      colsum = lax.psum(x, 'j')
      allsum = lax.psum(x, ('i', 'j'))
      return rowsum, colsum, allsum
    axis_env = [('i', 4), ('j', 2)]
    c = api.xla_computation(g, axis_env=axis_env)(5.)
    self.assertIn('all-reduce', c.as_hlo_text())
    self.assertIn('replica_groups={{0,2,4,6},{1,3,5,7}}', c.as_hlo_text())
    self.assertIn('replica_groups={{0,1},{2,3},{4,5},{6,7}}', c.as_hlo_text())
    self.assertIn('replica_groups={{0,1,2,3,4,5,6,7}}', c.as_hlo_text())

    def h(x):
      rowsum = lax.psum(x, 'i', axis_index_groups=[[0, 1], [2, 3]])
      colsum = lax.psum(x, 'j')
      return rowsum, colsum
    axis_env = [('i', 4), ('j', 2)]
    c = api.xla_computation(h, axis_env=axis_env)(5.)
    self.assertIn('all-reduce', c.as_hlo_text())
    self.assertIn('replica_groups={{0,2},{4,6},{1,3},{5,7}}', c.as_hlo_text())
    self.assertIn('replica_groups={{0,1},{2,3},{4,5},{6,7}}', c.as_hlo_text())

  def test_xla_computation_args(self):
    def foo(x, y, z):
      return x + y + z

    c = api.xla_computation(foo)(1., 2., 3.)
    self.assertEqual(len(c.program_shape().parameter_shapes()), 3)

    c = api.xla_computation(foo, tuple_args=True)(1., 2., 3.)
    param_shapes = c.program_shape().parameter_shapes()
    self.assertEqual(len(param_shapes), 1)
    self.assertEqual(param_shapes[0].xla_element_type(),
                     xb.xla_client.PrimitiveType.TUPLE)

  def test_xla_computation_duck_typing(self):
    def foo(x, y, z):
      return x + y + z

    x = jax.ShapeDtypeStruct((), np.float32)
    y = jax.ShapeDtypeStruct((), np.float32)
    z = jax.ShapeDtypeStruct((), np.float32)

    c = api.xla_computation(foo)(x, y, z)
    self.assertEqual(len(c.program_shape().parameter_shapes()), 3)

    c = api.xla_computation(foo, tuple_args=True)(1., 2., 3.)
    param_shapes = c.program_shape().parameter_shapes()
    self.assertEqual(len(param_shapes), 1)
    self.assertEqual(param_shapes[0].xla_element_type(),
                     xb.xla_client.PrimitiveType.TUPLE)

  def test_staging_out_multi_replica(self):
    def f(x):
      return api.pmap(jnp.mean)(x)
    xla_comp = api.xla_computation(f)
    xla_comp(jnp.arange(8)).as_hlo_text()  # doesn't crash

  def test_xla_computation_instantiate_constant_outputs(self):
    def f():
      return jnp.zeros((3, 4))

    xla_comp = api.xla_computation(f)()
    out_shape, = xla_comp.program_shape().result_shape().tuple_shapes()
    self.assertEqual(out_shape.dimensions(), (3, 4))

  def test_xla_computation_static_argnums(self):
    def f(x, y):
      return x + y

    xla_comp = api.xla_computation(f, static_argnums=(1,))(2, 3)
    hlo_text = xla_comp.as_hlo_text()
    self.assertIn("constant(3)", hlo_text)
    # The static arguments should be removed from the function being compiled,
    # thus the function should have only a single argument.
    self.assertIn("parameter.1", hlo_text)
    self.assertNotIn("parameter.2", hlo_text)

  def test_xla_computation_return_shape(self):
    _, shape_tree = api.xla_computation(lambda x: (x + 1, jnp.zeros(2, jnp.float32)),
                                        return_shape=True)(np.int32(1))
    expected = (api.ShapeDtypeStruct(shape=(), dtype=jnp.int32),
                api.ShapeDtypeStruct(shape=(2,), dtype=jnp.float32))
    self.assertEqual(shape_tree, expected)

  def test_xla_computation_partitioned(self):
    def f(x, y):
      return jnp.dot(x, y) + 1

    x = jax.ShapeDtypeStruct((8, 8), np.float32)
    y = jax.ShapeDtypeStruct((8, 16), np.float32)
    xla_comp = api.xla_computation(f, in_parts=(P(2, 2), None),
                                   out_parts=P(4, 1))(x, y)
    hlo_text = xla_comp.as_hlo_text()
    self.assertIn('sharding={devices=[2,2]0,1,2,3}', hlo_text)
    self.assertIn('sharding={replicated}', hlo_text)
    self.assertIn('sharding={{devices=[4,1]0,1,2,3}}', hlo_text)

  def test_xla_computation_replicated_and_partitioned(self):
    def f(x, y):
      return jnp.dot(x, y), lax.psum(x, 'i')

    x = jax.ShapeDtypeStruct((8, 8), np.float32)
    y = jax.ShapeDtypeStruct((8, 16), np.float32)
    axis_env = [('i', 4)]
    xla_comp = api.xla_computation(f, axis_env=axis_env,
                                   in_parts=(P(2, 2), None),
                                   out_parts=(P(4, 1), None))(x, y)
    hlo_text = xla_comp.as_hlo_text()
    self.assertIn('all-reduce', hlo_text)
    self.assertIn('replica_groups={{0,1,2,3}}', hlo_text)
    self.assertIn('sharding={devices=[2,2]0,1,2,3}', hlo_text)
    self.assertIn('sharding={replicated}', hlo_text)
    self.assertIn('sharding={{devices=[4,1]0,1,2,3}, {replicated}}', hlo_text)

  def test_xla_computation_psum_constant(self):
    f = lambda: jax.lax.psum(1, "i")
    api.xla_computation(f, axis_env=[("i", 2)])()  # doesn't crash

  @jtu.skip_on_devices("cpu", "gpu")
  @jtu.ignore_warning(message="Some donated buffers were not usable")
  def test_xla_computation_donate_argnums(self):
    api.xla_computation(lambda x: None, donate_argnums=(0,))(3)  # doesn't crash

  def test_concurrent_device_get_and_put(self):
    def f(x):
      for _ in range(100):
        y = jax.device_put(x)
        x = jax.device_get(y)
      return x

    xs = [np.random.randn(i) for i in range(10)]
    with concurrent.futures.ThreadPoolExecutor() as executor:
      futures = [executor.submit(partial(f, x)) for x in xs]
      ys = [f.result() for f in futures]
    for x, y in zip(xs, ys):
      self.assertAllClose(x, y)

  def test_dtype_warning(self):
    # cf. issue #1230
    if config.x64_enabled:
      raise unittest.SkipTest("test only applies when x64 is disabled")

    def check_warning(warn, nowarn):
      with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        nowarn()  # get rid of extra startup warning

        prev_len = len(w)
        nowarn()
        assert len(w) == prev_len

        warn()
        assert len(w) > 0
        msg = str(w[-1].message)
        expected_prefix = "Explicitly requested dtype "
        self.assertEqual(expected_prefix, msg[:len(expected_prefix)])

        prev_len = len(w)
        nowarn()
        assert len(w) == prev_len

    check_warning(lambda: jnp.array([1, 2, 3], dtype="float64"),
                  lambda: jnp.array([1, 2, 3], dtype="float32"))
    check_warning(lambda: jnp.array([1, 2, 3], dtype="float64"),
                  lambda: jnp.array([1, 2, 3], dtype=float))
    check_warning(lambda: jnp.ones(3, dtype=np.float64),
                  lambda: jnp.ones(3))
    check_warning(lambda: jnp.ones(3, dtype=np.float64),
                  lambda: jnp.ones(3, dtype=float))
    check_warning(lambda: jnp.ones_like(3, dtype=np.int64),
                  lambda: jnp.ones_like(3, dtype=np.int32))
    check_warning(lambda: jnp.zeros(3, dtype="int64"),
                  lambda: jnp.zeros(3, dtype="int32"))
    check_warning(lambda: jnp.zeros_like(3, dtype="float64"),
                  lambda: jnp.zeros_like(3, dtype="float32"))
    check_warning(lambda: jnp.full((2, 3), 1, dtype="int64"),
                  lambda: jnp.full((2, 3), 1))
    check_warning(lambda: jnp.ones(3).astype("float64"),
                  lambda: jnp.ones(3).astype("float32"))
    check_warning(lambda: jnp.eye(3, dtype=np.float64),
                  lambda: jnp.eye(3))
    check_warning(lambda: jnp.arange(3, dtype=np.float64),
                  lambda: jnp.arange(3, dtype=np.float32))
    check_warning(lambda: jnp.linspace(0, 3, dtype=np.float64),
                  lambda: jnp.linspace(0, 3, dtype=np.float32))
    check_warning(lambda: jnp.tri(2, dtype="float64"),
                  lambda: jnp.tri(2, dtype="float32"))
    check_warning(lambda: jnp.arange(1).astype("float64"),
                  lambda: jnp.arange(1).astype(float))
    check_warning(lambda: jnp.arange(1.0).astype("int64"),
                  lambda: jnp.arange(1.0).astype(int))

  def test_error_for_invalid_dtype(self):
    with self.assertRaisesRegex(TypeError, ".*not a valid JAX array type.*"):
      lax.add(jnp.array(7), np.array("hello"))

  def test_vmap_preserves_docstr(self):
    def superfun(a):
      """Does things with stuff."""
      pass

    self.assertRegex(api.vmap(superfun).__doc__, "\n".join([
        "Vectorized version of superfun.*",
        "",
        "Original documentation:",
        "",
        superfun.__doc__,
    ]))

  def test_vmap_in_axes_list(self):
    # https://github.com/google/jax/issues/2367
    dictionary = {'a': 5., 'b': jnp.ones(2)}
    x = jnp.zeros(3)
    y = jnp.arange(3.)


    def f(dct, x, y):
      return dct['a'] + dct['b'] + x + y

    out1 = api.vmap(f, (None, 0, 0))(dictionary, x, y)
    out2 = api.vmap(f, [None, 0, 0])(dictionary, x, y)
    self.assertAllClose(out1, out2)

  def test_vmap_in_axes_tree_prefix_error(self):
    # https://github.com/google/jax/issues/795
    value_tree = jnp.ones(3)
    self.assertRaisesRegex(
        ValueError,
        "vmap in_axes specification must be a tree prefix of the corresponding "
        r"value, got specification \(0, 0\) for value tree "
        + re.escape(f"{tree_util.tree_structure((value_tree,))}."),
        lambda: api.vmap(lambda x: x, in_axes=(0, 0))(value_tree)
    )

  def test_vmap_in_axes_leaf_types(self):
    with self.assertRaisesRegex(
        TypeError, r"vmap in_axes must be an int, None, or .*"):
      api.vmap(lambda x: x, in_axes=(jnp.array([1., 2.]),))(jnp.array([1., 2.]))

  def test_vmap_out_axes_leaf_types(self):
    with self.assertRaisesRegex(
        TypeError, r"vmap out_axes must be an int, None, or .*"):
      api.vmap(lambda x: x, out_axes=(jnp.array([1., 2.]),))(jnp.array([1., 2.]))

  def test_vmap_unbatched_object_passthrough_issue_183(self):
    # https://github.com/google/jax/issues/183
    fun = lambda f, x: f(x)
    vfun = api.vmap(fun, (None, 0))
    ans = vfun(lambda x: x + 1, jnp.arange(3))
    self.assertAllClose(ans, np.arange(1, 4), check_dtypes=False)

  def test_vmap_mismatched_axis_sizes_error_message_issue_705(self):
    # https://github.com/google/jax/issues/705
    def h(a, b):
      return jnp.sum(a) + jnp.sum(b)

    X = np.random.randn(10, 4)
    U = np.random.randn(10, 2)

    with self.assertRaisesRegex(
        ValueError,
        "vmap got inconsistent sizes for array axes to be mapped:\n"
        r"arg 0 has shape \(10, 4\) and axis 0 is to be mapped" "\n"
        r"arg 1 has shape \(10, 2\) and axis 1 is to be mapped" "\n"
        "so\n"
        "arg 0 has an axis to be mapped of size 10\n"
        "arg 1 has an axis to be mapped of size 2"):
      api.vmap(h, in_axes=(0, 1))(X, U)

    with self.assertRaisesRegex(
        ValueError,
        "vmap got inconsistent sizes for array axes to be mapped:\n"
        r"arg 0 has shape \(10, 4\) and axis 0 is to be mapped" "\n"
        r"arg 1 has shape \(10, 2\) and axis 1 is to be mapped" "\n"
        r"arg 2 has shape \(10, 4\) and axis 0 is to be mapped" "\n"
        "so\n"
        "args 0, 2 have axes to be mapped of size 10\n"
        "arg 1 has an axis to be mapped of size 2"):
      api.vmap(lambda x, y, z: None, in_axes=(0, 1, 0))(X, U, X)

    with self.assertRaisesRegex(
        ValueError,
        "vmap got inconsistent sizes for array axes to be mapped:\n"
        "the tree of axis sizes is:\n"
        r"\(10, \[2, 2\]\)"):
      api.vmap(h, in_axes=(0, 1))(X, [U, U])

    error = (r"vmap was requested to map its argument along axis 0, which "
             r"implies that its rank should be at least 1, but is only 0 "
             r"\(its shape is \(\)\)")
    with self.assertRaisesRegex(ValueError, error):
      # The mapped inputs cannot be scalars
      api.vmap(lambda x: x)(1.)

    with self.assertRaisesRegex(
        ValueError, "vmap must have at least one non-None value in in_axes"):
      # If the output is mapped, there must be a non-None in_axes
      api.vmap(lambda x: x, in_axes=None)(jnp.array([1., 2.]))

    error = (r"vmap was requested to map its argument along axis 1, which "
             r"implies that its rank should be at least 2, but is only 1 "
             r"\(its shape is \(2,\)\)")
    with self.assertRaisesRegex(ValueError, error):
      api.vmap(lambda x: x, in_axes=1)(jnp.array([1., 2.]))

    # Error is: TypeError: only integer scalar arrays can be converted to a scalar index
    with self.assertRaisesRegex(
        ValueError,
        "vmap out_axes specification must be a tree prefix of the "
        "corresponding value.*"):
      api.vmap(lambda x: x, in_axes=0, out_axes=(2, 3))(jnp.array([1., 2.]))

    with self.assertRaisesRegex(
        ValueError,
        r"vmap has mapped output \(axis_name=foo\) but out_axes is None"):
      # If the output is mapped (user-named axis), then there must be some
      # out_axes specified.
      api.vmap(lambda x: x, out_axes=None, axis_name="foo")(jnp.array([1., 2.]))

    with self.assertRaisesRegex(
        ValueError,
        "vmap has mapped output but out_axes is None"):
      # If the output is mapped (unnamed axis), then there must be some out_axes
      # specified.
      api.vmap(lambda x: x, out_axes=None)(jnp.array([1., 2.]))

  def test_vmap_structured_in_axes(self):

    A, B, C, D = 2, 3, 4, 5
    K = 6  # batch size
    x = np.ones((K, A, B))  # batch axis in different locations
    y = np.ones((B, K, C))
    z = np.ones((C, D, K))

    def foo(tree_arg):
      x, (y, z) = tree_arg
      return jnp.dot(x, jnp.dot(y, z))

    tree = (x, (y, z))
    vfoo = api.vmap(foo, in_axes=((0, (1, 2)),))
    self.assertEqual(vfoo(tree).shape, (6, 2, 5))

    Point = collections.namedtuple("Point", ["x", "y"])
    tree = (x, Point(y, z))
    vfoo = api.vmap(foo, in_axes=((0, Point(1, 2)),))
    self.assertEqual(vfoo(tree).shape, (6, 2, 5))

    def foo(tree_arg):
      x, dct = tree_arg
      y, z = dct['a'], dct['b']
      return jnp.dot(x, jnp.dot(y, z))

    tree = (x, {'a': y, 'b': z})
    vfoo = api.vmap(foo, in_axes=((0, {'a': 1, 'b': 2}),))
    self.assertEqual(vfoo(tree).shape, (6, 2, 5))

    tree = (x, collections.OrderedDict([('a', y), ('b', z)]))
    vfoo = api.vmap(
        foo, in_axes=((0, collections.OrderedDict([('a', 1), ('b', 2)])),))
    self.assertEqual(vfoo(tree).shape, (6, 2, 5))

  def test_vmap_in_axes_bool_error(self):
    # https://github.com/google/jax/issues/6372
    with self.assertRaisesRegex(TypeError, "must be an int"):
      api.vmap(lambda x: x, in_axes=False)(jnp.zeros(3))

  def test_pmap_in_axes_bool_error(self):
    # https://github.com/google/jax/issues/6372
    with self.assertRaisesRegex(TypeError, "must be an int"):
      api.pmap(lambda x: x, in_axes=False)(jnp.zeros(1))

  def test_pmap_global_cache(self):
    def f(x, y):
      return x, y

    x = np.ones((1, 1, 1))

    # All defaults
    with jtu.assert_num_jit_and_pmap_compilations(1):
      for _ in range(2):
        api.pmap(f)(x, x)

    # With axis name
    with jtu.assert_num_jit_and_pmap_compilations(1):
      for _ in range(2):
        api.pmap(f, 'i')(x, x)

    # With in_axes and out_axes
    for x_in, y_in, x_out, y_out in it.product(*((0, 1, 2) for _ in range(4))):
      with jtu.assert_num_jit_and_pmap_compilations(1):
        for _ in range(2):
          api.pmap(f, 'i', in_axes=(x_in, y_in), out_axes=(x_out, y_out))(x, x)

    # Forward-mode AD on the outside
    with jtu.assert_num_jit_and_pmap_compilations(1):
      for _ in range(2):
        api.jvp(api.pmap(f), (x, x), (x, x))

    # Reverse-mode AD on the outside. One compilation for forward, one for backward.
    with jtu.assert_num_jit_and_pmap_compilations(2):
      for _ in range(2):
        api.vjp(api.pmap(f), x, x)[1]((x, x))

  def test_device_array_repr(self):
    rep = jnp.ones(()) + 1.
    self.assertStartsWith(repr(rep), "DeviceArray")

  def test_device_array_hash(self):
    rep = jnp.ones(()) + 1.
    self.assertIsInstance(rep, jax.interpreters.xla.DeviceArray)
    self.assertNotIsInstance(rep, collections.Hashable)
    with self.assertRaisesRegex(TypeError, 'unhashable type'):
      hash(rep)

  def test_grad_without_enough_args_error_message(self):
    # https://github.com/google/jax/issues/1696
    def f(x, y): return x + y
    df = api.grad(f, argnums=0)
    self.assertRaisesRegex(
        TypeError,
        "differentiating with respect to argnums=0 requires at least 1 "
        "positional arguments to be passed by the caller, but got only 0 "
        "positional arguments.",
        lambda: partial(df, x=0.)(y=1.))

  def test_grad_of_jit_compilation_caching(self):
    if not hasattr(self, "assertLogs"):
      raise unittest.SkipTest("test requires assertLogs (python 3)")

    lax.add(1, 2)  # make sure some initial warnings are already printed

    sin = api.jit(jnp.sin)

    prev_level = logging.get_verbosity()
    try:
      logging.set_verbosity('DEBUG')
      with self.assertLogs(level=logging.DEBUG) as l:
        ans1 = api.grad(sin)(2.)
        ans2 = api.grad(sin)(3.)
    finally:
      logging.set_verbosity(prev_level)
    self.assertLen(l.output, 2)

    self.assertAllClose(ans1, np.cos(2.), check_dtypes=False)
    self.assertAllClose(ans2, np.cos(3.), check_dtypes=False)

  def test_trivial_computations(self):
    x = jnp.array([1, 2, 3])
    y = api.jit(lambda x: x)(x)
    self.assertIs(x, y)

    z1, z2 = api.jit(lambda x: (x, x))(x)
    self.assertIs(z1, z2)

    x1, x2 = jnp.array([1, 2]), jnp.array([2, 3])
    z1, z2, z3 = api.jit(lambda x, y: (y, 1, x))(x1, x2)
    self.assertIs(z1, x2)
    self.assertIs(z3, x1)
    self.assertEqual(z2, 1)

  def test_nested_jit_hoisting(self):
    @api.jit
    def f(x, y):
      z = 2 * x
      return y + z, 3

    @api.jit
    def g(x):
      return f(2, x)

    jaxpr_subcomp = xla.jaxpr_subcomp

    jaxprs = []
    def jaxpr_subcomp_and_collect(c, jaxpr, *args, **kwargs):
      jaxprs.append(jaxpr)
      return jaxpr_subcomp(c, jaxpr, *args, **kwargs)

    try:
      xla.jaxpr_subcomp = jaxpr_subcomp_and_collect
      ans = g(3)
    finally:
      xla.jaxpr_subcomp = jaxpr_subcomp

    self.assertEqual(ans, (7, 3))
    self.assertLen(jaxprs, 2)
    outer_jaxpr, inner_jaxpr = jaxprs

    self.assertLen(outer_jaxpr.eqns, 1)
    self.assertEqual(outer_jaxpr.eqns[0].primitive.name, 'xla_call')
    subjaxpr_1 = outer_jaxpr.eqns[0].params["call_jaxpr"]
    self.assertEqual(str(subjaxpr_1), str(inner_jaxpr))
    self.assertLen(inner_jaxpr.eqns, 2)
    self.assertEqual(inner_jaxpr.eqns[-2].primitive.name, 'mul')
    self.assertEqual(inner_jaxpr.eqns[-1].primitive.name, 'add')

  def test_primitive_compilation_cache(self):
    with jtu.count_primitive_compiles() as count:
      lax.add(1, 2)
      lax.add(2, 3)
    self.assertEqual(count[0], 1)

  def test_arange_jit(self):
    # see https://github.com/google/jax/issues/553
    def fun(x):
      r = jnp.arange(x.shape[0])[x]
      return r

    jit(fun)(jnp.array([0, 1, 2], dtype=jnp.int32))  # doesn't crash

  def helper_save_tracer(self, x):
    self._saved_tracer = x
    return x

  def test_escaped_tracers_different_top_level_traces(self):
    api.jit(self.helper_save_tracer)(0.)
    with self.assertRaisesRegex(
        core.UnexpectedTracerError, "Encountered an unexpected tracer"):
      api.jit(lambda x: self._saved_tracer)(0.)

  def test_escaped_tracers_cant_lift_sublevels(self):
    api.jit(self.helper_save_tracer)(0.)
    with self.assertRaisesRegex(
        core.UnexpectedTracerError,
        re.compile(
          "Encountered an unexpected tracer",
          re.DOTALL)):
      api.jit(lambda x: x)(self._saved_tracer)

  def test_escaped_tracers_tracer_from_higher_level(self):
    api.grad(self.helper_save_tracer)(0.)
    with self.assertRaisesRegex(
        core.UnexpectedTracerError,
        re.compile(
          "Encountered an unexpected tracer.*Tracer from a higher level",
          re.DOTALL)):
      api.grad(lambda x: x)(self._saved_tracer)

  def test_escaped_tracers_incompatible_sublevel(self):
    def func1(x):
      api.jit(self.helper_save_tracer)(0.)
      # Use the tracer
      return x + self._saved_tracer
    with self.assertRaisesRegex(
        core.UnexpectedTracerError,
        re.compile("Encountered an unexpected tracer",
                   re.DOTALL)):
      api.jit(func1)(2.)

  def test_escaped_tracers_cant_lift(self):
    def func1(x):
      api.grad(self.helper_save_tracer)(0.)
      return x + self._saved_tracer
    with self.assertRaisesRegex(
        core.UnexpectedTracerError,
        re.compile("Encountered an unexpected tracer.*Can't lift",
                   re.DOTALL)):
      api.grad(func1)(2.)

  def test_escaped_tracers_not_among_input_tracers(self):
    def func1(x):
      api.grad(self.helper_save_tracer)(x)
      # Use the tracer
      return x + self._saved_tracer

    with self.assertRaisesRegex(
        core.UnexpectedTracerError,
        re.compile(
          "Encountered an unexpected tracer.*Tracer not among input tracers",
          re.DOTALL)):
      api.jit(func1)(2.)

  def test_escaped_tracer_omnistaging(self):
    count = 1

    @jit
    def f():
      nonlocal count
      count = jnp.add(count, 1)
    f()  # leaked a tracer! but currently undetected

    def f(x, c):
      jnp.add(count, 1)
      return None, None

    @jit
    def g():
      lax.scan(f, None, None, length=2)

    with self.assertRaisesRegex(core.UnexpectedTracerError,
                                "was created on line"):
      g()

  def test_escaped_tracer_omnistaging_top_trace(self):
    count = 1

    def f(_, __):
      nonlocal count
      count = jnp.add(count, 1)
      return None, None

    lax.scan(f, None, None, length=2)  # leaked a tracer! (of level 1!)

    with self.assertRaisesRegex(core.UnexpectedTracerError,
                                "was created on line"):
      # The following call will try and raise the ones array to the count tracer
      # level, which is no longer live.
      jax.jit(jnp.add)(jnp.ones(()), count)

  def test_escaped_tracer_transform_name(self):
    with self.assertRaisesRegex(core.UnexpectedTracerError,
                                "for jit"):
      jax.jit(self.helper_save_tracer)(1)
      _ = self._saved_tracer+1

    with self.assertRaisesRegex(core.UnexpectedTracerError,
                                "for pmap"):
      jax.pmap(self.helper_save_tracer)(jnp.ones((1, 2)))
      _ = self._saved_tracer+1

    with self.assertRaisesRegex(core.UnexpectedTracerError,
                                "for eval_shape"):
      jax.eval_shape(self.helper_save_tracer, 1)
      _ = self._saved_tracer+1

  def test_escaped_tracer_shape_dtype(self):
    with self.assertRaisesRegex(core.UnexpectedTracerError,
                                r"shape \(4, 3\) and dtype int32"):
      jax.jit(self.helper_save_tracer)(jnp.ones((4, 3), dtype=jnp.int32))
      _ = self._saved_tracer+1

  def test_pmap_static_kwarg_error_message(self):
    # https://github.com/google/jax/issues/3007
    def f(a, b):
      return a + b

    g = jax.pmap(f, static_broadcasted_argnums=(1,))

    msg = (r"pmapped function has static_broadcasted_argnums=\(1,\) but was "
           r"called with only 1 positional argument. All static broadcasted "
           r"arguments must be passed positionally.")
    with self.assertRaisesRegex(ValueError, msg):
      g(jnp.ones((1, 1)), b=1)

  def test_vmap_unmapped_last(self):
    @partial(jax.vmap, out_axes=-1)
    def f(x):
      return np.zeros((2,))
    f(np.zeros((5,)))

  # TODO(jakevdp): re-enable this if possible.
  @unittest.skipIf(True, "broken by convert_element_type change.")
  def test_xla_constant_dedup(self):
    y = np.array([7, 14], dtype=np.float32)
    def f(x):
      return x + y + y

    x = np.array([1, 2], dtype=np.float32)
    hlo_lines = jax.xla_computation(f)(x).as_hlo_text().split('\n')
    hlo_lines = set([s.strip() for s in hlo_lines])
    self.assertIn('constant.1 = f32[2]{0} constant({7, 14})', hlo_lines)
    self.assertNotIn('constant.2 = f32[2]{0} constant({7, 14})', hlo_lines)

  def test_eval_context(self):
    @jit
    def f():
      with core.eval_context():
        assert jnp.add(1, 1) == 2

    f()  # doesn't crash

  def test_concrete_error_because_arg_unary(self):
    @jax.jit
    def f(x):
      if x > 0:
        return x
      else:
        return 0

    msg = r"on the value of the argument 'x'"
    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f(1)

  def test_concrete_error_because_arg_binary(self):
    @jax.jit
    def f(x, y):
      if x > y:
        return x
      else:
        return y

    msg = r"on the values of the arguments 'x' and 'y'"
    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f(1, 2)

  def test_concrete_error_because_arg_ternary(self):
    @jax.jit
    def f(x, y, z):
      if x > z:
        return x
      else:
        return y

    msg = r"on the values of the arguments 'x' and 'z'"
    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f(1, 2, 3)

    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f(1, 2, z=3)

    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f(1, y=2, z=3)

  def test_concrete_error_because_arg_varargs(self):
    @jax.jit
    def f(*args):
      x, y, z = args
      if x > z:
        return x
      else:
        return y

    msg = r"on the values of the argument 'args'"
    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f(1, 2, 3)

  def test_concrete_error_because_arg_kwargs(self):
    @jax.jit
    def f(**kwargs):
      x, y, z = kwargs['x'], kwargs['y'], kwargs['z']
      if x > z:
        return x
      else:
        return y

    msg = r"on the values of the argument 'kwargs'"
    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f(x=1, y=2, z=3)

  def test_concrete_error_because_arg_pytree(self):
    @jax.jit
    def f(xy, z):
      x, y = xy
      if x > 0:
        return x
      else:
        return y

    msg = r"on the value of the argument 'xy'"
    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f((1, 2), z=3)

  def test_concrete_error_because_const(self):
    @jax.jit
    def f():
      assert jnp.add(1, 1) > 0

    msg = "on these lines"
    with self.assertRaisesRegex(core.ConcretizationTypeError, msg):
      f()

  def test_xla_computation_zeros_doesnt_device_put(self):
    with jtu.count_device_put() as count:
      api.xla_computation(lambda: jnp.zeros(3))()
    self.assertEqual(count[0], 0)

  def test_join_concrete_arrays_with_omnistaging(self):
    # https://github.com/google/jax/issues/4622
    x = jnp.array([1., 2., 3.])
    y = jnp.array([1., 2., 4.])

    @jit
    def f():
      core.lattice_join(core.ConcreteArray(x), core.ConcreteArray(y))

    f()  # doesn't crash

  def test_linearize_aval_error(self):
    # https://github.com/google/jax/issues/4622
    f = lambda x: x

    # these should not error
    _, f_jvp = api.linearize(f, 1.)
    f_jvp(1.)
    _, f_jvp = api.linearize(f, np.ones(2, np.int32))
    f_jvp(np.zeros(2, float0))

    # these should error
    _, f_jvp = api.linearize(f, 1.)
    with self.assertRaisesRegex(ValueError, "tangent values inconsistent"):
      f_jvp(1)
    _, f_jvp = api.linearize(f, np.ones(2, np.int32))
    with self.assertRaisesRegex(ValueError, "tangent values inconsistent"):
      f_jvp(np.ones(2, np.int32))

  def test_grad_of_token_consuming_primitive(self):
    # https://github.com/google/jax/issues/5463
    tokentest_p = core.Primitive("tokentest")
    tokentest_p.def_impl(partial(xla.apply_primitive, tokentest_p))
    tokentest_p.def_abstract_eval(lambda x, y: x)
    xla.translations[tokentest_p] = lambda c, x, y:  x
    ad.defjvp(tokentest_p, (lambda g, x, token: x), None)

    token = jax.lax.create_token(123)
    arr = jnp.ones((3, 2))
    res, vjp_fun = jax.vjp(lambda x: tokentest_p.bind(x, token), arr)
    # Should not crash.
    vjp_fun(arr)

  def test_jit_returning_token(self):
    x = jax.jit(jax.lax.create_token)(1.0)
    self.assertIsInstance(x, jax.interpreters.xla.Token)

  def test_leak_checker_catches_a_jit_leak(self):
    with jax.checking_leaks():
      lst = []

      @jit
      def f(x):
        lst.append(x)
        return x

      with self.assertRaisesRegex(Exception, r"Leaked"):
        f(3)

  def test_leak_checker_catches_a_pmap_leak(self):
    with jax.checking_leaks():
      lst = []

      @api.pmap
      def f(x):
        lst.append(x)
        return x

      with self.assertRaisesRegex(Exception, r"Leaked"):
        f(np.ones(1))

  def test_leak_checker_catches_a_grad_leak(self):
    with jax.checking_leaks():
      lst = []

      def f(x):
        lst.append(x)
        return x

      with self.assertRaisesRegex(Exception, r"Leaked trace"):
        api.grad(f)(3.)

  def test_leak_checker_avoids_false_positives(self):
    with jax.checking_leaks():
      @jit
      def f(x):
        return x
      f(3)  # doesn't crash
      api.vmap(f)(np.arange(3))  # doesn't crash
      api.grad(f)(3.)  # doesn't crash

      @api.pmap
      def f(x):
        return x
      f(np.ones(1))  # doesn't crash
      api.vmap(f)(np.ones((1, 1)))  # doesn't crash

  def test_leak_checker_catches_a_scan_leak(self):
    with jax.checking_leaks():
      lst = []

      to_scan = lambda c, x: (lst.append(c) or jnp.sin(c), None)

      with self.assertRaisesRegex(Exception, r"Leaked trace"):
        lax.scan(to_scan, 1., np.arange(3.))

  def test_leak_checker_avoids_false_positives_scan(self):
    with jax.checking_leaks():
      to_scan = lambda c, x: (jnp.sin(c), None)
      lax.scan(to_scan, 1., np.arange(3.))  # doesn't crash

  def test_leak_checker_avoids_false_positives_scan_jvp(self):
    with jax.checking_leaks():
      to_scan = lambda c, x: (c, None)

      def f(x):
        lax.scan(to_scan, x, None, length=1)
      api.jvp(f, (3.,), (1.,))  # doesn't crash

  def test_leak_checker_avoids_false_positives_scan_vmap(self):
    with jax.checking_leaks():
      to_scan = lambda c, _: (1., None)

      @api.vmap
      def f(x):
        lax.scan(to_scan, x, None, length=1)
      f(np.arange(5.))  # doesn't crash

  def test_leak_checker_avoids_false_positives_scan_vmap_2(self):
    with jax.checking_leaks():
      to_scan = lambda c, _: (c, None)

      @api.vmap
      def f(x):
        lax.scan(to_scan, x, None, length=1)
      f(np.arange(5.))  # doesn't crash

  def test_leak_checker_catches_a_sublevel_leak(self):
    with jax.checking_leaks():
      @jit
      def f(x):
        lst = []
        @jit
        def g(x):
          lst.append(x)
          return x

        x = g(x)
        return x

      with self.assertRaisesRegex(Exception, r"Leaked sublevel"):
        f(3)

  def test_default_backend(self):
    first_local_device = api.local_devices()[0]
    self.assertEqual(first_local_device.platform, api.default_backend())

  def test_dunder_jax_array(self):
    # https://github.com/google/jax/pull/4725

    class AlexArray:
      def __init__(self, jax_val):
        self.jax_val = jax_val
      def __jax_array__(self):
        return self.jax_val
      dtype = property(lambda self: self.jax_val.dtype)
      shape = property(lambda self: self.jax_val.shape)

    x = AlexArray(jnp.array([1., 2., 3.]))
    y = jnp.sin(x)
    self.assertAllClose(y, jnp.sin(jnp.array([1., 2., 3.])))
    y = api.grad(api.jit(lambda x: jnp.sin(x).sum()))(x)
    self.assertAllClose(y, jnp.cos(jnp.array([1., 2., 3.])))

    x = AlexArray(jnp.array([[1., 2., 3.]]))
    y = api.pmap(jnp.sin)(x)
    self.assertAllClose(y, jnp.sin(jnp.array([[1., 2., 3.]])))

    x = jnp.array(1)
    a = AlexArray(x)
    for f in [jnp.isscalar, jnp.size, jnp.shape, jnp.dtype]:
      self.assertEqual(f(x), f(a))

  def test_constant_handler_mro(self):
    # https://github.com/google/jax/issues/6129

    class Foo(enum.IntEnum):
      bar = 1

    @api.pmap
    def f(_):
      return Foo.bar

    ans = f(jnp.arange(1))  # doesn't crash
    expected = jnp.arange(1) + 1
    self.assertAllClose(ans, expected)

  def test_large_python_int_to_float(self):
    # https://github.com/google/jax/pull/6165
    jnp.multiply(2 ** 100, 3.)  # doesn't crash
    out = lax.convert_element_type(2 ** 100, jnp.float32)  # doesn't crash
    self.assertArraysEqual(out, np.float32(2 ** 100))

  def test_dot_precision_context_manager(self):
    x = jnp.zeros((2, 2))

    with jax.default_matmul_precision(None):
      jnp.dot(x, x)  # doesn't crash
      jaxpr = jax.make_jaxpr(jnp.dot)(x, x)
    self.assertIn('precision=None', str(jaxpr))

    with jax.default_matmul_precision("bfloat16"):
      x @ x  # doesn't crash
      jaxpr = jax.make_jaxpr(op.matmul)(x, x)
    self.assertIn('Precision.DEFAULT', str(jaxpr))

    with jax.default_matmul_precision("tensorfloat32"):
      jnp.dot(x, x)  # doesn't crash
      jaxpr = jax.make_jaxpr(jnp.dot)(x, x)
    self.assertIn('Precision.HIGH', str(jaxpr))

    with jax.default_matmul_precision("float32"):
      jnp.dot(x, x)  # doesn't crash
      jaxpr = jax.make_jaxpr(jnp.dot)(x, x)
    self.assertIn('Precision.HIGHEST', str(jaxpr))

    dot = partial(jnp.dot, precision=lax.Precision.HIGHEST)
    with jax.default_matmul_precision("tensorfloat32"):
      dot(x, x)  # doesn't crash
      jaxpr = jax.make_jaxpr(dot)(x, x)
    self.assertIn('Precision.HIGHEST', str(jaxpr))

  def test_dot_precision_flag(self):
    x = jnp.zeros((2, 2))

    prev_val = config._read("jax_default_matmul_precision")
    try:
      config.FLAGS.jax_default_matmul_precision = "tensorfloat32"
      jnp.dot(x, x)  # doesn't crash
      jaxpr = jax.make_jaxpr(jnp.dot)(x, x)
    finally:
      config.FLAGS.jax_default_matmul_precision = prev_val
    self.assertIn('Precision.HIGH', str(jaxpr))
    self.assertEqual(prev_val, config._read("jax_default_matmul_precision"))

    prev_val = config._read("jax_default_matmul_precision")
    try:
      config.update('jax_default_matmul_precision','tensorfloat32')
      jnp.dot(x, x)  # doesn't crash
      jaxpr = jax.make_jaxpr(jnp.dot)(x, x)
    finally:
      config.update('jax_default_matmul_precision', prev_val)
    self.assertIn('Precision.HIGH', str(jaxpr))
    self.assertEqual(prev_val, config._read("jax_default_matmul_precision"))

  def test_dot_precision_forces_retrace(self):
    num_traces = 0

    def g(x):
      nonlocal num_traces
      num_traces += 1
      return jnp.dot(x, x)
    def f_cond(x):
      return lax.cond(True, g, g, x)

    @jax.jit
    def f_jit(x):
      nonlocal num_traces
      num_traces += 1
      return jnp.dot(x, x)

    for f in [f_jit, f_cond]:
      precision = config.jax_default_matmul_precision
      try:
        num_traces = 0
        x = jnp.zeros((2, 2))
        f(x)
        self.assertEqual(num_traces, 1)
        f(x)
        self.assertEqual(num_traces, 1)
        with jax.default_matmul_precision("tensorfloat32"):
          f(x)
          self.assertEqual(num_traces, 2)
          FLAGS.jax_default_matmul_precision = "float32"
          f(x)
          self.assertGreaterEqual(num_traces, 2)
        nt = num_traces
        f(x)
        self.assertEqual(num_traces, nt + 1)
        f(x)
        self.assertEqual(num_traces, nt + 1)
      finally:
        FLAGS.jax_default_matmul_precision = precision

  def test_rank_promotion_forces_retrace(self):
    num_traces = 0

    def g(x):
      nonlocal num_traces
      num_traces += 1
      return x + x
    def f_cond(x):
      return lax.cond(True, g, g, x)

    @jax.jit
    def f_jit(x):
      nonlocal num_traces
      num_traces += 1
      return x + x

    for f in [f_jit, f_cond]:
      allow_promotion = config.jax_numpy_rank_promotion
      try:
        num_traces = 0
        @jax.jit
        def f(x):
          nonlocal num_traces
          num_traces += 1
          return x + x
        x = jnp.zeros((2, 2))
        f(x)
        self.assertEqual(num_traces, 1)
        f(x)
        self.assertEqual(num_traces, 1)
        with jax.numpy_rank_promotion("warn"):
          f(x)
          self.assertEqual(num_traces, 2)
          FLAGS.jax_numpy_rank_promotion = "raise"
          f(x)
          self.assertGreaterEqual(num_traces, 2)
        nt = num_traces
        f(x)
        self.assertEqual(num_traces, nt + 1)
        f(x)
        self.assertEqual(num_traces, nt + 1)
      finally:
        FLAGS.jax_numpy_rank_promotion = allow_promotion

  def test_backward_pass_ref_dropping(self):
    refs = []

    @api.custom_vjp
    def f(x):
      return x
    def f_fwd(x):
      return x, None
    def f_rev(_, g):
      assert len(refs) != 2 or refs[0]() is None
      zero = np.zeros(())
      refs.append(weakref.ref(zero))
      return (zero,)
    f.defvjp(f_fwd, f_rev)

    api.grad(lambda x: f(f(f(x))))(1.)

  def test_custom_vjp_scan_batching_edge_case(self):
    # https://github.com/google/jax/issues/5832
    @jax.custom_vjp
    def mul(x, coeff): return x * coeff
    def mul_fwd(x, coeff): return mul(x, coeff), (x, coeff)
    def mul_bwd(res, g):
        x, coeff = res
        g_x = g * coeff
        g_coeff = (x * g).sum()
        return g_x, g_coeff
    mul.defvjp(mul_fwd, mul_bwd)

    def scan_over_mul(x, coeff):
        def f_(x, t):
            return mul(x, coeff), None
        y, _ = jax.lax.scan(f_, x, jnp.arange(3))
        return y

    key = jax.random.PRNGKey(0)
    key1, key2 = jax.random.split(key, 2)
    x_batch = jax.random.normal(key1, (3, 2))
    covector_batch = jax.random.normal(key2, (3, 2))
    coeff = jnp.array(1.)

    batched_scan_over_mul = jax.vmap(scan_over_mul, in_axes=(0, None), out_axes=0)
    res, vjp_fun = jax.vjp(batched_scan_over_mul, x_batch, coeff)
    vjp_fun(covector_batch)  # doesn't crash

    jtu.check_grads(batched_scan_over_mul, (x_batch, coeff), order=2,
                    modes=['rev'])

  def test_jit_inline(self):
    @partial(api.jit, inline=False)
    def f(x):
      return x * 2

    jaxpr = api.make_jaxpr(f)(3)
    self.assertIn('xla_call', str(jaxpr))

    @partial(api.jit, inline=True)
    def f(x):
      return x * 2

    jaxpr = api.make_jaxpr(f)(3)
    self.assertNotIn('xla_call', str(jaxpr))

  # Repro for https://github.com/google/jax/issues/7229.
  def test_compute_with_large_transfer(self):
    def f(x, delta):
      return x + jnp.asarray(delta, x.dtype)

    # A large and potentially unaligned array to trigger non-zero-copy and
    # async device array copy.
    xs = np.random.uniform(0., 1., size=(10, 131, 111, 3)).astype(np.float32)
    for x in xs:
      delta = np.random.uniform(-0.5, 0.5, size=())
      jitted_f = api.jit(f)
      np.testing.assert_allclose(jitted_f(x, delta), f(x, delta))


class RematTest(jtu.JaxTestCase):

  def test_remat_basic(self):
    @api.remat
    def g(x):
      return lax.sin(lax.sin(x)), 3.

    def f(x):
      x, _ = g(x)
      return x

    ans = f(2.)
    expected = np.sin(np.sin(2.))
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans, f_lin = api.linearize(f, 2.)
    expected = np.sin(np.sin(2.))
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = f_lin(3.)
    expected = np.cos(np.sin(2.)) * np.cos(2.) * 3.
    self.assertAllClose(ans, expected, check_dtypes=False)

    sin_calls = []
    cos_calls = []
    sin_impl = lax.sin_p.impl
    cos_impl = lax.cos_p.impl
    try:
      lax.sin_p.def_impl(lambda x: sin_calls.append(1) or sin_impl(x))
      lax.cos_p.def_impl(lambda x: cos_calls.append(1) or cos_impl(x))
      f_lin(3.)
    finally:
      lax.sin_p.def_impl(sin_impl)
      lax.cos_p.def_impl(cos_impl)
    self.assertEqual(len(sin_calls), 1)
    self.assertEqual(len(cos_calls), 2)

  def test_remat_freevars(self):
    def f1(x):
      y = 2 * jnp.sin(x)
      z = jnp.cos(x) * jnp.sin(y)
      return z

    def f2(x):
      y = 2 * jnp.sin(x)
      z = api.remat(lambda x: jnp.cos(x) * jnp.sin(y))(x)
      return z

    ans, f_lin = api.linearize(f2, 2.)
    expected, f_lin_expected = api.linearize(f1, 2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = f_lin(3.)
    expected = f_lin_expected(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_remat_grad_python_control_flow(self):
    @partial(api.remat, concrete=True)
    def g(x):
      if x > 0:
        return lax.sin(x), 3.
      else:
        return lax.cos(x), 4.

    def f(x):
      x, _ = g(x)
      return x

    ans = f(2.)
    expected = np.sin(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(f)(2.)
    expected = np.cos(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_remat_jit(self):
    @api.remat
    def g(x):
      return lax.sin(lax.sin(x))

    def f_(x):
      return g(x)
    f = api.jit(f_)

    ans = f(2.)
    expected = np.sin(np.sin(2.))
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(f)(2.)
    expected = np.cos(np.sin(2.)) * np.cos(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jit(api.grad(f_))(2.)
    expected = np.cos(np.sin(2.)) * np.cos(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_remat_vmap(self):
    @api.remat
    def g(x):
      return lax.sin(lax.sin(x))

    x = np.arange(3.)

    ans = api.vmap(g)(x)
    expected = np.sin(np.sin(x))
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jacfwd(g)(x)
    expected = np.diag(np.cos(np.sin(x)) * np.cos(x))
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jacrev(g)(x)
    expected = np.diag(np.cos(np.sin(x)) * np.cos(x))
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_remat_higher_order_autodiff(self):
    def f(x):
      return lax.cos(lax.sin(x))
    g = api.remat(f)

    ans = api.grad(api.grad(g))(3.)
    expected = api.grad(api.grad(f))(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_remat_scan(self):
    to_scan = lambda c, x: (jnp.sin(c), None)

    def f_noremat(x):
      y, _ = lax.scan(to_scan, x, np.arange(3.))
      return y

    def f_yesremat(x):
      y, _ = lax.scan(api.remat(to_scan), x, np.arange(3.))
      return y

    ans = f_yesremat(4.)
    expected = f_noremat(4.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(f_yesremat)(4.)
    expected = api.grad(f_noremat)(4.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    jaxpr = api.make_jaxpr(api.linearize(f_yesremat, 4.)[1])(1.)
    scan_eqn, = jaxpr.jaxpr.eqns
    self.assertIn(' cos ', str(scan_eqn.params['jaxpr']))

    jaxpr = api.make_jaxpr(api.vjp(f_yesremat, 4.)[1])(1.)
    scan_eqn, = jaxpr.jaxpr.eqns
    self.assertIn(' cos ', str(scan_eqn.params['jaxpr']))

  def test_remat_no_redundant_flops(self):
    # see https://github.com/google/jax/pull/1749#issuecomment-558267584

    @api.jit
    def g(x):
      return f(2., x)

    @api.remat
    def f(x, y):
      return jnp.sin(x) * y

    # We swap out sin_p's impl rule to count how many times it's invoked
    called = []
    sin_impl = lax.sin_p.impl
    try:
      lax.sin_p.def_impl(lambda x: called.append(1) or sin_impl(x))
      api.grad(g)(3.)
    finally:
      lax.sin_p.def_impl(sin_impl)
    num_calls = len(called)
    self.assertLessEqual(num_calls, 1)

  def test_remat_binomial_checkpointing(self):
    def binom_checkpoint(funs):
      if len(funs) == 1:
        return funs[0]
      else:
        f1 = binom_checkpoint(funs[:len(funs)//2])
        f2 = binom_checkpoint(funs[len(funs)//2:])
        return api.remat(lambda x: f1(f2(x)))

    f1 = binom_checkpoint([jnp.sin, jnp.sin, jnp.sin, jnp.sin])
    f2 = lambda x: jnp.sin(jnp.sin(jnp.sin(jnp.sin(x))))
    x = 4.
    self.assertAllClose(f1(x), f2(x), check_dtypes=False)
    self.assertAllClose(api.grad(f1)(x), api.grad(f2)(x), check_dtypes=False)

  def test_remat_symbolic_zeros(self):
    # code from https://github.com/google/jax/issues/1907

    key = jax.random.PRNGKey(0)
    key, split = jax.random.split(key)
    n = 5

    def func(D0):
      def shift(R, dR, **unused_kwargs):
        return R + dR

      def apply_fn(R):
        return D0 * R

      Rinit = jax.random.uniform(split, (n,3), minval=0.0, maxval=5.0,
                                 dtype=jnp.float32)

      def move(R,i):
        F = apply_fn(R)
        return shift(R, 0.001 * F), jnp.array([0.])

      move = api.remat(move)
      R, temp = lax.scan(move, Rinit, jnp.arange(2))
      return R[0, 0]

    api.grad(func)(5.0)  # doesn't crash

  def test_remat_jit2(self):
    @api.jit
    def f(x):
      y = 2 * x

      @api.remat
      def g():
        return y

      return g()

    self.assertAllClose(f(3), 6, check_dtypes=False)

  def test_remat_nontrivial_env(self):
    # simplified from https://github.com/google/jax/issues/2030

    @api.remat
    def foo(state, dt=0.5, c=1):
      u, u_t = state
      u_tt = c**2 * u
      u_t = u_t + u_tt * dt
      return (u, u_t)

    @partial(api.jit, static_argnums=(1,))
    def _multi_step(state, count, dt, c):
      f = lambda s, _: (foo(s, dt, c), _)
      return lax.scan(f, state, None, count)

    def multi_step(state, count, dt=1/jnp.sqrt(2), c=1):
      return _multi_step(state, count, dt, c)

    def loss(u0, target, steps, dt=1/jnp.sqrt(2), c=1):
      init = (u0, jnp.zeros_like(u0))
      (uf, _), _ = multi_step(init, steps, dt, c)
      return ((uf - target) ** 2).mean()

    target = jnp.zeros((128, 128))
    u0 = jnp.ones_like(target)
    loss(u0, target, 10)  # doesn't crash

  def test_remat_jit3(self):
    # https://github.com/google/jax/issues/2180
    def f(w, x):
      a = jnp.dot(x, w)
      b = jnp.einsum("btd,bTd->btT", a, a)
      c = jnp.einsum("btT,btd->btd", b, a)
      return jnp.sum(c)

    w = jnp.ones([1, 1])
    x = jnp.ones([1, 1, 1])
    f = api.remat(f)
    api.grad(f)(w, x)  # doesn't crash

    @api.jit
    def mul(a, b):
      return a * b

    def f(w, x):
      a = mul(w, x)
      b = mul(a, a)
      return b

    w = 1.
    x = 1.
    f = api.remat(f)
    api.grad(f)(w, x)  # doesn't crash

  def test_remat_scan2(self):
    # https://github.com/google/jax/issues/1963

    def scan_bug(x0):
      f = lambda x, _: (x + 1, None)
      def scanned_f(x, _):
        return lax.scan(f, x, xs=None, length=1)[0], None
      x, _ = jax.remat(scanned_f)(x0, None)
      return x

    jax.grad(scan_bug)(1.0)  # doesn't crash

  def test_remat_jit_static_argnum_omnistaging(self):
    # https://github.com/google/jax/issues/2833
    def named_call(f):
      def named_f(*args):
        f_ = lu.wrap_init(lambda: (f(*args),))
        out, = core.call_p.bind(f_)
        return out
      return named_f

    def f(a_bool, y):
      if a_bool:
        return y + 1
      else:
        return y

    api.jit(named_call(f), static_argnums=0)(True, 1)  # no crash

  def test_remat_eval_counter(self):
    # https://github.com/google/jax/issues/2737
    add_one_p = Primitive('add_one')
    add_one = add_one_p.bind

    num_evals = 0

    @contextmanager
    def assertEvals(n):
      start = num_evals
      yield
      assert num_evals - start == n

    def add_one_impl(x):
      nonlocal num_evals
      num_evals += 1
      return x + 1
    add_one_p.def_impl(add_one_impl)

    def add_one_jvp(pin, tin):
      pout = add_one(pin[0])
      return pout, pout * tin[0]
    ad.primitive_jvps[add_one_p] = add_one_jvp

    add_one_p.def_abstract_eval(lambda x: x)

    v = np.zeros((1,))

    f = jax.remat(add_one)
    g = jax.remat(lambda x: add_one(f(x)))

    # 2 calls needed to evaluate g
    with assertEvals(2):
      _, vjp = jax.vjp(g, v)
    # 2 calls made while transposing g, 1 call made while transposing f
    with assertEvals(3):
      vjp(v)

    @jax._src.util.curry
    def call(f, *args):
      return jax.core.call(
          jax.linear_util.wrap_init(lambda *args: [f(*args)]),
          *args, name='foo')[0]

    f = call(add_one)
    g = jax.remat(lambda x: add_one(f(x)))

    # 2 calls needed to evaluate g
    with assertEvals(2):
      _, vjp = jax.vjp(g, v)
    # 2 calls made while transposing g, no reevaluation for transposition of f
    with assertEvals(2):
      vjp(v)

  def test_escaped_tracer_remat(self):
    # b/169779185
    def f():
      seq = [jnp.zeros([])]
      def g():
        seq[0] += 1  # this is line 7 btw
        return seq[0]

      api.remat(g)()
      api.remat(g)()

    with self.assertRaisesRegex(core.UnexpectedTracerError, "global state"):
      api.jit(f)()

  def test_no_cse_widget_on_primals(self):
    @api.remat
    def g(x):
      return lax.sin(lax.sin(x)), 3.

    def f(x):
      x, _ = g(x)
      return x

    c = api.xla_computation(f)(2.)
    self.assertNotIn('while', c.as_hlo_text())
    self.assertNotIn('conditional', c.as_hlo_text())

    c = api.xla_computation(grad(f))(2.)
    text = c.as_hlo_text()
    self.assertTrue('while' in text or 'conditional' in text)

  def test_no_cse_widget_with_prevent_cse_false(self):
    @partial(api.remat, prevent_cse=False)
    def g(x):
      return lax.sin(lax.sin(x)), 3.

    def f(x):
      x, _ = g(x)
      return x

    c = api.xla_computation(f)(2.)
    self.assertNotIn('while', c.as_hlo_text())
    self.assertNotIn('conditional', c.as_hlo_text())

    c = api.xla_computation(grad(f))(2.)
    self.assertNotIn('while', c.as_hlo_text())
    self.assertNotIn('conditional', c.as_hlo_text())


class JaxprTest(jtu.JaxTestCase):

  def test_scalar_literals(self):
    jaxpr = api.make_jaxpr(lambda x: x + 2)(42)
    self.assertLen(jaxpr.jaxpr.constvars, 0)

  def test_abstract_inputs(self):
    jaxpr = api.make_jaxpr(lambda x: x + 2.)(
        types.SimpleNamespace(shape=(), dtype=np.dtype(np.float32)))
    self.assertEqual(jaxpr.in_avals[0].shape, ())
    self.assertEqual(jaxpr.in_avals[0].dtype, np.float32)

  def test_const(self):
    def fun(x):
      return (x, 1., np.zeros(1))

    expected = """
    { lambda a ; b.
    let
    in (b, 1.0, a) }
    """

    jaxpr = api.make_jaxpr(fun)(0.)
    self.assertMultiLineStrippedEqual(expected, str(jaxpr))

  def test_cond(self):
    def f(x):
      return lax.cond(x >= 0.,
                      x + 1.,
                      lambda xt: xt + x,
                      x + 2.,
                      lambda xf: xf - x)
    expected = """
    { lambda  ; a.
      let b = ge a 0.0
          c = add a 1.0
          d = add a 2.0
          e = convert_element_type[ new_dtype=int32
                                    weak_type=False ] b
          f = cond[ branches=( { lambda  ; e_ a b c.
                                  let d = sub c a
                                  in (d,) }
                                { lambda  ; a f_ b c.
                                  let d = add b a
                                  in (d,) } )
                    linear=(False, False, False, False) ] e a a c d
      in (f,) }
      """
    jaxpr = api.make_jaxpr(f)(3.)
    self.assertMultiLineStrippedEqual(expected, str(jaxpr))

  def test_make_jaxpr_static_argnums(self):
    def f(x, y):
      return x + y

    jaxpr = api.make_jaxpr(f, static_argnums=(1,))(2, 3)
    self.assertIn('3', str(jaxpr))

  def test_make_jaxpr_return_shape(self):
    _, shape_tree = api.make_jaxpr(lambda x: (x + 1, jnp.zeros(2, jnp.float32)),
                                   return_shape=True)(np.int32(1))
    expected = (api.ShapeDtypeStruct(shape=(), dtype=jnp.int32),
                api.ShapeDtypeStruct(shape=(2,), dtype=jnp.float32))
    self.assertEqual(shape_tree, expected)

  def test_make_jaxpr_axis_env(self):
    def f(x):
      return x - lax.psum(x, 'i')
    jaxpr = api.make_jaxpr(f, axis_env=[('i', 4)])(2)
    self.assertIn('psum', str(jaxpr))

  def test_make_jaxpr_named(self):
    def f(x):
      return x - lax.psum(x, 'i')

    x = api.ShapeDtypeStruct(
        shape=(2, 3), dtype=jnp.dtype(jnp.float32), named_shape={'i': 10})
    jaxpr = api.make_jaxpr(f, axis_env=[('i', 10)])(x)
    named_shapes = [v.aval.named_shape for v in jaxpr.jaxpr.eqns[1].invars]
    self.assertEqual(named_shapes, [{'i': 10}, {}])

  @parameterized.parameters(True, False)
  def test_vjp_reduce_axes_jaxpr(self, gy_batched):
    def f(w, x):
      return jnp.sin(jnp.dot(x, w))

    w = api.ShapeDtypeStruct(
        shape=(3, 4), dtype=jnp.float32, named_shape={})
    x = api.ShapeDtypeStruct(
        shape=(3,), dtype=jnp.float32, named_shape={'batch': 2})
    gy = api.ShapeDtypeStruct(
        shape=(4,), dtype=jnp.float32,
        named_shape={'batch': 2} if gy_batched else {})

    # per-example
    jaxpr, shapes = api.make_jaxpr(
        lambda w, x, gy: api.vjp(f, w, x)[1](gy), axis_env=[('batch', 2)],
        return_shape=True)(w, x, gy)
    expected = (api.ShapeDtypeStruct(
        shape=(3, 4), dtype=jnp.float32, named_shape={'batch': 2}), x)
    self.assertEqual(shapes, expected)
    self.assertNotIn('psum', str(jaxpr))

    # reduced
    jaxpr, shapes = api.make_jaxpr(
        lambda w, x, gy: api.vjp(f, w, x, reduce_axes=('batch',))[1](gy),
        axis_env=[('batch', 2)],
        return_shape=True)(w, x, gy)
    expected = (w, x)
    self.assertEqual(shapes, expected)
    self.assertIn('psum', str(jaxpr))


class CustomJVPTest(jtu.JaxTestCase):

  def test_basic(self):
    @api.custom_jvp
    def f(x):
      return jnp.sin(x)
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), 2 * jnp.cos(x) * g
    f.defjvp(f_jvp)

    x = 3.
    self.assertAllClose(f(x), jnp.sin(x))
    self.assertAllClose(api.jvp(f, (x,), (1.,)),
                        (jnp.sin(x), 2 * jnp.cos(x)))
    self.assertAllClose(api.grad(f)(x), 2 * jnp.cos(x))

  def test_invariance(self):
    @api.custom_jvp
    def f(x):
      return jnp.cos(2 * x) / 2.
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return (f(x), 3 * g)
    f.defjvp(f_jvp)
    def f2(x):
      y, _ = api.jvp(f, (x,), (x,))
      return y
    def f3(x):
      y, _ = api.jvp(f2, (x,), (x,))
      return y
    x = 1.
    self.assertAllClose(api.jvp(f, (x,), (x,)),
                        api.jvp(f2, (x,), (x,)),
                        check_dtypes=False)
    self.assertAllClose(api.jvp(f, (x,), (x,)),
                        api.jvp(f3, (x,), (x,)),
                        check_dtypes=False)

  def test_python_control_flow(self):
    @api.custom_jvp
    def f(x):
      if x > 0:
        return jnp.sin(x)
      else:
        return jnp.cos(x)
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      if x > 0:
        return f(x), 2 * g
      else:
        return f(x), 3 * g
    f.defjvp(f_jvp)
    x = 2.
    self.assertAllClose(f(x), jnp.sin(x))
    self.assertAllClose(f(-x), jnp.cos(-x))
    self.assertAllClose(api.jvp(f, (x,), (1.,)),
                        (jnp.sin(x), 2.),
                        check_dtypes=False)
    self.assertAllClose(api.jvp(f, (-x,), (1.,)),
                        (jnp.cos(-x), 3.),
                        check_dtypes=False)
    self.assertAllClose(api.grad(f)(x), 2., check_dtypes=False)
    self.assertAllClose(api.grad(f)(-x), 3., check_dtypes=False)

  def test_vmap(self):
    @api.custom_jvp
    def f(x):
      assert jnp.ndim(x) == 0
      return jnp.sin(x)
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      assert jnp.ndim(x) == jnp.ndim(g) == 0
      return f(x), 2 * jnp.cos(x) * g
    f.defjvp(f_jvp)

    x = jnp.arange(3.)
    xx = jnp.arange(6.).reshape(2, 3)

    # vmap of f
    self.assertAllClose(api.vmap(f)(x), jnp.sin(x))
    self.assertAllClose(api.vmap(api.vmap(f))(xx), jnp.sin(xx))

    # vmap of jvp of f
    self.assertAllClose(api.vmap(lambda x: api.jvp(f, (x,), (x,)))(x),
                        (jnp.sin(x), 2 * jnp.cos(x) * x))
    self.assertAllClose(api.vmap(api.vmap(lambda x: api.jvp(f, (x,), (x,))))(xx),
                        (jnp.sin(xx), 2 * jnp.cos(xx) * xx))

    # jvp of vmap of f
    self.assertAllClose(api.jvp(api.vmap(f), (x,), (x,)),
                        (jnp.sin(x), 2 * jnp.cos(x) * x))
    self.assertAllClose(api.jvp(api.vmap(api.vmap(f)), (xx,), (xx,)),
                        (jnp.sin(xx), 2 * jnp.cos(xx) * xx))

    # vmap of jvp of vmap of f
    self.assertAllClose(api.vmap(lambda x: api.jvp(api.vmap(f), (x,), (x,)))(xx),
                        (jnp.sin(xx), 2 * jnp.cos(xx) * xx))

  def test_jit(self):
    @api.custom_jvp
    def f(x):
      return jnp.sin(x)
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), 2 * jnp.cos(x) * g
    f.defjvp(f_jvp)

    x = 3.

    # jit
    self.assertAllClose(api.jit(f)(x), jnp.sin(x))
    self.assertAllClose(api.jit(api.jit(f))(x), jnp.sin(x))

    # jit of jvp
    self.assertAllClose(api.jit(lambda x: api.jvp(f, (x,), (x,)))(x),
                        (jnp.sin(x), 2 * jnp.cos(x) * x),
                        check_dtypes=False)

    # jvp of jit
    self.assertAllClose(api.jvp(api.jit(f), (x,), (x,)),
                        (jnp.sin(x), 2 * jnp.cos(x) * x),
                        check_dtypes=False)

  def test_pytrees(self):
    @api.custom_jvp
    def f(x):
      return {'b': jnp.sin(x['a'])}
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), {'b': 2 * jnp.cos(x['a']) * g['a']}
    f.defjvp(f_jvp)
    x = {'a': 3.}
    self.assertAllClose(f(x)['b'], jnp.sin(x['a']))
    self.assertAllClose(api.jvp(f, (x,), (x,)),
                        ({'b': jnp.sin(x['a'])},
                         {'b': 2 * jnp.cos(x['a']) * x['a']}),
                        check_dtypes=False)

  def test_kwargs(self):
    # from https://github.com/google/jax/issues/1938
    @api.custom_jvp
    def my_fun(x, y, c=1.):
      return c * (x + y)
    def my_jvp(primals, tangents):
      x, y, c = primals
      t_x, t_y, t_c = tangents
      return my_fun(x, y, c), t_c
    my_fun.defjvp(my_jvp)
    f = lambda x, y: jnp.square(my_fun(x, y, c=2.)).sum()
    f(10., 5.)  # doesn't crash
    api.jvp(f, (10., 5.), (1., 1.))  # doesn't crash

  def test_initial_style(self):
    @api.custom_jvp
    def f(x):
      return 3 * x
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), 2 * g
    f.defjvp(f_jvp)

    def foo(x):
      out, _  = lax.scan(lambda c, _: (f(c), None), x, None, length=1)
      return out

    ans = api.grad(foo)(3.)
    expected = 2.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.jit(foo))(3.)
    expected = 2.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jit(api.grad(foo))(3.)
    expected = 2.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.grad(foo))(3.)
    expected = 0.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.grad(api.jit(foo)))(3.)
    expected = 0.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.jit(api.grad(foo)))(3.)
    expected = 0.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jit(api.grad(api.grad(foo)))(3.)
    expected = 0.
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_initial_style_vmap(self):
    @api.custom_jvp
    def f(x):
      assert jnp.ndim(x) == 0
      return 3 * x
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), 2 * g
    f.defjvp(f_jvp)

    def foo(x):
      out, _  = lax.scan(lambda c, _: (f(c), None), x, None, length=1)
      return out

    ans = api.vmap(foo)(jnp.ones(3))
    expected = 3. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.vmap(api.jit(foo))(jnp.ones(3))
    expected = 3. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jit(api.vmap(foo))(jnp.ones(3))
    expected = 3. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(lambda x: api.vmap(foo)(x).sum())(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(lambda x: api.vmap(api.jit(foo))(x).sum())(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(lambda x: api.jit(api.vmap(foo))(x).sum())(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.jit(lambda x: api.vmap(foo)(x).sum()))(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jit(api.grad(lambda x: api.vmap(foo)(x).sum()))(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_closed_over_tracers_error_message(self):
    def f(x):
      @api.custom_jvp
      def g(y):
        return x + y
      def g_jvp(primals, tangents):
        return g(x), 2 * primals[0]
      g.defjvp(g_jvp)
      return g(1.)

    self.assertRaises(ad.CustomJVPException, lambda: api.jvp(f, (3.,), (1.,)))
    self.assertRaises(ad.CustomJVPException, lambda: api.grad(f)(3.))

  def test_nondiff_arg(self):
    @partial(api.custom_jvp, nondiff_argnums=(0,))
    def app(f, x):
      return f(x)
    def app_jvp(f, primals, tangents):
      (x,), (t,) = primals, tangents
      return app(f, x), 3 * t
    app.defjvp(app_jvp)

    ans = app(lambda x: 2 * x, 1)
    expected = 2
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jvp(lambda x: app(lambda y: 2 * y, x), (1.,), (1.,))
    expected = (2., 3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_nondiff_arg_jit_tracer(self):
    @partial(api.custom_jvp, nondiff_argnums=(0,))
    def f(x, y):
      return x * y
    def f_jvp(x, primals, tangents):
      (y,), (t_y,) = primals, tangents
      return f(x, y), 5 * t_y
    f.defjvp(f_jvp)

    @jit
    def g(x, y):
      return f(x, y)

    ans = api.jvp(lambda y: g(2., y), (3.,), (1.,))
    expected = (6., 5.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_nondiff_arg_hiding_jvp_tracer(self):
    def f(x):
      @partial(api.custom_jvp, nondiff_argnums=(0,))
      def g(h, x):
        return h(x)
      @g.defjvp
      def g_jvp(h, primals, tangents):
        x, = primals
        t, = tangents
        return g(h, x), 2. * t
      h = lambda y: x + y  # capture x
      return g(h, x)

    with self.assertRaisesRegex(ad.CustomJVPException, "Detected differentiation"):
      api.jvp(f, (2.,), (1.,))

  def test_vmap_axes(self):
    raise unittest.SkipTest("TODO")  # TODO(mattjj): write test

  def test_pmap(self):
    raise unittest.SkipTest("TODO")  # TODO(mattjj): write test

  def test_missing_jvp_rule_error_message(self):
    @api.custom_jvp
    def foo(x):
      return x ** 2

    self.assertRaisesRegex(
        AttributeError,
        r"No JVP defined for custom_jvp function foo using defjvp.",
        lambda: foo(2))
    self.assertRaisesRegex(
        AttributeError,
        r"No JVP defined for custom_jvp function foo using defjvp.",
        lambda: api.jvp(foo, (2.,), (1.,)))
    self.assertRaisesRegex(
        AttributeError,
        r"No JVP defined for custom_jvp function foo using defjvp.",
        lambda: api.grad(foo)(2.))

  def test_jvp_rule_inconsistent_pytree_structures_error_message(self):
    @api.custom_jvp
    def f(x):
      return (x**2,)

    @f.defjvp
    def foo_jvp(primals, tangents):
      x, = primals
      t, = tangents
      return f(x), [2 * x * t, x]

    f(2.)  # doesn't crash
    self.assertRaisesRegex(
        TypeError,
        re.escape(
            "Custom JVP rule must produce primal and tangent outputs "
            "with equal container (pytree) structures, but got "
            "{} and {} respectively.".format(
                tree_util.tree_structure((1,)),
                tree_util.tree_structure([1, 2]))
        ),
        lambda: api.jvp(f, (2.,), (1.,)))

  def test_primal_tangent_aval_disagreement_error_message(self):
    @api.custom_jvp
    def f(x):
      return x ** 2

    @f.defjvp
    def foo_jvp(primals, tangents):
      x, = primals
      t, = tangents
      return f(x), jnp.reshape(t, (1,))

    f(2.)  # doesn't crash
    self.assertRaisesRegex(
        TypeError,
        re.escape(
            "Custom JVP rule must produce primal and tangent outputs "
            "with equal shapes and dtypes, but got float32[] and float32[1] "
            "respectively."),
        lambda: api.jvp(f, (jnp.float32(2.),), (jnp.float32(1.),)))

  def test_jvp_rule_doesnt_return_pair_error_message(self):
    # https://github.com/google/jax/issues/2516

    @api.custom_jvp
    def f(x):
      return x ** 2

    @f.defjvp
    def foo_jvp(primals, tangents):
      x, = primals
      t, = tangents
      return t

    f(2.)  # doesn't crash
    self.assertRaisesRegex(
        TypeError,
        re.escape(
            "Custom JVP rule must produce a pair (list or tuple of length two) "
            "representing primal and tangent outputs, got 1.0"),
        lambda: api.jvp(f, (2.,), (1.,)))

  def test_multiple_rule_invocations(self):
    @jax.custom_jvp
    def expit(x):
      return 1 / (1 + lax.exp(-x))

    @expit.defjvp
    def _expit_jvp(primals, tangents):
      (x,), (t,) = primals, tangents
      ans = expit(x)
      t_out = t * ans * (1 - ans)
      return ans, t_out

    def scanned_fun(c, _):
      return [expit(c[0])] + [c[i-1] + c[i] for i in range(1, len(c))], None

    def foo(x):
      c, _ = lax.scan(scanned_fun, [x, 0., 0., 0., 0.], None, length=10)
      return c[-1]

    # just make sure these don't crash
    foo(3.)
    grad(foo)(3.)
    grad(lambda x: jax.vmap(foo)(x).sum())(jnp.arange(3.))

  def test_hard_stuff(self):
    arr = jnp.ones((5, 2, 2))
    api.jit(jax.vmap(jnp.linalg.det))(arr)  # doesn't crash

  def test_hard_stuff2(self):
    @jax.custom_jvp
    def f(x):
      return lax.tie_in(x, np.zeros(x.shape, x.dtype))

    @f.defjvp
    def f_jvp(primals, tangents):
      x, = primals
      t, = tangents
      return f(x), t

    # don't crash
    jax.jit(jax.vmap(f))(jnp.arange(3.))
    jax.jit(jax.vmap(jax.grad(f)))(jnp.arange(3.))
    jax.jit(jax.grad(lambda x: jax.vmap(f)(x).sum()))(jnp.arange(3.))
    jax.grad(lambda x: jax.vmap(f)(x).sum())(jnp.arange(3.))
    jax.jvp(jax.vmap(f), (jnp.arange(3.),), (jnp.ones(3),))

  def test_hard_stuff3(self):
    @jax.custom_jvp
    def relu(x):
      return jnp.maximum(x, 0)

    @relu.defjvp
    def _relu_jvp(primals, tangents):
      x, = primals
      t, = tangents
      return relu(x), lax.select(x > 0, t, lax.full_like(t, 0))

    def scanned_fun(c, _):
      return [relu(c[0])] + [c[i-1] + c[i] for i in range(1, len(c))], None

    def f(x):
      c, _ = lax.scan(scanned_fun, [x, 0., 0., 0., 0.], None, length=10)
      return c[-1]

    # don't crash
    jax.jit(jax.vmap(f))(jnp.arange(3.))
    jax.jit(jax.vmap(jax.grad(f)))(jnp.arange(3.))
    jax.jit(jax.grad(lambda x: jax.vmap(f)(x).sum()))(jnp.arange(3.))
    jax.grad(lambda x: jax.vmap(f)(x).sum())(jnp.arange(3.))
    jax.jvp(jax.jit(jax.vmap(f)), (jnp.arange(3.),), (jnp.ones(3),))

  def test_eval_shape(self):
    @jax.custom_jvp
    def expit(x):
      return 1 / (1 + lax.exp(-x))

    @expit.defjvp
    def _expit_jvp(primals, tangents):
      (x,), (t,) = primals, tangents
      ans = expit(x)
      t_out = t * ans * (1 - ans)
      return ans, t_out

    # don't crash
    api.eval_shape(expit, jnp.ones((2, 3)))
    api.eval_shape(api.grad(lambda x: expit(x).sum()), jnp.ones((2, 3)))

  def test_jaxpr_zeros(self):
    # from https://github.com/google/jax/issues/2657
    @api.custom_jvp
    def f(A, b):
      return A @ b

    def f_jvp(primals, tangents):
      A, b = primals
      dA, db = tangents
      z = f(A, b)
      dz = A @ db + dA @ b
      return z, dz

    f.defjvp(f_jvp)

    def experiment(theta):
      def step(q, _):
        z = f(jnp.eye(3), jnp.ones(3) * theta)
        q += z[0]
        return q, q

      q = 0.
      q, _ = lax.scan(step, q, None, 4)
      return q

    grad(experiment)(1.)  # doesn't crash

  def test_linear_in_scan(self):
    @api.custom_jvp
    def f(x):
      return -x

    @f.defjvp
    def f_jvp(primals, tangents):
      x, = primals
      x_dot, = tangents
      return f(x), f(x_dot)

    def foo(x):
      out, _  = lax.scan(lambda c, _: (f(c), None), x, None, length=1)
      return out

    ans = api.grad(foo)(3.)
    expected = -1.
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_custom_jvps_first_rule_is_none(self):
    # https://github.com/google/jax/issues/3389
    @api.custom_jvp
    def f(x, y):
      return x ** 2 * y

    f.defjvps(None, lambda x_dot, primal_out, x, y: 2 * x * y * x_dot)
    ans = grad(f, 1)(2., 3.)  # doesn't crash
    expected = 12.
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_concurrent_initial_style(self):
    # https://github.com/google/jax/issues/3843
    def unroll(param, sequence):
      def scan_f(prev_state, inputs):
        return prev_state, jax.nn.sigmoid(param * inputs)
      return jnp.sum(jax.lax.scan(scan_f, None, sequence)[1])

    def run():
      return jax.grad(unroll)(jnp.array(1.0), jnp.array([1.0]))

    expected = run()

    # we just don't want this to crash
    n_workers = 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as e:
      futures = []
      for _ in range(n_workers):
        futures.append(e.submit(run))
      results = [f.result() for f in futures]
    for ans in results:
      self.assertAllClose(ans, expected)

  def test_nondiff_argnums_vmap_tracer(self):
    # https://github.com/google/jax/issues/3964
    @partial(jax.custom_jvp, nondiff_argnums=(0, 2))
    def sample(shape, param, seed):
      return jax.random.uniform(key=seed, shape=shape, minval=param)

    @sample.defjvp
    def sample_jvp(shape, seed, primals, tangents):
      param, = primals
      dparam, = tangents
      dparam = jnp.broadcast_to(dparam, shape)
      samples = sample(shape, param, seed)
      return samples, samples * dparam  # dummy jvp for proof of concept

    # check these don't crash
    jax.vmap(lambda seed: sample((2,3), 1., seed))(
        jax.random.split(jax.random.PRNGKey(1), 10))
    jax.jvp(lambda x: sample((2, 3), x, jax.random.PRNGKey(1)),
            (1.,), (1.,))

  def test_fun_with_nested_calls_2(self):
    def call(f, *args):
      f = api.custom_jvp(f)
      f.defjvp(lambda primals, tangents: (f(*primals), sum(tangents)))
      return f(*args)

    def fun_with_nested_calls_2(x):
      def bar(y):
        def baz(w):
          q = call(lambda x: y, x)
          q = q + call(lambda: y)
          q = q + call(lambda y: w + y, y)
          q = call(lambda w: call(jnp.sin, x) * y, 1.0) + q
          return q
        return api.jit(baz)(x)
      return call(bar, x)

    # test these don't crash
    self.assertAllClose(api.jit(fun_with_nested_calls_2)(3.),
                        fun_with_nested_calls_2(3.))
    api.vmap(fun_with_nested_calls_2)(jnp.arange(3.))

  def test_closure_with_vmap(self):
    # https://github.com/google/jax/issues/3822
    alpha = np.float32(2.)

    def sample(seed):
      @api.custom_jvp
      def f(alpha):
        return jax.random.gamma(seed, alpha, shape=[])

      @f.defjvp
      def f_jvp(primal, tangent):
        alpha = primal
        dalpha = tangent
        sample = f(alpha)
        partial_alpha = lax.random_gamma_grad(alpha, sample)
        return sample, partial_alpha * dalpha
      return f(alpha)

    api.vmap(sample)(jax.random.split(jax.random.PRNGKey(1), 3))  # don't crash

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_float0(self):
    @api.custom_jvp
    def f(x, y):
      return x, y
    def f_jvp(primals, _):
      # we need a defined (non-float0) tangent to trigger the rule
      return primals, (2., 1)
    f.defjvp(f_jvp)

    primals = (2., 3)
    tangents = (np.ones(()), np.zeros((), float0),)
    expected_tangents = (2., np.zeros((), float0))
    self.assertArraysEqual(api.jvp(f, primals, tangents),
                           (primals, expected_tangents))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_float0_initial_style(self):
    @api.custom_jvp
    def f(x, y):
      return x, y
    def f_jvp(primals, _):
      x, y = primals
      return (x, y), (2., 1)
    f.defjvp(f_jvp)

    def foo(x, y):
      out, _ = lax.scan(lambda c, _: (f(*c), None), (x, y), None, length=1)
      return out

    primals = (2., 3)
    tangents = (np.ones(()), np.zeros((), float0),)
    expected_tangents = (2., np.zeros((), float0))
    self.assertArraysEqual(api.jvp(foo, primals, tangents),
                           (primals, expected_tangents))

  def test_remat(self):
    @api.custom_jvp
    def f(x):
      return jnp.sin(x)
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), 2 * jnp.cos(x) * g
    f.defjvp(f_jvp)

    @api.remat
    def g(x):
      return f(f(x))

    ans = g(2.)
    expected = np.sin(np.sin(2.))
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(g)(2.)
    expected = 4. * api.grad(lambda x: jnp.sin(jnp.sin(x)))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_remat_higher_order(self):
    @api.custom_jvp
    def f(x):
      return jnp.sin(x)
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), 2 * jnp.cos(x) * g
    f.defjvp(f_jvp)

    def g(x):
      return f(f(x))

    ans = api.grad(api.grad(api.remat(g)))(2.)
    expected = api.grad(api.grad(g))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.remat(api.grad(g)))(2.)
    expected = api.grad(api.grad(g))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.grad(api.grad(api.remat(g))))(2.)
    expected = api.grad(api.grad(api.grad(g)))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_initial_style_vmap_2(self):
    # This is like test_initial_style_vmap except the primal function closes
    # over an array constant.
    y = jnp.array([1., 2., 3.])

    @api.custom_jvp
    def f(x):
      assert jnp.ndim(x) == 0
      return 3 * x * jnp.sum(y)
    def f_jvp(primals, tangents):
      x, = primals
      g, = tangents
      return f(x), 2 * g
    f.defjvp(f_jvp)

    def foo(x):
      out, _  = lax.scan(lambda c, _: (f(c), None), x, None, length=1)
      return out

    ans = api.grad(lambda x: api.vmap(foo)(x).sum())(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(lambda x: api.vmap(api.jit(foo))(x).sum())(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(lambda x: api.jit(api.vmap(foo))(x).sum())(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.jit(lambda x: api.vmap(foo)(x).sum()))(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.jit(api.grad(lambda x: api.vmap(foo)(x).sum()))(jnp.ones(3))
    expected = 2. * jnp.ones(3)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_custom_jvp_vmap_broadcasting_interaction(self):
    # https://github.com/google/jax/issues/6452
    def f2(y, z):
      v1 = z
      v2 = jnp.sum(y) + z
      return jnp.logaddexp(v1, v2)

    def f1(y, z):
      v = api.vmap(lambda _y: f2(_y, z))(y)
      return jnp.sum(v)

    y = jnp.ones((3, 2))
    f = lambda z: f1(y, z)
    z = 0.1
    val, g = api.value_and_grad(f)(z)
    self.assertEqual(val.shape, ())
    self.assertEqual(g.shape, ())

  def test_custom_jvp_vmap_broadcasting_interaction_2(self):
    # https://github.com/google/jax/issues/5849
    @api.custom_jvp
    def transform(box, R):
      if jnp.isscalar(box) or box.size == 1:
        return R * box
      elif box.ndim == 2:
        return jnp.einsum('ij,j->i', box, R)
      raise ValueError()

    @transform.defjvp
    def transform_jvp(primals, tangents):
      box, R = primals
      dbox, dR = tangents
      return (transform(box, R), dR + transform(dbox, R))

    def periodic_general(box):
      def displacement_fn(Ra, Rb, **kwargs):
        _box = kwargs.get('box', box)
        return transform(_box, Ra - Rb)

      return displacement_fn

    N = 250

    scalar_box = 1.0
    displacement = periodic_general(scalar_box)

    key = jax.random.PRNGKey(0)
    R = jax.random.uniform(key, (N, 2))

    def energy_fn(box):
      d = partial(displacement, box=box)
      d = api.vmap(api.vmap(d, (None, 0)), (0, None))
      return jnp.sum(d(R, R) ** 2)

    self.assertEqual(grad(energy_fn)(scalar_box).shape, ())

  def test_custom_jvp_implicit_broadcasting(self):
    # https://github.com/google/jax/issues/6357
    if config.x64_enabled:
      raise unittest.SkipTest("test only applies when x64 is disabled")

    @jax.custom_jvp
    def projection_unit_simplex(x: jnp.ndarray) -> jnp.ndarray:
      """Projection onto the unit simplex."""
      s = 1.0
      n_features = x.shape[0]
      u = jnp.sort(x)[::-1]
      cssv = jnp.cumsum(u) - s
      ind = jnp.arange(n_features) + 1
      cond = u - cssv / ind > 0
      idx = jnp.count_nonzero(cond)
      threshold = cssv[idx - 1] / idx.astype(x.dtype)
      return jax.nn.relu(x - threshold)


    @projection_unit_simplex.defjvp
    def projection_unit_simplex_jvp(primals, tangents):
      x, = primals
      x_dot, = tangents
      primal_out = projection_unit_simplex(x)
      supp = primal_out > 0
      card = jnp.count_nonzero(supp)
      tangent_out = supp * x_dot - (jnp.dot(supp, x_dot) / card) * supp
      return primal_out, tangent_out

    rng = np.random.RandomState(0)
    x = rng.rand(5).astype(np.float32)

    J_rev = jax.jacrev(projection_unit_simplex)(x)
    J_fwd = jax.jacfwd(projection_unit_simplex)(x)

    p = projection_unit_simplex(x)
    support = (p > 0).astype(jnp.int32)
    cardinality = jnp.count_nonzero(support)
    J_true = jnp.diag(support) - jnp.outer(support, support) / cardinality
    self.assertAllClose(J_true, J_fwd)
    self.assertAllClose(J_true, J_rev)

    proj = jax.vmap(projection_unit_simplex)

    def fun(X):
      return jnp.sum(proj(X) ** 2)

    rng = np.random.RandomState(0)
    X = rng.rand(4, 5).astype(np.float32)
    U = rng.rand(4, 5)
    U /= np.sqrt(np.sum(U ** 2))
    U = U.astype(np.float32)

    eps = 1e-3
    dir_deriv_num = (fun(X + eps * U) - fun(X - eps * U)) / (2 * eps)
    dir_deriv = jnp.vdot(jax.grad(fun)(X), U)
    self.assertAllClose(dir_deriv, dir_deriv_num, atol=1e-3)

  def test_vmap_inside_defjvp(self):
    # https://github.com/google/jax/issues/3201
    seed = 47
    key = jax.random.PRNGKey(seed)
    mat = jax.random.normal(key, (2, 3))

    @jax.custom_jvp
    def f(mat, aux):
        num_rows, num_cols = mat.shape
        return jnp.ones((num_rows, 1)) / num_cols

    @f.defjvp
    def f_jvp(primals, tangents):
        mat, aux = primals
        vec, _ = tangents
        output = f(*primals)
        num_rows, num_cols = mat.shape
        size = num_rows * num_cols
        # -----
        bd_mat = mat.reshape(1, 1, num_rows, num_cols)
        bd_mat = jnp.tile(bd_mat, reps=(num_rows, num_cols))
        bd_mat = bd_mat.reshape(size, num_rows, num_cols)
        # -----
        rowsum = jnp.sum(mat, axis=1, keepdims=True)
        colsum = jnp.sum(mat, axis=0, keepdims=True)
        bd_rowsum = jnp.tile(rowsum, reps=(1, num_rows))
        bd_colsum = jnp.tile(colsum, reps=(num_cols, 1))
        # -----
        bd_vec = vec.reshape(size, 1)
        # -----
        def operate(mx, val):
            buf = 0
            for i in range(2):
                buf = buf + jnp.matmul(mx, bd_colsum) / jnp.power(aux, i)
            buf = jnp.matmul(bd_rowsum, buf)
            return buf * val
        # -----
        # Vertorizing will raise shape error
        bd_buf = jax.vmap(operate, in_axes=(0, 0), out_axes=0)(bd_mat, bd_vec)
        # -----
        bd_buf = bd_buf / aux
        jvp = jnp.sum(bd_buf, axis=0)
        jvp = jnp.mean(jvp, axis=1, keepdims=True)
        # -----
        # JVP ends successfully, but still raise an error
        return (output, jvp)

    jax.grad(lambda mat, aux: jnp.sum(f(mat, aux)))(mat, 0.5)  # doesn't crash

  def test_custom_jvp_unbroadcasting(self):
    # https://github.com/google/jax/issues/3056
    a = jnp.array([1., 1.])

    @jax.custom_jvp
    def f(x):
      return a * x

    @f.defjvp
    def f_jvp(primals, tangents):
      x, = primals
      dx, = tangents
      return a * x, a * dx

    shape = grad(lambda x: jnp.sum(f(x)))(jnp.array(1.)).shape
    self.assertEqual(shape, ())


class CustomVJPTest(jtu.JaxTestCase):

  def test_basic(self):
    @api.custom_vjp
    def f(x):
      return jnp.sin(x)
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    x = 3.
    self.assertAllClose(f(x), jnp.sin(x))
    self.assertAllClose(api.grad(f)(x), 2 * jnp.cos(x))
    self.assertAllClose(api.value_and_grad(f)(x),
                        (jnp.sin(x), 2 * jnp.cos(x)))

  def test_invariance(self):
    @api.custom_vjp
    def f(x):
      return jnp.cos(2 * x) / 2.
    def f_fwd(x):
      return (f(x), x)
    def f_rev(x, g):
      return (g * 3,)
    f.defvjp(f_fwd, f_rev)
    def f2(x):
      y, _ = api.value_and_grad(f)(x)
      return y
    def f3(x):
      y, _ = api.value_and_grad(f2)(x)
      return y
    x = 1.
    self.assertAllClose(f(x), f2(x), check_dtypes=False)
    self.assertAllClose(f(x), f3(x), check_dtypes=False)
    self.assertAllClose(api.grad(f)(x), api.grad(f2)(x),
                        check_dtypes=False)
    self.assertAllClose(api.grad(f)(x), api.grad(f3)(x),
                        check_dtypes=False)

  def test_python_control_flow(self):
    @api.custom_vjp
    def f(x):
      if x > 0:
        return jnp.sin(x)
      else:
        return jnp.cos(x)
    def f_fwd(x):
      if x > 0:
        return f(x), x
      else:
        return f(x), x
    def f_rev(x, g):
      if x > 0:
        return (2 * g,)
      else:
        return (3 * g,)
    f.defvjp(f_fwd, f_rev)
    x = 2.
    self.assertAllClose(f(x), jnp.sin(x))
    self.assertAllClose(f(-x), jnp.cos(-x))
    self.assertAllClose(api.value_and_grad(f)(x), (jnp.sin(x), 2.),
                        check_dtypes=False)
    self.assertAllClose(api.value_and_grad(f)(-x), (jnp.cos(-x), 3.),
                        check_dtypes=False)

  def test_vmap(self):
    @api.custom_vjp
    def f(x):
      assert jnp.ndim(x) == 0
      return jnp.sin(x)
    def f_fwd(x):
      assert jnp.ndim(x) == 0
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    x = jnp.arange(3.)
    xx = jnp.arange(6.).reshape(2, 3)

    # vmap of f
    self.assertAllClose(api.vmap(f)(x), jnp.sin(x))
    self.assertAllClose(api.vmap(api.vmap(f))(xx), jnp.sin(xx))

    # vmap of grad of f
    self.assertAllClose(api.vmap(api.grad(f))(x), 2 * jnp.cos(x))
    self.assertAllClose(api.vmap(api.value_and_grad(f))(x),
                        (jnp.sin(x), 2 * jnp.cos(x)))
    self.assertAllClose(api.vmap(api.vmap(api.grad(f)))(xx), 2 * jnp.cos(xx))
    self.assertAllClose(api.vmap(api.vmap(api.value_and_grad(f)))(xx),
                        (jnp.sin(xx), 2 * jnp.cos(xx)))

    # grad of vmap of f
    self.assertAllClose(api.grad(lambda x: api.vmap(f)(x).sum())(x),
                        2 * jnp.cos(x))
    self.assertAllClose(api.grad(lambda x: api.vmap(api.vmap(f))(x).sum())(xx),
                        2 * jnp.cos(xx))

    # vmap of grad of vmap of f
    self.assertAllClose(api.vmap(api.grad(lambda x: api.vmap(f)(x).sum()))(xx),
                        2 * jnp.cos(xx))

  def test_jit(self):
    @api.custom_vjp
    def f(x):
      return jnp.sin(x)
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    x = 3.

    # jit
    self.assertAllClose(api.jit(f)(x), jnp.sin(x))
    self.assertAllClose(api.jit(api.jit(f))(x), jnp.sin(x))

    # jit of grad
    self.assertAllClose(api.jit(api.grad(f))(x), 2 * jnp.cos(x),
                        check_dtypes=False)

    # grad of jit
    self.assertAllClose(api.grad(api.jit(f))(x), 2 * jnp.cos(x),
                        check_dtypes=False)

  def test_pytrees(self):
    @api.custom_vjp
    def f(x):
      return {'b': jnp.sin(x['a'])}
    def f_fwd(x):
      return f(x), {'r': jnp.cos(x['a'])}
    def f_bwd(res, g):
      cos_x = res['r']
      return ({'a': 2 * cos_x * g['b']},)
    f.defvjp(f_fwd, f_bwd)
    x = {'a': 3.}
    self.assertAllClose(f(x)['b'], jnp.sin(x['a']))
    self.assertAllClose(api.grad(lambda x: f(x)['b'])(x),
                        {'a': 2 * jnp.cos(x['a'])})

  def test_jvp_error(self):
    @api.custom_vjp
    def f(x):
      return jnp.sin(x)
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    self.assertRaisesRegex(
        TypeError,
        r"can't apply forward-mode autodiff \(jvp\) to a custom_vjp function.",
        lambda: api.jvp(f, (3.,), (1.,)))
    self.assertRaisesRegex(
        TypeError,
        r"can't apply forward-mode autodiff \(jvp\) to a custom_vjp function.",
        lambda: api.jvp(api.vmap(f), (jnp.arange(3.),), (jnp.ones(3),)))
    self.assertRaisesRegex(
        TypeError,
        r"can't apply forward-mode autodiff \(jvp\) to a custom_vjp function.",
        lambda: api.jvp(jit(f), (3.,), (1.,)))

  def test_kwargs(self):
    # from https://github.com/google/jax/issues/1938
    @api.custom_vjp
    def my_fun(x, y, c=1.):
      return c * (x + y)
    my_fun.defvjp(lambda x, y, c=1.: (my_fun(c, y, c), None),
                  lambda _, g: (g, g, g))
    f = lambda x, y: jnp.square(my_fun(x, y, c=2.)).sum()
    f(10., 5.)  # doesn't crash
    api.grad(f)(10., 5.)  # doesn't crash

  def test_initial_style(self):
    @api.custom_vjp
    def f(x):
      return jnp.sin(x)
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    def foo(x):
      out, _  = lax.scan(lambda c, _: (f(c), None), x, None, length=1)
      return out

    ans = api.grad(foo)(3.)
    expected = 2. * jnp.cos(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.grad(foo))(3.)
    expected = -2. * jnp.sin(3.)
    self.assertAllClose(ans, expected)

  def test_initial_style_vmap(self):
    @api.custom_vjp
    def f(x):
      assert jnp.ndim(x) == 0
      return 3 * x
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    def foo(x):
      out, _  = lax.scan(lambda c, _: (f(c), None), x, None, length=1)
      return out

    ans = api.vmap(foo)(jnp.arange(3.))
    expected = 3. * jnp.arange(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(lambda x: api.vmap(foo)(x).sum())(jnp.arange(3.))
    expected = 2. * jnp.cos(jnp.arange(3.))
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_nondiff_arg(self):
    @partial(api.custom_vjp, nondiff_argnums=(0,))
    def app(f, x):
      return f(x)
    def app_fwd(f, x):
      return app(f, x), jnp.cos(x)
    def app_rev(f, cos_x, g):
      return (cos_x * g,)
    app.defvjp(app_fwd, app_rev)

    ans = app(lambda x: 2 * x, 1)
    expected = 2
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.value_and_grad(lambda x: app(lambda y: 2 * y, x))(1.)
    expected = (2., jnp.cos(1.))
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_nondiff_arg_tracer(self):
    # This test is now skipped because we decided not to support this behavior
    # anymore (namely, nondiff args can't be tracers), but
    # test_closed_over_tracer is a replacement test for analogous behavior that
    # we do support
    raise unittest.SkipTest("removed support for tracers in nondiff args")

    @partial(api.custom_vjp, nondiff_argnums=(0,))
    def f(x, y):
      return x * y
    def f_fwd(x, y):
      return f(x, y), jnp.cos(y)
    def f_rev(x, cos_y, g):
      return (cos_y * g,)
    f.defvjp(f_fwd, f_rev)

    @jit
    def g(x, y):
      return f(x, y)

    ans = g(2, 3.)
    expected = 6.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(g, 1)(2., 3.)
    expected = jnp.cos(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_closed_over_tracer(self):
    # This test is similar to test_nondiff_arg_tracer except it uses lexical
    # closure rather than the nondiff_argnums mechanism. We decided to disallow
    # tracers in nondiff_argnums to greatly simplify bookkeeping while still
    # supporting the cases for which it is necessary.
    def outer(x):
      @api.custom_vjp
      def f(y):
        return x * y
      def f_fwd(y):
        return f(y), jnp.cos(y)
      def f_rev(cos_y, g):
        return (cos_y * g,)
      f.defvjp(f_fwd, f_rev)
      return f

    @jit
    def g(x, y):
      return outer(x)(y)

    ans = g(2, 3.)
    expected = 6.
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(g, 1)(2., 3.)
    expected = jnp.cos(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_closed_over_tracer2(self):
    def outer(x):
      @api.custom_vjp
      def f(y):
        return x * y
      def f_fwd(y):
        return f(y), jnp.cos(y)
      def f_rev(cos_y, g):
        return (cos_y * g,)
      f.defvjp(f_fwd, f_rev)
      return f

    @api.vmap
    def g(x):
      return outer(x)(3.)

    ans = g(np.arange(3.))
    expected = np.arange(3.) * 3
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_closed_over_tracer3(self):
    def outer(x):
      @api.custom_vjp
      def f(y):
        return x * y
      def f_fwd(y):
        return f(y), (x, jnp.cos(y))
      def f_rev(res, g):
        x, cos_y = res
        return (cos_y * g * x,)
      f.defvjp(f_fwd, f_rev)
      return api.grad(f)

    @api.vmap
    def g(x):
      return outer(x)(3.)

    ans = g(np.arange(3.))
    expected = np.cos(3.) * np.arange(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_nondiff_arg_tracer_error(self):
    # This is similar to the old (now skipped) test_nondiff_arg_tracer, except
    # we're testing for the error message that that usage pattern now raises.

    @partial(api.custom_vjp, nondiff_argnums=(0,))
    def f(x, y):
      return x * y
    def f_fwd(x, y):
      return f(x, y), jnp.cos(y)
    def f_rev(x, cos_y, g):
      return (cos_y * g,)
    f.defvjp(f_fwd, f_rev)

    @jit
    def g(x, y):
      return f(x, y)

    with self.assertRaisesRegex(core.UnexpectedTracerError, "custom_vjp"):
      _ = g(2, 3.)
    with self.assertRaisesRegex(core.UnexpectedTracerError, "custom_vjp"):
      _ = api.grad(g, 1)(2., 3.)

  def test_vmap_axes(self):
    raise unittest.SkipTest("TODO")  # TODO(mattjj): write test

  def test_pmap(self):
    raise unittest.SkipTest("TODO")  # TODO(mattjj): write test

  def test_missing_vjp_rule_error(self):
    @api.custom_vjp
    def foo(x):
      return x ** 2

    self.assertRaisesRegex(
        AttributeError,
        r"No VJP defined for custom_vjp function foo using defvjp.",
        lambda: foo(2))
    self.assertRaisesRegex(
        AttributeError,
        r"No VJP defined for custom_vjp function foo using defvjp.",
        lambda: api.grad(foo)(2.))

  def test_vjp_rule_inconsistent_pytree_structures_error(self):
    @api.custom_vjp
    def f(x):
      return x

    def foo_fwd(x):
      return x, None

    def foo_bwd(_, g):
      return (g, g)

    f.defvjp(foo_fwd, foo_bwd)

    f(2)  # doesn't crash
    self.assertRaisesRegex(
        TypeError,
        re.escape(
            "Custom VJP rule must produce an output with the same container "
            "(pytree) structure as the args tuple of the primal function, "
            "and in particular must produce a tuple of length equal to the "
            "number of arguments to the primal function, but got VJP output "
            "structure {} for primal input structure {}.".format(
                tree_util.tree_structure((1, 1)),
                tree_util.tree_structure((1,)))
        ),
        lambda: api.grad(f)(2.))

  def test_vjp_bwd_returns_non_tuple_error(self):
    @api.custom_vjp
    def f(x):
      return x

    def foo_fwd(x):
      return x, None

    def foo_bwd(_, g):
      return 2. * g  # Should be a tuple

    f.defvjp(foo_fwd, foo_bwd)
    with self.assertRaisesRegex(TypeError, "Custom VJP rule .* must produce a tuple"):
      api.grad(f)(3.)

  def test_issue2511(self):
    arr = jnp.ones((5, 2, 2))
    foo = lambda x: api.vmap(jnp.linalg.det, (0,))(x)
    api.jit(foo)(arr)  # doesn't crash

  def test_lowering_out_of_traces(self):
    # https://github.com/google/jax/issues/2578

    class F(collections.namedtuple("F", ["a"])):
      def __call__(self, x):
        return jax.nn.relu(self.a) * x

    @jax.jit
    def g(f, x):
      return f(x)

    jax.grad(g, argnums=(1,))(F(2.0), 0.)  # doesn't crash

  def test_nondiff_argnums_stop_gradient(self):
    # This test is now skipped because we decided not to support this behavior
    # anymore (namely, nondiff args can't be tracers), but test_clip_gradient is
    # a replacement showing behavior we do support.
    raise unittest.SkipTest("removed support for tracers in nondiff args")

    # https://github.com/google/jax/issues/2784
    @partial(api.custom_vjp, nondiff_argnums=(0, 1))
    def _clip_gradient(lo, hi, x):
      return x  # identity function

    def clip_gradient_fwd(lo, hi, x):
      # return x, None
      return x, (hi, )

    def clip_gradient_bwd(lo, hi, _, g):
      return (jnp.clip(g, lo, hi),)

    _clip_gradient.defvjp(clip_gradient_fwd, clip_gradient_bwd)

    def clip_gradient(x):
      lo = -1
      hi = x + 1  # causes things to break
      return _clip_gradient(lo, hi, x)

    jax.grad(clip_gradient)(1.)  # doesn't crash

  def test_clip_gradient(self):
    # https://github.com/google/jax/issues/2784
    @api.custom_vjp
    def _clip_gradient(lo, hi, x):
      return x  # identity function when not differentiating

    def clip_gradient_fwd(lo, hi, x):
      return x, (lo, hi,)

    def clip_gradient_bwd(res, g):
      lo, hi = res
      return (None, None, jnp.clip(g, lo, hi),)

    _clip_gradient.defvjp(clip_gradient_fwd, clip_gradient_bwd)

    def clip_gradient(x):
      lo = -0.1
      hi = x + 0.1
      return _clip_gradient(lo, hi, x)

    g = jax.grad(clip_gradient)(0.1)  # doesn't crash
    self.assertAllClose(g, jnp.array(0.2))

  def test_nestable_vjp(self):
    # Verify that https://github.com/google/jax/issues/3667 is resolved.
    def f(x):
      return x ** 2

    @api.custom_vjp
    def g(x):
      return f(x)

    def g_fwd(x):
      y, f_vjp = api.vjp(f, x)
      return y, f_vjp

    def g_bwd(f_vjp, y_bar):
      return f_vjp(y_bar)

    g.defvjp(g_fwd, g_bwd)

    # Check that VJP can be nested in simple situations.  For this to pass,
    # vjp has to return a PyTree.
    _, g_vjp = api.vjp(g, 1.0)
    y, = g_vjp(1.0)
    self.assertAllClose(y, jnp.array(2.0))

    # Check that VJP can be nested in complex situations.  For this to pass,
    # vjp can't treat the closed-over tracer x as a static argument.
    @jit
    def z(x):
      _, g_vjp = api.vjp(g, x)
      return g_vjp
    y, = z(1.0)(3.0)
    self.assertAllClose(y, jnp.array(6.0))

  def test_initial_style_vmap_2(self):
    # https://github.com/google/jax/issues/4173
    x = jnp.ones((10, 3))

    # Create the custom function
    @api.custom_vjp
    def custom_fun(x):
      return x.sum()

    def forward(x):
      return x.sum(), (jnp.ones_like(x),)

    def backward(res, g):
      return g * res[0],

    custom_fun.defvjp(forward, backward)

    def train_fun(x):

      def summed_fun(x):
        return api.vmap(custom_fun)(x).sum()

      return api.grad(summed_fun)(x)

    def scan_body(carry, inputs):
      x = carry
      return carry, train_fun(x)

    scan_range = jnp.arange(4)
    lax.scan(scan_body, x, scan_range)  # don't crash

  def test_initial_style_vmap_3(self):
    # This is like test_initial_style_vmap except the primal function closes
    # over an array constant.
    y = jnp.array([1., 2., 3.])

    @api.custom_vjp
    def f(x):
      assert jnp.ndim(x) == 0
      return 3 * x * jnp.sum(y)
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    def foo(x):
      out, _  = lax.scan(lambda c, _: (f(c), None), x, None, length=1)
      return out

    ans = api.vmap(foo)(jnp.arange(3.))
    expected = 3. * jnp.arange(3.) * 6
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(lambda x: api.vmap(foo)(x).sum())(jnp.arange(3.))
    expected = 2. * jnp.cos(jnp.arange(3.))
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_bwd_closes_over_tracer(self):
    def f(y):
      @jax.custom_vjp
      def f(x):
        return 2. * jnp.sin(x)

      def fwd(x):
        return f(x), ()

      def bwd(_, g):
        return (2. * jnp.cos(y) * g,)  # capture!

      f.defvjp(fwd, bwd)

      return jax.grad(f)(1.)

    ans = jax.jit(f)(2.)
    self.assertAllClose(ans, 2. * jnp.cos(2.))

    ans = jax.vmap(f)(jnp.arange(3.))
    self.assertAllClose(ans, 2. * jnp.cos(jnp.arange(3.)))

    ans = jax.jit(jax.vmap(f))(jnp.arange(3.))
    self.assertAllClose(ans, 2. * jnp.cos(jnp.arange(3.)))

    ans = jax.vmap(jax.jit(f))(jnp.arange(3.))
    self.assertAllClose(ans, 2. * jnp.cos(jnp.arange(3.)))

    ans = jax.grad(f)(4.)
    self.assertAllClose(ans, -2. * jnp.sin(4.))

  def test_fwd_closes_over_tracer(self):
    def f(y):
      @jax.custom_vjp
      def f(x):
        return 2. * jnp.sin(x)

      def fwd(x):
        return f(x), y

      def bwd(y, g):
        return (2. * jnp.cos(y) * g,)  # capture!

      f.defvjp(fwd, bwd)

      return jax.grad(f)(1.)

    ans = jax.jit(f)(2.)
    self.assertAllClose(ans, 2. * jnp.cos(2.))

    ans = jax.vmap(f)(jnp.arange(3.))
    self.assertAllClose(ans, 2. * jnp.cos(jnp.arange(3.)))

    ans = jax.jit(jax.vmap(f))(jnp.arange(3.))
    self.assertAllClose(ans, 2. * jnp.cos(jnp.arange(3.)))

    ans = jax.vmap(jax.jit(f))(jnp.arange(3.))
    self.assertAllClose(ans, 2. * jnp.cos(jnp.arange(3.)))

    ans = jax.grad(f)(4.)
    self.assertAllClose(ans, -2. * jnp.sin(4.))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_float0(self):
    @api.custom_vjp
    def f(x, _):
      return x
    def f_fwd(x, _):
      # we need a defined (non-float0) tangent to trigger the rule
      return x, (2., 1)
    def f_rev(*_):
      return (2., 1)
    f.defvjp(f_fwd, f_rev)

    x = 2.
    y = 3
    self.assertEqual(api.grad(f, allow_int=True, argnums=(0, 1))(x, y),
                     (2., np.zeros(shape=(), dtype=float0)))

  @unittest.skipIf(numpy_version == (1, 21, 0),
                   "https://github.com/numpy/numpy/issues/19305")
  def test_float0_initial_style(self):
    @api.custom_vjp
    def f(x):
      return x
    def f_fwd(x):
      return x, (2., x)
    def f_rev(*_):
      return ((2., 1),)
    f.defvjp(f_fwd, f_rev)

    def foo(x, y):
      out, _ = lax.scan(lambda c, _: (f(c), None), (x, y), None, length=1)
      return out[0]

    x = 2.
    y = 3
    self.assertEqual(api.grad(foo, allow_int=True, argnums=(0, 1))(x, y),
                     (2., np.zeros(shape=(), dtype=float0)))

  def test_remat(self):
    @api.custom_vjp
    def f(x):
      return jnp.sin(x)
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    @api.remat
    def g(x):
      return f(f(x))

    ans = g(2.)
    expected = np.sin(np.sin(2.))
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(g)(2.)
    expected = 4. * api.grad(lambda x: jnp.sin(jnp.sin(x)))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_remat_higher_order(self):
    @api.custom_vjp
    def f(x):
      return jnp.sin(x)
    def f_fwd(x):
      return f(x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (2 * cos_x * g,)
    f.defvjp(f_fwd, f_rev)

    def g(x):
      return f(f(x))

    ans = api.grad(api.grad(api.remat(g)))(2.)
    expected = api.grad(api.grad(g))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.remat(api.grad(g)))(2.)
    expected = api.grad(api.grad(g))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

    ans = api.grad(api.grad(api.grad(api.remat(g))))(2.)
    expected = api.grad(api.grad(api.grad(g)))(2.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_bwd_nones(self):
    @api.custom_vjp
    def f(x, y):
      return x * jnp.sin(y)
    def f_fwd(x, y):
      return f(x, y), jnp.cos(y)
    def f_rev(cos, g):
      return (None, 2 * cos * g)
    f.defvjp(f_fwd, f_rev)

    ans = api.grad(lambda x: f(x, x))(3.)
    expected = 2 * jnp.cos(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_bwd_nones_vmap(self):
    @api.custom_vjp
    def f(x, y):
      return x * jnp.sin(y)
    def f_fwd(x, y):
      return f(x, y), jnp.cos(y)
    def f_rev(cos, g):
      return (None, 2 * cos * g)
    f.defvjp(f_fwd, f_rev)

    ans = api.grad(lambda x: api.vmap(f)(x, x).sum())(jnp.arange(3.))
    expected = 2 * jnp.cos(jnp.arange(3.))
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_bwd_nones_pytree(self):
    @api.custom_vjp
    def f(xs, y):
      x1, x2 = xs
      return x1 * x2 * jnp.sin(y)
    def f_fwd(xs, y):
      return f(xs, y), jnp.cos(y)
    def f_rev(cos, g):
      return (None, 2 * cos * g)
    f.defvjp(f_fwd, f_rev)

    ans = api.grad(lambda x: f((x, x), x))(3.)
    expected = 2 * jnp.cos(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_custom_vjp_closure_4521(self):
    # https://github.com/google/jax/issues/4521
    @api.custom_vjp
    def g(x, y):
      return None
    def g_fwd(x, y):
      return None, y
    def g_bwd(residuals, z_bar):
      assert False

    g.defvjp(g_fwd, g_bwd)

    def f(xs, y):
      v_g = api.vmap(g, in_axes=(0, None), out_axes=None)
      v_g(xs, y)

    def scan_body(xs, _):
      y = jnp.zeros(1)
      _, vjp_f = api.vjp(f, xs, y)
      vjp_f(None)
      return xs, None

    lax.scan(scan_body, jnp.ones(5), None, 100)  # doesn't crash

  def test_float0_bwd_none(self):
    @api.custom_vjp
    def f(i, x):
      return jnp.sin(x)
    def f_fwd(i, x):
      return f(i, x), jnp.cos(x)
    def f_rev(cos_x, g):
      return (None, 2 * cos_x * g)
    f.defvjp(f_fwd, f_rev)

    ans = api.grad(f, 1)(jnp.array([1, 2]), 3.)  # doesn't crash
    expected = 2 * jnp.cos(3.)
    self.assertAllClose(ans, expected, check_dtypes=False)

  def test_custom_gradient(self):
    @api.custom_gradient
    def f(x):
      return x ** 2, lambda g: (g * x,)

    self.assertAllClose(f(3.), 9., check_dtypes=False)
    self.assertAllClose(api.grad(f)(3.), 3., check_dtypes=False)
    self.assertAllClose(api.grad(api.grad(f))(3.), 1., check_dtypes=False)

  def test_custom_gradient_2(self):
    @api.custom_gradient
    def f(x, y):
      return x * y, lambda g: (y, x)

    self.assertAllClose(f(3., 4.), 12., check_dtypes=False)
    self.assertAllClose(api.grad(f, argnums=(0, 1))(3., 4.), (4., 3.),
                        check_dtypes=False)

  def test_custom_gradient_3(self):
    @api.custom_gradient
    def f(x):
      vjp = lambda g: (jnp.cos(x) * jnp.array([3., 4., 5.]),)
      return jnp.sum(jnp.sin(x)), vjp

    self.assertAllClose(f(jnp.arange(3)), jnp.sum(jnp.sin(jnp.arange(3.))),
                        check_dtypes=False)
    self.assertAllClose(
        api.grad(f)(jnp.arange(3.)),
        api.grad(lambda x: jnp.sum(jnp.sin(x)))(jnp.arange(3.)) * jnp.array([3., 4., 5.]),
        check_dtypes=False)

  def test_custom_gradient_can_return_singleton_value_in_vjp(self):
    @api.custom_gradient
    def f(x):
      return x ** 2, lambda g: g * x

    self.assertAllClose(f(3.), 9., check_dtypes=False)
    self.assertAllClose(api.grad(f)(3.), 3., check_dtypes=False)
    self.assertAllClose(api.grad(api.grad(f))(3.), 1., check_dtypes=False)

  def test_closure_convert(self):
    def minimize(objective_fn, x0):
      converted_fn, aux_args = api.closure_convert(objective_fn, x0)
      return _minimize(converted_fn, x0, *aux_args)

    @partial(api.custom_vjp, nondiff_argnums=(0,))
    def _minimize(objective_fn, x0, *args):
      _ = objective_fn(x0, *args)
      return jnp.cos(x0)

    def fwd(objective_fn, x0, *args):
      y = _minimize(objective_fn, x0, *args)
      return y, (y, args)

    def rev(objective_fn, res, g):
      y, args = res
      x0_bar = 17. * y
      args_bars = [42. * a for a in args]
      return (x0_bar, *args_bars)

    _minimize.defvjp(fwd, rev)

    def obj(c, x):
      return jnp.sum((x - c) ** 2.)

    def solve(c, x):
      def closure(x):
        return obj(c, x)
      return jnp.sum(minimize(closure, x))

    c, x = jnp.ones(2), jnp.zeros(2)
    self.assertAllClose(solve(c, x), 2.0, check_dtypes=False)
    g_c, g_x = api.grad(solve, argnums=(0, 1))(c, x)
    self.assertAllClose(g_c, 42. * jnp.ones(2), check_dtypes=False)
    self.assertAllClose(g_x, 17. * jnp.ones(2), check_dtypes=False)


class CustomTransposeTest(jtu.JaxTestCase):

  def transpose(self, f, x_example):
    def transposed(y):
      x, = api.linear_transpose(f, x_example)(y)
      return x
    return transposed

  def test_linear_call(self):
    def f(x, y):
      def fn(r, x): return x / r
      def tp(r, t): return t / r
      return x + api.linear_call(fn, tp, y, x)

    def f_ref(x, y):
      return x + x / y

    x = jnp.ones(2) * 6.
    y = jnp.ones(2) * 3.
    self.assertAllClose(f(x, y), f_ref(x, y))

    f1     = lambda x: f(x, y)
    f1_ref = lambda x: f_ref(x, y)
    self.assertAllClose(self.transpose(f1,     x)(x),
                        self.transpose(f1_ref, x)(x))

  def test_linear_call_incorrect_transpose(self):
    def f(x, y):
      def fn(r, x): return x / r
      def tp(r, t): return t / (2. * r)  # nb: not the true transpose
      return x + api.linear_call(fn, tp, y, x)

    def f_ref(x, y):
      return x + x / y

    x = jnp.ones(2) * 6.
    y = jnp.ones(2) * 3.
    self.assertAllClose(f(x, y), f_ref(x, y))

    f1     = lambda x: f(x, y)
    f1_ref = lambda x: f_ref(x, 2. * y)  # nb: double the reference divisor
    self.assertAllClose(self.transpose(f1,     x)(x),
                        self.transpose(f1_ref, x)(x))

  def test_linear_call_transpose_transpose_transpose(self):
    def fn(r, x): return x / r
    def tp(r, t): return t / (2. * r)  # nb: untrue transpose
    def f_(x, y):
      return x + api.linear_call(fn, tp, y, x)

    x = jnp.ones(2) * 6.
    y = jnp.ones(2) * 3.
    f = lambda x: f_(x, y)
    ft   = self.transpose(f,   x)
    ftt  = self.transpose(ft,  x)
    fttt = self.transpose(ftt, x)
    self.assertAllClose(ft(x), x + tp(y, x))
    self.assertAllClose(f(x),  ftt(x))
    self.assertAllClose(ft(x), fttt(x))

  def test_linear_call_scalar_to_vector(self):
    def f(c, x):
      def fn(_, x):
        return [x, x]

      def tp(_, t):
        t1, t2 = t
        return t1 + t2

      return api.linear_call(fn, tp, (), c * x)

    def f_ref(c, x):
      return [c * x, c * x]

    c, x = 2., 3.
    t = [4., 5.]
    self.assertAllClose(f(c, x), f_ref(c, x))
    self.assertAllClose(self.transpose(partial(f,     c), x)(t),
                        self.transpose(partial(f_ref, c), x)(t))

  def test_linear_call_nested(self):
    # identity function with an untrue transpose of 0
    def id_(x):
      def f(_, x): return x
      def t(_, t): return 0.
      return api.linear_call(f, t, (), x)

    # identity function with an untrue transpose of 7, and where both
    # forward and transpose have custom transpositions that should
    # never end up invoked.
    def f(x):
      def f_(_, x): return id_(x)
      def t_(_, t): return id_(7.)
      return api.linear_call(f_, t_, (), x)

    x = 5.
    id_t  = self.transpose(id_,  x)
    id_tt = self.transpose(id_t, x)
    ft   = self.transpose(f,    x)
    ftt  = self.transpose(ft,   x)
    fttt = self.transpose(ftt,  x)

    self.assertAllClose(id_(x),   x)
    self.assertAllClose(id_t(x),  0.)
    self.assertAllClose(id_tt(x), x)

    self.assertAllClose(f(x),    x)
    self.assertAllClose(ft(x),   7.)
    self.assertAllClose(ftt(x),  x)
    self.assertAllClose(fttt(x), 7.)


class InvertibleADTest(jtu.JaxTestCase):

  @jtu.ignore_warning(message="Values that an @invertible function closes")
  def test_invertible_basic(self):
    def f(x):
      return (jnp.exp(x) * 4) * x

    finv = jax.invertible(f)
    x = jnp.ones((5,))

    jaxpr = jax.make_jaxpr(lambda p, ct: jax.vjp(finv, p)[1](ct))(x, x)

    # expected = """
    # { lambda  ; a b.
    #   let c = exp a
    #       d = mul c 4.0
    #       e = mul d a
    #       f = mul b a
    #       g = div e a
    #       h = mul b g
    #       i = mul f 4.0
    #       j = div g 4.0
    #       k = mul f j
    #       _ = reduce_sum[ axes=(0,) ] k
    #       _ = log j
    #       l = mul i j
    #       m = add_any h l
    #   in (m,) }
    # """
    # self.assertMultiLineStrippedEqual(expected, str(jaxpr))  # no jaxpr test

    self.assertIn('div', str(jaxpr))
    self.assertIn('log', str(jaxpr))  # assumes no DCE
    self.assertAllClose(jax.value_and_grad(lambda x: np.sum(f(x)))(x),
                        jax.value_and_grad(lambda x: np.sum(finv(x)))(x),
                        check_dtypes=True)

  def test_invertible_blocks(self):
    # NB: This is the reversible ResNet block
    def mk_reversible_block(f, g):
      @jax.custom_ivjp
      def rev_block(x1, x2):
        y1 = f(x2) + x1
        y2 = g(y1) + x2
        return y1, y2

      @rev_block.defivjp
      def rev_block_ivjp(xs, ys, dys):
        (y1, y2) = ys
        (dy1, dy2) = dys

        dgo, dx2 = dy2, dy2
        go, gvjp = jax.vjp(g, y1)
        dy1 += gvjp(dgo)[0]
        del gvjp
        x2 = y2 - go

        dfo, dx1 = dy1, dy1
        fo, fvjp = jax.vjp(f, x2)
        dx2 += fvjp(dfo)[0]
        del fvjp
        x1 = y1 - fo

        return (x1, x2), (dx1, dx2)

      return rev_block

    rev_block = mk_reversible_block(jnp.sin, jnp.cos)

    def g(x1, x2):
      for i in range(2):
        x1, x2 = rev_block(x1, x2)
      return x1, x2

    def reduce(f, x1, x2):
      y1, y2 = f(x1, x2)
      return np.sum(y1) + np.sum(y2)

    x = np.ones((1,))
    # FIXME: This breaks when argnums is left as default (i.e. 0), because JVP prunes
    #        zero tangents from call primitives.
    self.assertAllClose(jax.value_and_grad(partial(reduce, jax.invertible(g)), argnums=(0, 1))(x, x + 2),
                        jax.value_and_grad(partial(reduce, g), argnums=(0, 1))(x, x + 2),
                        check_dtypes=True)

  def test_invertible_partial_diff(self):
    # Check that we don't have to differentiate with respect to inputs
    # of the invertible function.
    def f(x, y):
      return (jnp.exp(x) * 4) * x, y + 4

    finv = jax.invertible(f)
    o = np.ones((5,))
    self.assertAllClose(jax.value_and_grad(lambda x: np.sum(f(x, o)[0]))(o),
                        jax.value_and_grad(lambda x: np.sum(finv(x, o)[0]))(o),
                        check_dtypes=True)

  def test_invertible_pytree(self):
    def f(x, y):
      return jnp.exp(x[0]) * x[1] + y

    finv = jax.invertible(f)
    o = np.ones((5,))
    self.assertAllClose(jax.value_and_grad(lambda x: np.sum(f((x, x), x)[0]))(o),
                        jax.value_and_grad(lambda x: np.sum(finv((x, x), x)[0]))(o),
                        check_dtypes=True)


class BufferDonationTest(jtu.BufferDonationTestCase):

  @jtu.skip_on_devices("cpu")  # In/out aliasing not supported on CPU.
  def test_pmap_donate_argnums_invalidates_input(self):
    move = api.pmap(lambda x: x + x - x, donate_argnums=0)
    n = jax.local_device_count()
    x = api.pmap(lambda x: x)(jnp.ones([n]))
    y = move(x)
    self.assertDeleted(x)
    np.testing.assert_allclose(y, [1.] * n)

  def test_pmap_nested_donate_ignored(self):
    pmap_fun = jit(lambda x: api.pmap(lambda y: y ** 2, donate_argnums=0)(x))
    a = api.pmap(lambda x: x)(jnp.array([1]))

    # NOTE(mattjj): stopped raising error here and instead just ignored
    # with self.assertRaisesRegex(ValueError, "nested.*not supported"):
    #   pmap_fun(a)

    pmap_fun(a)  # doesn't crash


class NamedCallTest(jtu.JaxTestCase):

  def test_default_name(self):

    @api.named_call
    def my_test_function(x):
      return x**2

    @jax.jit
    def f(x):
      return my_test_function(x)

    c = jax.xla_computation(f)(2)
    self.assertIn("my_test_function", c.as_hlo_text())

  def test_non_jaxtype_arg(self):
    # For the test to fail without the invalid JaxType filter we need to pass
    # in a valid JaxType that forces the invalid Jaxtype to be raised to an
    # abstract value.
    def f(not_a_jaxtype, a_jaxtype):
      # then Jax needs to try and evaluate the abstractified non-JaxType
      if not_a_jaxtype:
        return a_jaxtype
      return 0

    f = api.named_call(f, name="test")
    out = jax.jit(f, static_argnums=(0,))("not a Jaxtype", 1)
    self.assertEqual(out, 1)

  @parameterized.parameters(jax.jit, jax.grad, jax.vmap, jax.remat)
  def test_jax_transforms(self, transform):
    f = jnp.sum
    x = jnp.array([1.])

    unnamed_out = transform(f)(x)
    named_out = transform(api.named_call(f, name="test"))(x)

    self.assertEqual(unnamed_out, named_out)

  def test_static_argnums(self):
    f = api.named_call(lambda x, y: y if x else None, name="test")
    f = jax.jit(f, static_argnums=(0,))
    out = f(True, 5)
    self.assertEqual(out, 5)

  def test_partial_eval(self):
    f = api.named_call(lambda x, y: y if x else None, name="test")
    f = jax.jit(functools.partial(f, True))
    out = f(5)
    self.assertEqual(out, 5)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_jit_type={}_func={}".format(jit_type, func),
       "jit_type": jit_type, "func": func}
      for func in ['identity', 'asarray', 'device_put']
      for jit_type in [None, "python", "cpp"]
      if not (jit_type is None and func == 'identity')))
  def test_integer_overflow(self, jit_type, func):
    funcdict = {
      'identity': lambda x: x,
      'asarray': jnp.asarray,
      'device_put': api.device_put,
    }
    jit = {
      'python': api._python_jit,
      'cpp': api._cpp_jit,
      None: lambda x: x,
    }
    f = jit[jit_type](funcdict[func])

    int_dtype = dtypes.canonicalize_dtype(jnp.int_)
    int_max = np.iinfo(int_dtype).max
    int_min = np.iinfo(int_dtype).min

    self.assertEqual(f(int_max).dtype, int_dtype)
    self.assertEqual(f(int_min).dtype, int_dtype)
    self.assertRaises(OverflowError, f, int_max + 1)
    self.assertRaises(OverflowError, f, int_min - 1)


class BackendsTest(jtu.JaxTestCase):

  @unittest.skipIf(not sys.executable, "test requires sys.executable")
  @unittest.skipIf(python_version < (3, 7), "test requires Python 3.7 or higher")
  @jtu.skip_on_devices("gpu", "tpu")
  def test_cpu_warning_suppression(self):
    warning_expected = (
      "import jax; "
      "jax.numpy.arange(10)")
    warning_not_expected = (
      "import jax; "
      "jax.config.update('jax_platform_name', 'cpu'); "
      "jax.numpy.arange(10)")

    result = subprocess.run([sys.executable, '-c', warning_expected],
                            check=True, capture_output=True)
    assert "No GPU/TPU found" in result.stderr.decode()

    result = subprocess.run([sys.executable, '-c', warning_not_expected],
                            check=True, capture_output=True)
    assert "No GPU/TPU found" not in result.stderr.decode()


if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
