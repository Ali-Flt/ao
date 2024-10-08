# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD 3-Clause license found in the
# LICENSE file in the root directory of this source tree.

"""
This is a script to estimate the benefit from converting a `torch.nn.Linear`
layer to float8, by estimating the difference in e2e GPU kernel time between:
1. bf16 gemms in fwd and bwd, and 
2. float8 gemms in fwd and bwd, and float8 overhead

The gemm times are estimated either from direct measurements via benchmarks,
or with a roofline estimation based on TOPS and peak compute bandwidth of an 
NVIDIA H100.

The float8 overhead times are estimated by counting memory reads and writes
based on the specified float8 scaling, and estimating that we can achieve
a certain % of machine peak memory bandwidth when performing these reads and writes.

Additional context:
1. the formulas for fwd/bwd gemms in a linear layer, with corresponding input
   and output sizes:

  input @ weight_t = output
  MxK @ KxN => MxN

  grad_output @ weight = grad_input
  MxN @ NxK => MxK

  input_t @ grad_output = grad_weight
  KxM @ MxN => KxN

2. we properly model the worst-case of the current torch.compile limitations regarding
   float8 scaling
3. assume for float8 activations/gradients that torch.compile will fuse to the
preceding op. Note that this is not always true in practice.
4. assume no AC (TODO model it)
5. assume no float8 all-gather (TODO model it)
"""

import csv
import copy
import time
from typing import Optional

import fire
import pandas as pd
import sympy

import torch
import torch.utils.benchmark as benchmark

from torchao.float8.roofline_utils import (
    get_gemm_time_sympy,
    get_float8_mem_sympy,
)


def benchmark_fn_in_sec(f, *args, **kwargs):
    # Manual warmup
    for _ in range(4):
        f(*args, **kwargs)
    t0 = benchmark.Timer(
        stmt="f(*args, **kwargs)", globals={"args": args, "kwargs": kwargs, "f": f}
    )
    measurement = t0.blocked_autorange()
    return measurement.mean


def get_gemm_times_cache(gemm_benchmarks_file: str):
    cache = {}
    with open(gemm_benchmarks_file, 'r') as f:
        reader = csv.reader(f)
        for idx, row in enumerate(reader):
            if idx == 0:
                # skip headers
                continue
            idx1, fast_accum, name, M, K, N, bf16_time, fp8_time, speedup = row
            fast_accum = fast_accum == 'True'
            cache[(int(M), int(K), int(N), fast_accum)] = (float(bf16_time), float(fp8_time))
    return cache


def run(
    outfile: str,
    gemm_time_strategy: str = "benchmarks",
    gemm_benchmarks_file: Optional[str] = None,
    model_torch_compile_limitations: bool = False,
    scaling_type_input: str = "dynamic",
    scaling_type_weight: str = "dynamic",
    scaling_type_grad_output: str = "dynamic",
):
    """
    Args:
    * `gemm_time_strategy`:
      - `benchmarks`: use benchmarks for gemm times (more accurate for all shapes)
      - `roofline`: use roofline model for gemm times (only accurate for large shapes)
    * `gemm_benchmarks_file`: filepath of precalculated gemm benchmarks, generated by
      `python benchmarks/float8/bench_matmul.py --shape_gen_name sweep --use_gpu_kernel_time True`
    * `model_torch_compile_limitations`: if True, adjust memory traffic estimates based
      on current limitations of torch.compile for float8 scaling/casting kernels.
    * `scaling_type_input`: `dynamic` or `delayed`
    * `scaling_type_weight`: `dynamic` or `delayed`
    * `scaling_type_grad_output`: `dynamic` or `delayed`
    """

    assert gemm_time_strategy in ("benchmarks", "roofline"), \
        "`gemm_time_strategy` must be 'benchmarks' or 'roofline'"
    if gemm_time_strategy == "benchmarks":
        assert gemm_benchmarks_file is not None, \
            f'gemm_benchmarks_file was not provided, this is not supported yet'
        gemm_times_cache = get_gemm_times_cache(gemm_benchmarks_file)
    else:
        gemm_times_cache = None

    M, K, N = sympy.symbols('M K N')

    fp8_mem_time_sympy = get_float8_mem_sympy(
        M, 
        K, 
        N, 
        model_torch_compile_limitations,
        scaling_type_input,
        scaling_type_weight,
        scaling_type_grad_output,
    )
    print()
    print('fp8_mem_time_sympy', fp8_mem_time_sympy)

    if gemm_time_strategy == "roofline":
        bf16_gemm_time_sympy = get_gemm_time_sympy(M, K, N, torch.bfloat16)
        print('bf16_gemm_time_sympy', bf16_gemm_time_sympy)
        fp8_gemm_time_sympy = get_gemm_time_sympy(M, K, N, torch.float8_e4m3fn)
        print('fp8_gemm_time_sympy', fp8_gemm_time_sympy)
        print()
    else:
        print()

    # quick sweep of runtime estimated by this model for powers of 2 of M, N, K
    Ms = [2 ** x for x in range(9, 16)]  # 256 to 65536
    Ks = Ms
    Ns = Ms

    headers = [
        'M', 'K', 'N', 
        'bf16_time_s', 
        'fp8_gemm_time_s', 'fp8_mem_time_s', 'fp8_time_s', 
        'speedup',
    ]
    results = []

    for M_val in Ms:
        for K_val in Ks:
            for N_val in Ns:
                if gemm_time_strategy == "benchmarks":
                    bf16_time_val = (
                        gemm_times_cache[(M_val, K_val, N_val, True)][0]
                        + gemm_times_cache[(M_val, N_val, K_val, False)][0]
                        + gemm_times_cache[(K_val, M_val, N_val, False)][0]
                    )
                    fp8_gemm_time_s = (
                        gemm_times_cache[(M_val, K_val, N_val, True)][1]
                        + gemm_times_cache[(M_val, N_val, K_val, False)][1]
                        + gemm_times_cache[(K_val, M_val, N_val, False)][1]
                    )
                    fp8_mem_time_s = fp8_mem_time_sympy.subs(M, M_val).subs(K, K_val).subs(N, N_val)
                    fp8_time_val = fp8_gemm_time_s + fp8_mem_time_s
                else:
                    assert gemm_time_strategy == "roofline", "unsupported"
                    bf16_time_val = bf16_gemm_time_sympy.subs(M, M_val).subs(K, K_val).subs(N, N_val)
                    fp8_gemm_time_s = fp8_gemm_time_sympy.subs(M, M_val).subs(K, K_val).subs(N, N_val)
                    fp8_mem_time_s = fp8_mem_time_sympy.subs(M, M_val).subs(K, K_val).subs(N, N_val)
                    fp8_time_val = fp8_gemm_time_s + fp8_mem_time_s

                results.append([
                    M_val, K_val, N_val, 
                    bf16_time_val, 
                    fp8_gemm_time_s, fp8_mem_time_s, fp8_time_val, 
                    bf16_time_val / fp8_time_val,
                ])

    df = pd.DataFrame(results, columns=headers)
    print(df)
    df.to_csv(outfile)
    print('done')

if __name__ == '__main__':
    fire.Fire(run)
