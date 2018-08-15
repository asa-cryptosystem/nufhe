# Copyright (C) 2018 NuCypher
#
# This file is part of nufhe.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from .numeric_functions import Torus32, t32_to_phase
from .polynomials import TorusPolynomialArray, shift_tp_inverted_power
from .lwe import LweKey, LweSampleArray, LweKeyswitchKey, lwe_keyswitch
from .tgsw import (
    TGswKey,
    TransformedTGswSampleArray,
    TGswParams,
    TGswSampleArray,
    tgsw_transform_samples,
    tgsw_encrypt_int,
    tgsw_transformed_external_mul,
    )
from .tlwe import (
    TLweSampleArray,
    tlwe_noiseless_trivial,
    tlwe_shift_polynomials,
    tlwe_add_to,
    tlwe_extract_lwe_samples,
    tlwe_copy,
    )
from .blind_rotate import BlindRotate_gpu
from .performance import PerformanceParameters


def lwe_bootstrapping_key(
        thr, rng, ks_decomp_length: int, ks_log2_base: int, key_in: LweKey, rgsw_key: TGswKey,
        perf_params: PerformanceParameters):

    bk_params = rgsw_key.params
    in_out_params = key_in.params
    accum_params = bk_params.tlwe_params
    extract_params = accum_params.extracted_lweparams

    accum_key = rgsw_key.tlwe_key
    extracted_key = LweKey.from_tlwe_key(extract_params, accum_key)

    ks = LweKeyswitchKey(thr, rng, extracted_key, key_in, ks_decomp_length, ks_log2_base)

    bk = TGswSampleArray(thr, bk_params, (in_out_params.size,))
    kin = key_in.key
    noise = accum_params.min_noise

    tgsw_encrypt_int(thr, rng, bk, kin, noise, rgsw_key, perf_params)

    return bk, ks


class LweBootstrappingKeyFFT:

    def __init__(
            self, thr, rng, ks_decomp_length: int, ks_log2_base: int,
            lwe_key: LweKey, tgsw_key: TGswKey, perf_params: PerformanceParameters):

        in_out_params = lwe_key.params
        bk_params = tgsw_key.params
        accum_params = bk_params.tlwe_params
        extract_params = accum_params.extracted_lweparams

        bk, ks = lwe_bootstrapping_key(
            thr, rng, ks_decomp_length, ks_log2_base, lwe_key, tgsw_key, perf_params)

        n = in_out_params.size

        # Bootstrapping Key FFT
        bkFFT = TransformedTGswSampleArray(thr, bk_params, (n,))
        tgsw_transform_samples(thr, bkFFT, bk, perf_params)

        self.in_out_params = in_out_params # paramètre de l'input et de l'output. key: s
        self.bk_params = bk_params # params of the Gsw elems in bk. key: s"
        self.accum_params = accum_params # params of the accum variable key: s"
        self.extract_params = extract_params # params after extraction: key: s'
        self.bkFFT = bkFFT # the bootstrapping key (s->s")
        self.ks = ks # the keyswitch key (s'->s)


def nufhe_MuxRotate_FFT(
        thr, result: TLweSampleArray, accum: TLweSampleArray, bki: TransformedTGswSampleArray, bk_idx: int,
        barai, bk_params: TGswParams, perf_params: PerformanceParameters):

    # TYPING: barai::Array{Int32}
    # ACC = BKi*[(X^barai-1)*ACC]+ACC
    # temp = (X^barai-1)*ACC
    tlwe_shift_polynomials(thr, result, accum, barai, bk_idx)

    # temp *= BKi
    tgsw_transformed_external_mul(thr, result, bki, bk_idx, perf_params)

    # ACC += temp
    tlwe_add_to(thr, result, accum)


