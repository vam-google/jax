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

"""Test TPU-specific extensions to pallas_call."""

from absl.testing import absltest
from absl.testing import parameterized
import jax
from jax import lax
from jax._src import state
from jax._src import test_util as jtu
from jax._src.interpreters import partial_eval as pe
from jax._src.lib import xla_extension
from jax.experimental import mesh_utils
from jax.experimental import mosaic
from jax.experimental import pallas as pl
from jax.experimental import shard_map
from jax.experimental.pallas import tpu as pltpu
from jax.experimental.pallas.ops.tpu import example_kernel
from jax.extend import linear_util as lu
import jax.numpy as jnp
import numpy as np


jax.config.parse_flags_with_absl()

P = jax.sharding.PartitionSpec


class PallasCallScalarPrefetchTest(jtu.JaxTestCase):
  interpret: bool = False

  def setUp(self):
    super().setUp()
    if not self.interpret and jtu.device_under_test() != "tpu":
      self.skipTest("Only interpret mode supported on non-TPU")

  def test_trivial_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    def _x_transform(i, s_ref):
      s = pl.load(s_ref, (i,))
      return (s, 0)

    out = pl.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[
                pl.BlockSpec(_x_transform, (x.shape[0] // 8, x.shape[1])),
            ],
            out_specs=pl.BlockSpec(lambda i, _: (i, 0),
                                   (x.shape[0] // 8, x.shape[1])),
            grid=8,
        ),
        interpret=self.interpret,
    )(s, x)
    np.testing.assert_allclose(out, x.reshape((8, 8, -1))[s].reshape(x.shape))

  def test_trivial_scalar_prefetch_with_windowless_args(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(8 * 8 * 128, dtype=jnp.int32).reshape((8 * 8, 128))

    out = pl.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
        ),
        interpret=self.interpret,
    )(s, x)
    np.testing.assert_array_equal(out, x)

  def test_vmap_scalar_prefetch(self):
    def body(_, x_ref, o_ref):
      o_ref[...] = x_ref[...]

    s = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    x = jnp.arange(2 * 8 * 8 * 128, dtype=jnp.int32).reshape((2, 8 * 8, 128))

    def _x_transform(i, s_ref):
      s = pl.load(s_ref, (i,))
      return (s, 0)

    def f(x):
      return pl.pallas_call(
          body,
          out_shape=jax.ShapeDtypeStruct(x.shape, jnp.int32),
          grid_spec=pltpu.PrefetchScalarGridSpec(
              num_scalar_prefetch=1,
              in_specs=[
                  pl.BlockSpec(_x_transform, (x.shape[0] // 8, x.shape[1])),
              ],
              out_specs=pl.BlockSpec(lambda i, _: (i, 0),
                                     (x.shape[0] // 8, x.shape[1])),
              grid=8,
          ),
          interpret=self.interpret,
      )(s, x)
    np.testing.assert_allclose(
        jax.vmap(f)(x), x.reshape((2, 8, 8, -1))[:, s].reshape(x.shape)
    )

  def test_multiple_scalar_prefetch(self):
    def body(s1_ref, s2_ref, x_ref, o_ref):
      del s1_ref, s2_ref
      o_ref[...] = x_ref[...]

    s1 = jnp.array([4, 3, 2, 5, 3, 5, 2, 7], jnp.int32)
    s2 = jnp.array([7, 6, 5, 4, 3, 2, 1, 0], jnp.int32)
    x = jnp.arange(64 * 128, dtype=jnp.int32).reshape((64, 128))

    def _x_transform(i, s1_ref, _):
      return s1_ref[i], 0

    def _o_transform(i, _, s2_ref):
      return s2_ref[i], 0

    out = pl.pallas_call(
        body,
        out_shape=jax.ShapeDtypeStruct((64, 128), jnp.int32),
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=2,
            in_specs=[
                pl.BlockSpec(_x_transform, (8, 128)),
            ],
            out_specs=pl.BlockSpec(_o_transform, (8, 128)),
            grid=8,
        ),
        interpret=self.interpret,
    )(s1, s2, x)
    out_ref = x.reshape((8, 8, -1))[s1][::-1].reshape((64, 128))
    np.testing.assert_allclose(out, out_ref)

  def test_scalar_interpreter(self):
    program = jnp.array([0, 0, 1, 0, 1, 1], jnp.int32)
    x = jnp.arange(8 * 8 * 128.0, dtype=jnp.float32).reshape(8 * 8, 128)

    def body(sprogram_ref, x_ref, o_ref, state_ref):
      x = x_ref[...]

      def add_branch_fn(j):
        state_ref[...] += jnp.float32(j)
        return ()

      def mult_branch_fn(j):
        state_ref[...] *= jnp.float32(j)
        return ()

      def single_inst(i, _):
        _ = jax.lax.switch(
            sprogram_ref[i],
            (
                add_branch_fn,
                mult_branch_fn,
            ),
            i,
        )

      # We can't use for loop state right now, because Pallas functionalizes it,
      # and Mosaic support for returning values form scf.if is incomplete.
      state_ref[...] = x
      lax.fori_loop(0, sprogram_ref.shape[0], single_inst, None, unroll=True)
      o_ref[...] = state_ref[...]

    # Ignore the scratch output.
    out, _ = pl.pallas_call(
        body,
        out_shape=[
            jax.ShapeDtypeStruct(x.shape, jnp.float32),
            jax.ShapeDtypeStruct((8, 128), jnp.float32),
        ],
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=1,
            in_specs=[pl.BlockSpec(lambda i, *_: (i, 0), (8, 128))],
            out_specs=[
                pl.BlockSpec(lambda i, *_: (i, 0), (8, 128)),
                pl.BlockSpec(lambda *_: (0, 0), (8, 128)),
            ],
            grid=8,
        ),
        interpret=self.interpret,
        debug=False,
    )(program, x)

    expected = x
    for i, p in enumerate(program):
      if p == 0:
        expected += i
      elif p == 1:
        expected *= i

    np.testing.assert_allclose(out, expected)


class PallasCallScalarPrefetchInterpretTest(PallasCallScalarPrefetchTest):
  interpret: bool = True


class PallasCallDMATest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    if not jtu.is_device_tpu_at_least(4):
      self.skipTest("DMAs not supported on TPU generations <= 3")

  def test_can_have_unspecified_memory_spaces(self):
    def kernel(x_ref, y_ref):
      # Just test whether things compile
      del x_ref, y_ref

    x = jnp.ones((8, 128), dtype=jnp.float32)
    y = pl.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(None, None, pltpu.TPUMemorySpace.ANY)],
        out_specs=pl.BlockSpec(None, None, pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    jax.block_until_ready(y)

  def test_run_scoped_tracks_effects(self):
    def kernel(x_ref, y_ref):
      def body(temp_ref):
        temp_ref[...] = jnp.ones_like(temp_ref)
        x_ref[...] = 4 * y_ref[...] + temp_ref[...]

      pltpu.run_scoped(body, pltpu.VMEM((8,), jnp.float32))
      return []

    jaxpr, _, _ = pe.trace_to_jaxpr_dynamic(
        lu.wrap_init(kernel),
        [
            state.shaped_array_ref((8,), jnp.float32),
            state.shaped_array_ref((8,), jnp.float32),
        ],
    )
    expected_effects = {state.ReadEffect(1), state.WriteEffect(0)}
    self.assertSetEqual(jaxpr.effects, expected_effects)

  def test_scoped_allocation(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.ones_like(x_ref)
        y_ref[...] = 4 * x_ref[...]

      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32))

    o = pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )()
    np.testing.assert_allclose(o, 4 * np.ones_like(o))

  def test_nested_scoped_allocation(self):
    def kernel(y_ref):
      def body(x_ref):
        x_ref[...] = jnp.zeros_like(x_ref)
        def inner_body(z_ref):
          z_ref[...] = jnp.ones_like(z_ref)
          x_ref[...] = z_ref[...]
        pltpu.run_scoped(inner_body, pltpu.VMEM((8, 128), jnp.float32))
        y_ref[...] = 4 * x_ref[...]
      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32))

    o = pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )()
    np.testing.assert_allclose(o, 4 * np.ones_like(o))

  def test_can_allocate_semaphore(self):
    def kernel(y_ref):
      def body(sem1):
        pass
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)

    jax.block_until_ready(pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_multiple_semaphores(self):
    def kernel(y_ref):
      def body(sem1, sem2):
        pass
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA,
                       pltpu.SemaphoreType.REGULAR)

    jax.block_until_ready(pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_semaphore_array(self):
    def kernel(y_ref):
      def body(dma_sems, sems):
        self.assertTupleEqual(dma_sems.shape, (4,))
        self.assertTupleEqual(sems.shape, (3,))
        self.assertTrue(jnp.issubdtype(dma_sems.dtype, pltpu.dma_semaphore))
        self.assertTrue(jnp.issubdtype(sems.dtype, pltpu.semaphore))
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA((4,)),
                       pltpu.SemaphoreType.REGULAR((3,)))

    jax.block_until_ready(pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_allocate_scratch_semaphore_array(self):
    def kernel(y_ref, dma_sems, sems):
      self.assertTupleEqual(dma_sems.shape, (4,))
      self.assertTupleEqual(sems.shape, (3,))
      self.assertTrue(jnp.issubdtype(dma_sems.dtype, pltpu.dma_semaphore))
      self.assertTrue(jnp.issubdtype(sems.dtype, pltpu.semaphore))

    jax.block_until_ready(
        pl.pallas_call(
            kernel,
            out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=0,
                scratch_shapes=[
                    pltpu.SemaphoreType.DMA((4,)),
                    pltpu.SemaphoreType.REGULAR((3,)),
                ],
            ),
        )()
    )

  def test_can_wait_on_semaphore(self):
    def kernel(y_ref):
      def body(sem):
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_wait(sem)
      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR)
      def body2(sem):
        pltpu.semaphore_signal(sem, 2)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
      pltpu.run_scoped(body2, pltpu.SemaphoreType.REGULAR)
      def body3(sem):
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_signal(sem)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
        pltpu.semaphore_wait(sem)
      pltpu.run_scoped(body3, pltpu.SemaphoreType.REGULAR)

    jax.block_until_ready(pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_wait_on_semaphore_array(self):
    def kernel(y_ref):
      def body(sems):
        pltpu.semaphore_signal(sems.at[0])
        pltpu.semaphore_wait(sems.at[0])

        pltpu.semaphore_signal(sems.at[1], 2)
        pltpu.semaphore_wait(sems.at[1])
        pltpu.semaphore_wait(sems.at[1])

        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_signal(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
        pltpu.semaphore_wait(sems.at[2])
      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR((3,)))

    jax.block_until_ready(pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )())

  def test_can_wait_on_semaphore_array_with_dynamic_index(self):
    def kernel(y_ref):
      i = pl.program_id(0)
      def body(sems):
        pltpu.semaphore_signal(sems.at[i, 0])
        pltpu.semaphore_wait(sems.at[i, 0])

        pltpu.semaphore_signal(sems.at[i, 1], 2)
        pltpu.semaphore_wait(sems.at[i, 1])
        pltpu.semaphore_wait(sems.at[i, 1])

        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_signal(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
        pltpu.semaphore_wait(sems.at[i, 2])
      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR((4, 3)))

    jax.block_until_ready(pl.pallas_call(
        kernel,
        in_specs=[],
        out_specs=pl.BlockSpec(lambda i: (0, 0), (8, 128)),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        grid=4,
        debug=True,
    )())

  def test_hbm_hbm_dma(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_cannot_dma_with_nonscalar_semaphore_ref(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA((1,)))
    with self.assertRaisesRegex(ValueError, "Cannot signal"):
      x = jnp.arange(8 * 128.).reshape((8, 128))
      pl.pallas_call(
          kernel,
          in_specs=[
              pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
          ],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
      )(x)

  def test_dma_with_scalar_semaphore_ref(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], y_hbm_ref.at[:, pl.ds(128)],
                         sem.at[0]).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA((1,)))
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)

  def test_hbm_hbm_grid_dma(self):
    # When using the grid, we have to emit Mosaic window_params. Test that they
    # work correctly with ANY memory space operands.
    def kernel(x_hbm_ref, y_hbm_ref):
      i = pl.program_id(0)
      def body(sem):
        pltpu.async_copy(
            x_hbm_ref.at[pl.ds(i, 1)], y_hbm_ref.at[pl.ds(i, 1)], sem
        ).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((2, 8, 128), jnp.float32),
        grid=(2,),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma(self):
    def kernel(x_hbm_ref, y_ref):
      def body(x_ref, sem):
        pltpu.async_copy(x_hbm_ref.at[pl.ds(8), :], x_ref.at[:, pl.ds(128)],
                         sem).wait()
        y_ref[...] = x_ref[...]
      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_vmem_hbm_dma(self):
    def kernel(x_ref, y_hbm_ref):
      def body(y_ref, sem):
        y_ref[...] = x_ref[...]
        pltpu.async_copy(y_hbm_ref, y_ref, sem).wait()
      pltpu.run_scoped(body, pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_vmem_hbm_vmem_dma(self):
    def kernel(x_hbm_ref, y_hbm_ref):
      def body(x_ref, y_ref, sem):
        pltpu.async_copy(x_hbm_ref, x_ref, sem).wait()
        y_ref[...] = x_ref[...]
        pltpu.async_copy(y_ref, y_hbm_ref, sem).wait()
      pltpu.run_scoped(body,
                       pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.VMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY)],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_smem_dma(self):
    def kernel(x_hbm_ref, y_ref):
      def body(x_ref, sem):
        pltpu.async_copy(x_hbm_ref, x_ref, sem).wait()
        y_ref[...] = x_ref[0, 0] * jnp.ones_like(y_ref)
      pltpu.run_scoped(body, pltpu.SMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = 4 * jnp.ones((8, 128), jnp.float32)
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_smem_hbm_dma(self):
    def kernel(x_ref, y_hbm_ref):
      def body(y_ref, sem):
        y_ref[0, 0] = x_ref[4, 4]
        pltpu.async_copy(y_ref, y_hbm_ref, sem).wait()
      pltpu.run_scoped(body, pltpu.SMEM((8, 128), jnp.float32),
                       pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.SMEM),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    expected = jnp.zeros_like(x).at[0, 0].set(x[4, 4])
    np.testing.assert_allclose(y, expected)

  def test_vmem_vmem_dma(self):
    def kernel(x_ref, y_ref):
      def body(sem):
        pltpu.async_copy(x_ref, y_ref, sem).wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma_slicing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[pl.ds(0, 8)], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[pl.ds(8, 8)], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((16, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x)

  def test_hbm_vmem_dma_indexing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[0], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[1], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x.reshape((16, 128)))

  def test_hbm_vmem_dma_multiple_indexing(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        for i in range(3):
          dma1 = pltpu.async_copy(
              x_hbm_ref.at[pl.ds(i, 1)].at[0, 0], y_ref.at[i].at[pl.ds(0, 8)],
              sem
          )
          dma2 = pltpu.async_copy(
              x_hbm_ref.at[pl.ds(i, 1)].at[0, 1], y_ref.at[i].at[pl.ds(8, 8)],
              sem
          )
          dma1.wait()
          dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(3 * 2 * 8 * 128.).reshape((3, 2, 8, 128))
    y = pl.pallas_call(
        kernel,
        in_specs=[
            pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
        ],
        out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        out_shape=jax.ShapeDtypeStruct((3, 16, 128), jnp.float32),
    )(x)
    np.testing.assert_allclose(y, x.reshape((3, 16, 128)))

  def test_cannot_squeeze_lane_sublane(self):
    def kernel(x_hbm_ref, y_ref):
      def body(sem):
        dma1 = pltpu.async_copy(
            x_hbm_ref.at[:, :, 0], y_ref.at[pl.ds(0, 8)], sem
        )
        dma2 = pltpu.async_copy(
            x_hbm_ref.at[:, :, 1], y_ref.at[pl.ds(8, 8)], sem
        )
        dma1.wait()
        dma2.wait()
      pltpu.run_scoped(body, pltpu.SemaphoreType.DMA)
    x = jnp.arange(2 * 8 * 128.).reshape((2, 8, 128))
    with self.assertRaises(Exception):
      _ = pl.pallas_call(
          kernel,
          in_specs=[
              pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
          ],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=jax.ShapeDtypeStruct((16, 128), jnp.float32),
      )(x)

  @parameterized.named_parameters(
      ('', False),
      ('_interpret', True),
  )
  def test_hoisted_scratch_space(self, interpret):
    def kernel(x_ref, y_ref, scratch_ref):
      i = pl.program_id(0)
      @pl.when(i == 0)
      def _():
        scratch_ref[...] = x_ref[...]
      scratch_ref[...] += jnp.ones_like(scratch_ref)

      @pl.when(i == 2)
      def _():
        y_ref[...] = scratch_ref[...]

    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(lambda i: (0, 0), (8, 128)),
            ],
            scratch_shapes=[pltpu.VMEM((8, 128), jnp.float32)],
            out_specs=pl.BlockSpec(lambda i: (0, 0), (8, 128)),
            grid=(3,),
        ),
        interpret=interpret,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x + 3)

  def test_hoisted_smem_space(self):
    # TODO(sharadmv,apaszke): enable SMEM scratch spaces
    # TODO(sharadmv,apaszke): add support for ()-shaped SMEM refs
    self.skipTest('Currently doesn\'t work')
    def kernel(y_ref, scratch_ref):
      scratch_ref[0, 0] = pl.program_id(0)
      y_ref[...] = jnp.broadcast_to(scratch_ref[0, 0], y_ref.shape)

    y = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[],
            scratch_shapes=[pltpu.SMEM((1, 1), jnp.int32)],
            out_specs=pl.BlockSpec(lambda i: (i, 0, 0), (None, 8, 128)),
            grid=(2,),
        ),
        debug=True,
        out_shape=jax.ShapeDtypeStruct((2, 8, 128), jnp.int32),
    )()
    expected = jnp.broadcast_to(jnp.arange(2, dtype=jnp.int32)[..., None, None],
                                (2, 8, 128))
    np.testing.assert_array_equal(y, expected)

  def test_hoisted_semaphore(self):
    def kernel(x_bbm_ref, y_ref, sem, dma_sem):
      pltpu.semaphore_signal(sem)
      pltpu.semaphore_wait(sem)
      pltpu.async_copy(x_bbm_ref, y_ref, dma_sem).wait()

    x = jnp.arange(8 * 128.).reshape((8, 128))
    y = pl.pallas_call(
        kernel,
        grid_spec=pltpu.PrefetchScalarGridSpec(
            num_scalar_prefetch=0,
            in_specs=[
                pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.ANY),
            ],
            scratch_shapes=[pltpu.SemaphoreType.REGULAR,
                            pltpu.SemaphoreType.DMA],
            out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
        ),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
    )(x)
    np.testing.assert_array_equal(y, x)