"""
 * multiply the accumulator by X^sum(bara_i.s_i)
 * @param accum the TLWE sample to multiply
 * @param bk An array of n TGSW FFT samples where bk_i encodes s_i
 * @param bara An array of n coefficients between 0 and 2N-1
 * @param bk_params The parameters of bk
"""
def nufhe_blindRotate_FFT(
        thr, accum: TLweSampleArray, bkFFT: TransformedTGswSampleArray, bara, n: int, bk_params: TGswParams,
        perf_params: PerformanceParameters):

    # TYPING: bara::Array{Int32}
    temp = TLweSampleArray(thr, bk_params.tlwe_params, accum.shape)

    temp2 = temp
    temp3 = accum

    accum_in_temp3 = True

    for i in range(n):
        # TODO: here we only need to pass bkFFT[i] and bara[:,i],
        # but Reikna kernels have to be recompiled for every set of strides/offsets,
        # so for now we are just passing full arrays and an index.
        nufhe_MuxRotate_FFT(thr, temp2, temp3, bkFFT, i, bara, bk_params, perf_params)

        temp2, temp3 = temp3, temp2
        accum_in_temp3 = not accum_in_temp3

    # TODO: add a test that checks this
    if not accum_in_temp3: # temp3 != accum
        tlwe_copy(thr, accum, temp3)


"""
 * result = LWE(v_p) where p=barb-sum(bara_i.s_i) mod 2N
 * @param result the output LWE sample
 * @param v a 2N-elt anticyclic function (represented by a TorusPolynomial)
 * @param bk An array of n TGSW FFT samples where bk_i encodes s_i
 * @param barb A coefficients between 0 and 2N-1
 * @param bara An array of n coefficients between 0 and 2N-1
 * @param bk_params The parameters of bk
"""
def nufhe_blindRotateAndExtract_FFT(
        thr, result: LweSampleArray,
        v: TorusPolynomialArray, bk: LweBootstrappingKeyFFT,
        barb, bara,
        perf_params: PerformanceParameters,
        no_keyswitch=False):

    # TYPING: barb::Array{Int32},
    # TYPING: bara::Array{Int32}

    bk_params = bk.bk_params

    if not no_keyswitch:
        extracted_result = LweSampleArray.empty(
            thr, bk.accum_params.extracted_lweparams, result.shape_info.shape)
    else:
        extracted_result = result

    accum_params = bk_params.tlwe_params
    extract_params = accum_params.extracted_lweparams
    N = accum_params.polynomial_degree

    # testvector = X^{2N-barb}*v
    testvectbis = TorusPolynomialArray.empty(thr, N, extracted_result.shape_info.shape)
    shift_tp_inverted_power(thr, testvectbis, barb, v)

    # Accumulator
    acc = TLweSampleArray(thr, accum_params, extracted_result.shape_info.shape)
    tlwe_noiseless_trivial(thr, acc, testvectbis)

    if perf_params.single_kernel_bootstrap:
        # includes blindrotate, extractlwesample and (optionally) keyswitch
        BlindRotate_gpu(result, acc, bk, bara, perf_params, no_keyswitch=no_keyswitch)

    else:
        # Blind rotation
        nufhe_blindRotate_FFT(
            thr, acc, bk.bkFFT, bara, bk.in_out_params.size, bk_params, perf_params)

        # Extraction
        tlwe_extract_lwe_samples(thr, extracted_result, acc)

        if not no_keyswitch:
            lwe_keyswitch(thr, result, bk.ks, extracted_result)


"""
 * result = LWE(mu) iff phase(x)>0, LWE(-mu) iff phase(x)<0
 * @param result The resulting LweSample
 * @param bk The bootstrapping + keyswitch key
 * @param mu The output message (if phase(x)>0)
 * @param x The input sample
"""
def bootstrap(
        thr, result: LweSampleArray, bk: LweBootstrappingKeyFFT, mu: Torus32, x: LweSampleArray,
        perf_params: PerformanceParameters,
        no_keyswitch=False):

    accum_params = bk.accum_params
    N = accum_params.polynomial_degree

    testvect = TorusPolynomialArray.empty(thr, N, result.shape_info.shape)

    # Modulus switching
    barb = thr.array(x.b.shape, Torus32)
    bara = thr.array(x.a.shape, Torus32)

    t32_to_phase(thr, barb, x.b, 2 * N)
    t32_to_phase(thr, bara, x.a, 2 * N)

    # the initial testvec = [mu,mu,mu,...,mu]
    testvect.coeffs.fill(mu)

    # Bootstrapping rotation and extraction
    nufhe_blindRotateAndExtract_FFT(
        thr, result, testvect, bk, barb, bara, perf_params,
        no_keyswitch=no_keyswitch)