class PallasCallRemoteDMATest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    if jax.device_count() < 2:
      self.skipTest('Only >=2 devices are supported.')
    if not jtu.is_device_tpu_at_least(5):
      self.skipTest('Only works with TPU v5')

  @parameterized.named_parameters(
      ('vmem', pltpu.TPUMemorySpace.VMEM),
      ('hbm', pltpu.TPUMemorySpace.ANY),
  )
  def test_basic_remote_vmem_dma(self, mem):
    # Implements very simple collective permute
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        dev_id = pltpu.device_id()
        other_dev_id = 1 - dev_id
        pltpu.semaphore_signal(ready_sem, device_id=other_dev_id,
                               device_id_type=pltpu.DeviceIdType.LOGICAL)
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, other_dev_id,
            device_id_type=pltpu.DeviceIdType.LOGICAL,
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    x = jnp.arange(2 * 8 * 128.0).reshape((2 * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=mem)],
          out_specs=pl.BlockSpec(memory_space=mem),
          out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
      )(x)

    devices = jax.devices()[:2]
    mesh = jax.sharding.Mesh(devices, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  @parameterized.named_parameters(
      ('left', 'left'),
      ('right', 'right')
  )
  def test_pallas_call_axis_index(self, direction):
    # Implements very simple collective permute
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        num_devices = lax.psum(1, 'x')
        if direction == 'right':
          neighbor = lax.rem(my_id + 1, num_devices)
        else:
          neighbor = lax.rem(my_id - 1, num_devices)
          # Neighbor might be negative here so we add num_devices in case
          neighbor = jnp.where(neighbor < 0, neighbor + num_devices, neighbor)
        pltpu.semaphore_signal(ready_sem, device_id=neighbor)
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=neighbor
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    num_devices = jax.local_device_count()
    x = jnp.arange(num_devices * 8 * 128).reshape((num_devices * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (jax.device_count(),), jax.devices())
    mesh = jax.sharding.Mesh(device_mesh, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    if direction == 'right':
      expected = jnp.concatenate([x[-8:], x[:-8]])
    else:
      expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  @parameterized.named_parameters(('left', 'left'), ('right', 'right'))
  def test_pallas_call_axis_index_2d_mesh(self, direction):
    # Implements very simple collective permute in a 2D mesh.
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        my_other_id = lax.axis_index('y')
        axis_size = lax.psum(1, 'x')
        if direction == 'right':
          neighbor = lax.rem(my_id + 1, axis_size)
        else:
          neighbor = lax.rem(my_id - 1, axis_size)
          # Neighbor might be negative here so we add num_devices in case
          neighbor = jnp.where(neighbor < 0, neighbor + axis_size, neighbor)
        pltpu.semaphore_signal(ready_sem, device_id=(my_other_id, neighbor))
        pltpu.semaphore_wait(ready_sem)
        copy_done = pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=(my_other_id, neighbor)
        )
        copy_done.wait_send()
        copy_done.wait_recv()

      pltpu.run_scoped(
          body,
          pltpu.SemaphoreType.REGULAR,
          pltpu.SemaphoreType.DMA,
          pltpu.SemaphoreType.DMA,
      )

    axis_size = jax.device_count() // 2
    x = jnp.arange(axis_size * 8 * 128).reshape((axis_size * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (2, axis_size), jax.devices()
    )
    mesh = jax.sharding.Mesh(device_mesh, ['y', 'x'])
    y = jax.jit(
        shard_map.shard_map(
            body,
            mesh,
            in_specs=P('x', None),
            out_specs=P('x', None),
            check_rep=False,
        )
    )(x)
    if direction == 'right':
      expected = jnp.concatenate([x[-8:], x[:-8]])
    else:
      expected = jnp.concatenate([x[8:], x[:8]])
    np.testing.assert_allclose(y, expected)

  def test_barrier_semaphore(self):
    def kernel(x_ref, y_ref):
      def body(ready_sem, send_sem, recv_sem):
        my_id = lax.axis_index('x')
        num_devices = lax.psum(1, 'x')
        neighbor = lax.rem(my_id + 1, num_devices)
        barrier_sem = pltpu.get_barrier_semaphore()
        pltpu.semaphore_signal(barrier_sem, device_id=neighbor)
        pltpu.semaphore_wait(barrier_sem)
        pltpu.semaphore_signal(ready_sem, device_id=neighbor)
        pltpu.semaphore_wait(ready_sem)
        pltpu.async_remote_copy(
            x_ref, y_ref, send_sem, recv_sem, device_id=neighbor
        ).wait()

      pltpu.run_scoped(body, pltpu.SemaphoreType.REGULAR,
                       pltpu.SemaphoreType.DMA, pltpu.SemaphoreType.DMA)

    num_devices = jax.local_device_count()
    x = jnp.arange(num_devices * 8 * 128).reshape((num_devices * 8, 128))

    def body(x):
      return pl.pallas_call(
          kernel,
          in_specs=[pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM)],
          out_specs=pl.BlockSpec(memory_space=pltpu.TPUMemorySpace.VMEM),
          out_shape=x,
          mosaic_params=dict(collective_id=0)
      )(x)

    device_mesh = mesh_utils.create_device_mesh(
        (jax.device_count(),), jax.devices())
    mesh = jax.sharding.Mesh(device_mesh, ['x'])
    y = jax.jit(
        shard_map.shard_map(
            body, mesh, in_specs=P('x'), out_specs=P('x'), check_rep=False
        )
    )(x)
    expected = jnp.concatenate([x[-8:], x[:-8]])
    np.testing.assert_allclose(y, expected)


class PallasCallTest(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    if jtu.device_under_test() != "tpu":
      self.skipTest("Test only works on TPU")

  def test_cost_analysis(self):
    def kernel(x, y):
      y[:] = x[:]
    x = jnp.arange(1024.).reshape(8, 128)
    f = pl.pallas_call(
        kernel,
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.float32),
        mosaic_params=dict(
            cost_estimate=pltpu.CostEstimate(
                flops=1234, transcendentals=21, bytes_accessed=12345
            )
        ),
    )
    (analysis_result,) = jax.jit(f).lower(x).compile().cost_analysis()
    self.assertEqual(analysis_result['flops'], 1234)
    self.assertEqual(analysis_result['transcendentals'], 21)
    self.assertEqual(analysis_result['bytes accessed'], 12345)

  def test_vmem_limit(self):
    shape = (128, 128)

    def kernel(x_ref, y_ref):
      y_ref[...] = x_ref[...]

    x = jnp.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    with self.assertRaises(xla_extension.XlaRuntimeError):
      pl.pallas_call(
          kernel, out_shape=x, mosaic_params=dict(vmem_limit_bytes=256)
      )(x)
    pl.pallas_call(
        kernel, out_shape=x, mosaic_params=dict(vmem_limit_bytes=int(2**18))
    )(x)


class PallasUXTest(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    if jtu.device_under_test() != "tpu":
      self.skipTest("Test only works on TPU")

  def test_mlir_location(self):
    # Make sure that MLIR locations are correctly propagated to primitives.
    args = (jax.ShapeDtypeStruct((8, 128), jnp.float32),)
    f = example_kernel.double
    as_tpu_kernel = mosaic.as_tpu_kernel
    def capture_as_tpu_kernel(module, *args, **kwargs):
      asm = module.operation.get_asm(enable_debug_info=True)
      self.assertIn('example_kernel.py":25', asm)
      return as_tpu_kernel(module, *args, **kwargs)
    mosaic.as_tpu_kernel = capture_as_tpu_kernel
    try:
      jax.jit(f).lower(*args)
    finally:
      mosaic.as_tpu_kernel = as_tpu_kernel


class PallasMegacoreTest(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    if jtu.device_under_test() != "tpu":
      self.skipTest("Test only works on TPU")

  def test_megacore_splitting(self):
    # We want to make sure a 3-sized dimension is split across megacore
    # correctly, and if we combine the (3, 3) dimensions together it is still
    # correct.

    def matmul_kernel(x_ref, y_ref, z_ref):
      @pl.when(pl.program_id(2) == 0)
      def _():
        z_ref[...] = jnp.zeros_like(z_ref)
      z_ref[...] += x_ref[...] @ y_ref[...]

    k1, k2 = jax.random.split(jax.random.key(0))
    x = jax.random.uniform(k1, (3, 3, 512, 512))
    y = jax.random.uniform(k2, (3, 3, 512, 512))

    z = jax.vmap(jax.vmap(
        pl.pallas_call(
            matmul_kernel,
            out_shape=jax.ShapeDtypeStruct((512, 512), jnp.float32),
            grid=(4, 4, 4),
            in_specs=[
                pl.BlockSpec(lambda i, j, k: (i, k), (128, 128)),
                pl.BlockSpec(lambda i, j, k: (k, j), (128, 128)),
            ],
            out_specs=pl.BlockSpec(lambda i, j, k: (i, j), (128, 128)),
            debug=True,
        )
    ))(x, y)
    np.testing.assert_allclose(z, jax.vmap(jax.vmap(jnp.dot))(x, y))


class PallasCallVmapTest(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    if jtu.device_under_test() != "tpu":
      self.skipTest("Test only works on TPU")

  def test_scratch_input_vmap(self):
    """Test that vmapp-ing a kernel with scratch inputs works correctly."""

    # Scratch inputs are only available for PallasTPU. This is why this test
    # does not live with the other vmap tests in:
    # jax/tests/pallas/pallas_test.py
    def add_one_with_scratch(x_ref, o_ref, scratch_ref):
      scratch_ref[...] = jnp.ones_like(scratch_ref[...])
      o_ref[...] = x_ref[...] + scratch_ref[...]

    tile_size = 128
    tile_shape = (tile_size, tile_size)
    array_shape = (2 * tile_size, 2 * tile_size)
    vmapped_add_one_with_scratch = jax.vmap(
        pl.pallas_call(
            add_one_with_scratch,
            out_shape=jax.ShapeDtypeStruct(array_shape, jnp.int32),
            grid_spec=pltpu.PrefetchScalarGridSpec(
                num_scalar_prefetch=0,
                in_specs=[pl.BlockSpec(lambda i, j: (i, j), tile_shape)],
                out_specs=pl.BlockSpec(lambda i, j: (i, j), tile_shape),
                scratch_shapes=[pltpu.VMEM(tile_shape, dtype=jnp.int32)],
                grid=(2, 2),
            ),
        )
    )

    x = jnp.broadcast_to(jnp.arange(array_shape[0]), (10, *array_shape))

    out = vmapped_add_one_with_scratch(x)
    out_ref = x + 1

    np.testing.assert_array_equal(out, out_ref, strict=True)


class PallasCallControlFlowTest(jtu.JaxTestCase):

  def setUp(self):
    super().setUp()
    if jtu.device_under_test() != "tpu":
      self.skipTest("Test only works on TPU")

  def test_nested_conds(self):
    def kernel(y_ref):
      def select(pred, x, y, nesting=0):
        def _true():
          if nesting == 0:
            return x + 1
          return select(x == nesting, x, y, nesting=nesting - 1)

        def _false():
          if nesting == 0:
            return y + 1
          return select(y == nesting, x, y, nesting=nesting - 1)

        return jax.lax.cond(pred, _true, _false)

      j = pl.program_id(0)
      j = select(j == 0, j, j, nesting=4)
      y_ref[...] = j * jnp.ones_like(y_ref)

    pl.pallas_call(
        kernel,
        grid=(1,),
        out_specs=pl.BlockSpec(lambda i: (0, 0), (8, 128)),
        out_shape=jax.ShapeDtypeStruct((8, 128), jnp.int32),
    )()
    return


if __name__ == "__main__":
  absltest.main(testLoader=jtu.JaxTestLoader())
